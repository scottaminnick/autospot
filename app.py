from flask import Flask, request, render_template_string, session, redirect, url_for
import datetime
import numpy as np
import os
import warnings
import hmac
from functools import wraps
import xarray as xr
import requests
import uuid
import time
import io
import base64
import matplotlib
matplotlib.use('Agg') # Required for thread-safe plotting in Flask
from matplotlib.figure import Figure
import matplotlib.patches as mpatches
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from concurrent.futures import ThreadPoolExecutor

# --- UI AND CSS LAYER ---
REPORT_CSS = """
<style>
    body { font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }
    .container { background-color: #fff; padding: 30px; border-radius: 8px; max-width: 700px; margin: auto; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
    .report-wrap { background: #fff; border: 1px solid #ccc; padding: 25px; max-width: 950px; margin: 20px auto; box-shadow: 0 2px 5px rgba(0,0,0,0.1); overflow-x: auto; }
    pre { font-family: 'Courier New', Courier, monospace !important; font-size: 13px; line-height: 1.5; white-space: pre !important; color: #111; margin: 0; }
    label { display: block; margin-top: 10px; font-weight: bold; }
    input[type='number'], input[type='text'], input[type='date'], select { width: 100%; padding: 8px; margin-top: 5px; box-sizing: border-box; }
    .flex-row { display: flex; gap: 10px; }
    .flex-col { flex: 1; }
    button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; margin-top: 20px; cursor: pointer; }
    .btn-transmit { background: #28a745; width: 100%; font-size: 16px; margin-bottom: 20px; font-weight: bold; }
    .btn-transmit:hover { background: #218838; }
    .taf-editor { width: 100%; font-family: 'Courier New', Courier, monospace !important; font-size: 13px; line-height: 1.5; color: #111; padding: 15px; border: 1px solid #007bff; border-radius: 4px; box-sizing: border-box; resize: vertical; white-space: pre; background-color: #f8f9fa; margin-bottom: 15px; }
</style>
"""

HTML_FORM = """
<!DOCTYPE html><html>
<head><title>AutoSpot Request</title>{{ css|safe }}</head>
<body>
<div class='container'>
    <h2>NWSI 10-813 Spot Forecast Generator</h2>
    <form action='/submit' method='post'>
        <label>Model Routing:</label>
        <select name='model_choice'>
            <option value='AUTO'>Best Available (Auto-Detect)</option>
            <option value='HRRR'>Force HRRR (CONUS 3km)</option>
            <option value='GFS'>Force GFS (Global 25km)</option>
        </select>

        <div class="flex-row">
            <div class="flex-col">
                <label>Latitude (N):</label>
                <input type='number' name='latitude' step='0.01' placeholder='e.g., 39.1' required>
            </div>
            <div class="flex-col">
                <label>Longitude (Negative for West):</label>
                <input type='number' name='longitude' step='0.01' placeholder='e.g., -94.6' required>
            </div>
        </div>

        <div class="flex-row">
            <div class="flex-col">
                <label>TAF Valid Start Date (UTC):</label>
                <input type='date' name='start_date' required>
            </div>
            <div class="flex-col">
                <label>Valid Start Hour (UTC):</label>
                <select name='start_hour'>
                    <option value='00'>00z</option><option value='01'>01z</option>
                    <option value='02'>02z</option><option value='03'>03z</option>
                    <option value='04'>04z</option><option value='05'>05z</option>
                    <option value='06'>06z</option><option value='07'>07z</option>
                    <option value='08'>08z</option><option value='09'>09z</option>
                    <option value='10'>10z</option><option value='11'>11z</option>
                    <option value='12'>12z</option><option value='13'>13z</option>
                    <option value='14'>14z</option><option value='15'>15z</option>
                    <option value='16'>16z</option><option value='17'>17z</option>
                    <option value='18'>18z</option><option value='19'>19z</option>
                    <option value='20'>20z</option><option value='21'>21z</option>
                    <option value='22'>22z</option><option value='23'>23z</option>
                </select>
            </div>
        </div>

        <div class="flex-row">
            <div class="flex-col">
                <label>Forecast Duration:</label>
                <select name='duration'>
                    <option value='12'>12 Hours</option>
                    <option value='24' selected>24 Hours</option>
                    <option value='36'>36 Hours</option>
                </select>
            </div>
            <div class="flex-col">
                <label>SAR Name / Label:</label>
                <input type='text' name='sar_name' value='AUTO-SPOT-PROD'>
            </div>
        </div>

        <button type='submit'>Generate Report</button>
    </form>
</div>
<script>
    window.onload = function() {
        var now = new Date();
        document.getElementsByName('start_date')[0].value = now.toISOString().slice(0,10);
        var currentHour = now.getUTCHours().toString().padStart(2, '0');
        document.getElementsByName('start_hour')[0].value = currentHour;
    };
</script>
</body></html>
"""

# --- USGS API FETCHER ---
def get_usgs_elevation(lat, lon):
    url = "https://epqs.nationalmap.gov/v1/json"
    params = {"x": lon, "y": lat, "units": "Feet", "wkid": 4326, "includeDate": "false"}
    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'value' in data and data['value'] is not None:
                val = float(data['value'])
                if val > -1000: return val
    except Exception:
        pass
    return None

# --- METEOROLOGICAL TRANSLATORS ---
class AviationTranslator:
    @staticmethod
    def format_wind(direction, speed, gust=0):
        speed = int(round(speed))
        gust = int(round(gust))
        dir_rounded = int(round(direction / 10.0) * 10)
        if dir_rounded == 0: dir_rounded = 360
        if speed < 3 and gust >= 10: speed = 3
        if speed == 0: return "00000KT"
        if speed <= 3: dir_str = "VRB"
        else: dir_str = f"{dir_rounded:03d}"
        spd_str = f"{speed:02d}"
        if gust >= 10 and (gust - speed) >= 10:
            return f"{dir_str}{spd_str}G{gust:02d}KT"
        return f"{dir_str}{spd_str}KT"

    @staticmethod
    def extract_visibility(vis_meters):
        vis_sm = float(vis_meters) / 1609.34
        if vis_sm >= 6: return "P6SM"
        elif vis_sm >= 5: return "5SM"
        elif vis_sm >= 4: return "4SM"
        elif vis_sm >= 3: return "3SM"
        elif vis_sm >= 2: return "2SM"
        elif vis_sm >= 1: return "1SM"
        elif vis_sm >= 0.5: return "1/2SM"
        elif vis_sm >= 0.25: return "1/4SM"
        else: return "M1/4SM"

    @staticmethod
    def extract_present_weather(prate, is_convective, vis_string):
        wx_string = ""
        intensity = "-" if 0 < prate < 0.0005 else ("+" if prate > 0.002 else "")
        if prate > 0: wx_string = f"{intensity}TSRA" if is_convective else f"{intensity}RA"
        if wx_string == "" and vis_string != "P6SM":
            wx_string = "FG" if vis_string in ["1/4SM", "1/2SM", "M1/4SM"] else "BR"
        return wx_string if wx_string else "NSW"

    @staticmethod
    def evaluate_clouds(tcdc, cbh, is_convective, prate, lcc=0, mcc=0, hcc=0):
        val = float(tcdc)
        if cbh > 30000 or np.isnan(cbh): cbh = 2500
        cbh = max(100, cbh)

        if val < 10:
            if is_convective:
                h_str = f"{int((round(cbh / 100) * 100) / 100):03d}"
                return f"FEW{h_str}CB"
            elif prate > 0:
                h_str = f"{int((round(cbh / 100) * 100) / 100):03d}"
                return f"FEW{h_str}"
            return "SKC"

        if lcc == 0 and mcc == 0 and hcc == 0: lcc = val
        layers = []
        current_cov = 0

        if lcc >= 10:
            current_cov = min(100, current_cov + lcc)
            hgt = cbh if cbh < 8000 else 2500
            layers.append((current_cov, hgt, is_convective))
        if mcc >= 10:
            current_cov = min(100, current_cov + mcc)
            hgt = cbh if (not layers and cbh < 18000) else max(cbh + 4000, 10000)
            layers.append((current_cov, hgt, False))
        if hcc >= 10:
            current_cov = min(100, current_cov + hcc)
            hgt = cbh if (not layers) else max(cbh + 10000, 20000)
            layers.append((current_cov, hgt, False))

        if not layers: layers.append((val, cbh, is_convective))

        cloud_strings = []
        for cov, hgt, conv in layers:
            if cov < 30: amt = "FEW"
            elif cov < 60: amt = "SCT"
            elif cov < 90: amt = "BKN"
            else: amt = "OVC"

            if hgt < 3000: rounded_ft = round(hgt / 100) * 100
            elif hgt < 10000: rounded_ft = round(hgt / 500) * 500
            else: rounded_ft = round(hgt / 1000) * 1000
            if rounded_ft < 100: rounded_ft = 100

            h_str = f"{int(rounded_ft / 100):03d}"
            cb_str = "CB" if conv else ""
            cloud_strings.append(f"{amt}{h_str}{cb_str}")
            if amt == "OVC": break

        return " ".join(cloud_strings[:3])

# --- NWSI 10-813 LOGIC ENGINE ---
class FirstGuessTAF:
    def __init__(self, sar_name, lat, lon, sfc_data, grid_data, cycle_label, duration, true_elev_ft):
        self.sar_name = sar_name.upper()
        self.lat = lat
        self.lon = lon
        self.sfc_data = sfc_data
        self.grid_data = grid_data
        self.cycle_label = cycle_label
        self.duration = duration
        self.true_elev_ft = true_elev_ft
        self.taf_groups = []

    def check_10_813_triggers(self, base, current):
        wind_shift = abs(current['wdir'] - base['wdir'])
        if wind_shift > 180: wind_shift = 360 - wind_shift
        if current['wspd'] >= 8 and wind_shift >= 30: return True
        if wind_shift >= 45: return True
        if abs(current['wspd'] - base['wspd']) >= 5: return True
        if current['gust'] - current['wspd'] >= 8 and base['gust'] == 0: return True

        base_vis_str = AviationTranslator.extract_visibility(base['vis'])
        curr_vis_str = AviationTranslator.extract_visibility(current['vis'])
        if base_vis_str != curr_vis_str: return True

        base_wx = AviationTranslator.extract_present_weather(base['prate'], base['is_conv'], base_vis_str)
        curr_wx = AviationTranslator.extract_present_weather(current['prate'], current['is_conv'], curr_vis_str)
        if base_wx != curr_wx: return True

        for t in [3000, 2000, 1500, 1000, 500, 200]:
            if (base['cbh'] >= t and current['cbh'] < t) or (base['cbh'] < t and current['cbh'] >= t):
                return True
        return False

    def process_hour(self, data):
        wind = AviationTranslator.format_wind(data['wdir'], data['wspd'], data['gust'])
        vis = AviationTranslator.extract_visibility(data['vis'])
        wx = AviationTranslator.extract_present_weather(data['prate'], data['is_conv'], vis)
        clouds = AviationTranslator.evaluate_clouds(data['tcc'], data['cbh'], data['is_conv'], data['prate'], data.get('lcc', 0), data.get('mcc', 0), data.get('hcc', 0))
        wx_str = f" {wx}" if wx != "NSW" else ""
        return f"{wind} {vis}{wx_str} {clouds}".strip()

    def generate_report(self, valid_start_time):
        if not self.sfc_data: return "Error: Model data retrieval failed or timed out."
        end_time = valid_start_time + datetime.timedelta(hours=self.duration)

        lat_dir = "N" if self.lat >= 0 else "S"
        lon_dir = "E" if self.lon >= 0 else "W"
        elev_str = f"{int(self.true_elev_ft)}FT MSL"
        self.taf_groups.append(f"SAR: {self.sar_name}  LOC: {abs(self.lat):.2f}{lat_dir}/{abs(self.lon):.2f}{lon_dir}  ELEV: {elev_str}")

        base_data = self.sfc_data[0]
        header_line = f"TAF {abs(self.lat):.2f}{lat_dir}/{abs(self.lon):.2f}{lon_dir} {valid_start_time.strftime('%d%H%M')}Z {valid_start_time.strftime('%d%H')}/{end_time.strftime('%d%H')} {self.process_hour(base_data)}"
        self.taf_groups.append(header_line)

        i = 1
        while i < len(self.sfc_data):
            current_data = self.sfc_data[i]
            if self.check_10_813_triggers(base_data, current_data):
                is_tempo = False
                if i + 1 < len(self.sfc_data):
                    next_data = self.sfc_data[i + 1]
                    if not self.check_10_813_triggers(base_data, next_data): is_tempo = True

                curr_valid = valid_start_time + datetime.timedelta(hours=int(current_data['fhour']) - int(self.sfc_data[0]['fhour']))
                ts = curr_valid.strftime('%d%H')

                if is_tempo:
                    next_valid = valid_start_time + datetime.timedelta(hours=int(next_data['fhour']) - int(self.sfc_data[0]['fhour']))
                    next_ts = next_valid.strftime('%d%H')
                    if current_data['is_conv']:
                        self.taf_groups.append(f"  PROB30 {ts}/{next_ts} {self.process_hour(current_data)}")
                    else:
                        self.taf_groups.append(f"    TEMPO {ts}/{next_ts} {self.process_hour(current_data)}")
                    i += 1
                else:
                    self.taf_groups.append(f"  FM{ts}00 {self.process_hour(current_data)}")
                    base_data = current_data
            i += 1

        self.taf_groups.append(f"\nDATA BASED ON {self.cycle_label} RUN VALID {valid_start_time.strftime('%d%H%M')}Z.")

        levels = [3000, 6000, 9000, 12000, 18000, 24000, 30000, 34000, 39000]
        header = f"{'DDHH':<6}" + "".join([f"{ft:<8}" for ft in levels])
        self.taf_groups.append(header)

        for h in sorted(self.grid_data.keys()):
            row_offset = h - int(self.sfc_data[0]['fhour'])
            valid_ts = (valid_start_time + datetime.timedelta(hours=row_offset)).strftime('%d%H')
            grid_row = f"{valid_ts:<6}"

            for ft in levels:
                if ft - self.true_elev_ft <= 1500:
                    grid_row += f"{'':<8}"
                    continue
                d = self.grid_data[h].get(ft)
                if d:
                    temp_c = int(round(d['temp']))
                    wspd = int(round(d['wspd']))
                    wdir_rounded = int(round(d['wdir'] / 10.0))

                    if wspd < 5: wind_str = "9900"
                    elif 100 <= wspd <= 199: wind_str = f"{(wdir_rounded + 50):02d}{(wspd - 100):02d}"
                    elif wspd >= 200: wind_str = f"{(wdir_rounded + 50):02d}99"
                    else: wind_str = f"{wdir_rounded:02d}{wspd:02d}"

                    t_str = f"{temp_c:+03d}" if ft <= 24000 else f"{abs(temp_c):02d}"
                    if ft == 3000: val_str = f"{wind_str}"
                    else: val_str = f"{wind_str}{t_str}"
                    grid_row += f"{val_str:<8}"
                else:
                    grid_row += f"{'N/A':<8}"
            self.taf_groups.append(grid_row.rstrip())

        return "\n".join(self.taf_groups)

# --- METEOGRAM PLOTTING ENGINE ---
def generate_meteogram(processed_taf, valid_start_time):
    if not processed_taf: return ""
    hours = [d['fhour'] for d in processed_taf]
    times = [valid_start_time + datetime.timedelta(hours=h - hours[0]) for h in hours]
    time_labels = [t.strftime('%d/%H:%Mz') for t in times]

    cbh_plot = []
    for d in processed_taf:
        c_val = d['cbh']
        t_val = float(d['tcc'])
        is_conv = d['is_conv']
        prate = d['prate']

        if c_val > 30000 or c_val < 100 or np.isnan(c_val): c_val = 2500
        if t_val < 10 and not is_conv and prate <= 0: cbh_plot.append(12000)
        else:
            if c_val >= 12000: cbh_plot.append(12000)
            else: cbh_plot.append(c_val)

    vis = [float(d['vis']) / 1609.34 for d in processed_taf]
    vis = [v if v < 10 else 10 for v in vis]

    vis_colors = []
    for v in vis:
        if v < 1.0: vis_colors.append('purple')
        elif v < 3.0: vis_colors.append('red')
        elif v <= 5.0: vis_colors.append('blue')
        else: vis_colors.append('green')

    wspd = [d['wspd'] for d in processed_taf]
    gust = [d['gust'] if d['gust'] > d['wspd'] else np.nan for d in processed_taf]

    # NEW: Precipitation Array (converted from kg/m^2/s to inches/hour)
    prates_in_hr = [d['prate'] * 3600 / 25.4 for d in processed_taf]
    is_conv_list = [d['is_conv'] for d in processed_taf]
    precip_colors = ['red' if c else '#1f77b4' for c in is_conv_list]

    # Updated to 4 subplots
    fig = Figure(figsize=(11, 10))
    ax1, ax2, ax3, ax4 = fig.subplots(4, 1, sharex=True)
    fig.tight_layout(pad=3.0)

    # Plot 1: Ceilings
    ax1.plot(time_labels, cbh_plot, marker='o', color='#7f7f7f', linewidth=2)
    ax1.axhline(500, color='purple', linestyle='--', alpha=0.5, label='LIFR (500ft)')
    ax1.axhline(1000, color='red', linestyle='--', alpha=0.5, label='IFR (1000ft)')
    ax1.axhline(3000, color='blue', linestyle='--', alpha=0.5, label='MVFR (3000ft)')
    ax1.set_ylabel('Ceiling (ft AGL)')
    ax1.set_ylim(bottom=0, top=12000)
    ax1.margins(y=0)
    ax1.set_yticks(np.arange(0, 12001, 2500))
    ax1.set_title('Lowest Cloud Base Height')
    ax1.legend(loc='upper right')
    ax1.grid(True, linestyle=':', alpha=0.6)

    # Plot 2: Visibility
    ax2.bar(time_labels, vis, color=vis_colors, alpha=0.7)
    ax2.axhline(1, color='purple', linestyle='--', alpha=0.5, label='LIFR (1SM)')
    ax2.axhline(3, color='red', linestyle='--', alpha=0.5, label='IFR (3SM)')
    ax2.axhline(5, color='blue', linestyle='--', alpha=0.5, label='MVFR (5SM)')
    ax2.set_ylabel('Visibility (SM)')
    ax2.set_ylim(0, 10.5)
    ax2.set_title('Surface Visibility')
    ax2.legend(loc='upper right')
    ax2.grid(True, linestyle=':', alpha=0.6)

    # Plot 3: Winds
    ax3.plot(time_labels, wspd, marker='o', color='#2ca02c', linewidth=2, label='Sustained Speed')
    ax3.plot(time_labels, gust, marker='x', color='#d62728', linestyle='', markersize=8, label='Gusts')
    ax3.set_ylabel('Speed (KT)')
    ax3.set_title('Surface Winds & Convective Gusts')
    ax3.legend(loc='upper right')
    ax3.grid(True, linestyle=':', alpha=0.6)

    # Plot 4: Precipitation & Convection (NEW)
    ax4.bar(time_labels, prates_in_hr, color=precip_colors, alpha=0.7)
    ax4.set_ylabel('Precip (in/hr)')
    ax4.set_title('Precipitation Rate')

    # Dynamic scaling for precipitation
    max_p = max(prates_in_hr) if prates_in_hr else 0
    ax4.set_ylim(bottom=0, top=(max_p * 1.2) if max_p > 0 else 0.1)
    ax4.grid(True, linestyle=':', alpha=0.6)

    # Legend for Precip Types
    blue_patch = mpatches.Patch(color='#1f77b4', alpha=0.7, label='Stratiform Rain (RA)')
    red_patch = mpatches.Patch(color='red', alpha=0.7, label='Convective (TSRA)')
    ax4.legend(handles=[blue_patch, red_patch], loc='upper right')

    ax4.tick_params(axis='x', rotation=45)

    img = io.BytesIO()
    fig.savefig(img, format='png', bbox_inches='tight', dpi=100)
    img.seek(0)
    encoded = base64.b64encode(img.getvalue()).decode()
    return f"<h3>Forecaster Meteogram</h3><img src='data:image/png;base64,{encoded}' style='width:100%; max-width:900px; border:1px solid #ccc; box-shadow: 0 2px 4px rgba(0,0,0,0.1);'/>"

# --- CORE DATA FETCHER ---
def get_best_model(lat, lon):
    if 24.5 <= lat <= 50.0 and -125.0 <= lon <= -67.0: return "HRRR"
    return "GFS"

def get_latest_complete_run(model, max_fhour, session):
    now = datetime.datetime.now(datetime.timezone.utc)
    cycles = ['18', '12', '06', '00'] if model == "GFS" else [f"{h:02d}" for h in range(23, -1, -1)]
    url_template = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{fhour}.idx" if model == "GFS" else "https://storage.googleapis.com/high-resolution-rapid-refresh/hrrr.{date}/conus/hrrr.t{cycle}z.wrfprsf{fhour}.grib2.idx"

    for day_offset in range(3):
        date_str = (now - datetime.timedelta(days=day_offset)).strftime('%Y%m%d')
        for cycle in cycles:
            if day_offset == 0 and int(cycle) > now.hour: continue
            try:
                if session.head(url_template.format(date=date_str, cycle=cycle, fhour=max_fhour), timeout=5).status_code == 200:
                    return date_str, cycle
            except: continue
    return None, None

def get_model_variable(target_lat, target_lon, date_str, cycle, fhour, search_strings, model, session, idx_cache):
    warnings.filterwarnings("ignore")
    if isinstance(search_strings, str): search_strings = [search_strings]
    is_upper_air = any("mb:" in s for s in search_strings)

    if model == "HRRR":
        file_type = "wrfprs" if is_upper_air else "wrfsfc"
        base_url = f"https://storage.googleapis.com/high-resolution-rapid-refresh/hrrr.{date_str}/conus/hrrr.t{cycle}z.{file_type}f{fhour}.grib2"
    else:
        base_url = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{date_str}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{fhour}"

    idx_url = f"{base_url}.idx"
    lines = idx_cache.get(idx_url, [])
    if not lines: return None

    start_byte, end_byte = None, ''
    for i, line in enumerate(lines):
        if any(s in line for s in search_strings):
            start_byte = int(line.split(':')[1])
            for j in range(i + 1, len(lines)):
                next_byte = int(lines[j].split(':')[1])
                if next_byte > start_byte:
                    end_byte = next_byte - 1
                    break
            break

    if start_byte is not None:
        grib = None
        for attempt in range(3):
            try:
                r = session.get(base_url, headers={"Range": f"bytes={start_byte}-{end_byte}"}, timeout=15)
                if r.status_code == 206:
                    if end_byte != '':
                        expected_len = int(end_byte) - start_byte + 1
                        if len(r.content) < expected_len:
                            time.sleep(1)
                            continue
                    grib = r
                    break
            except: pass
            time.sleep(1)

        if not grib: return None
        tmp = f"/tmp/atomic_{uuid.uuid4().hex}.grib2"
        os.makedirs("/tmp", exist_ok=True)
        try:
            with open(tmp, "wb") as f:
                f.write(grib.content)
                f.flush()
                os.fsync(f.fileno())
            try:
                ds = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={'indexpath': ''})
                lat_key = next((k for k in ['latitude', 'lat'] if k in ds.coords), None)
                lon_key = next((k for k in ['longitude', 'lon'] if k in ds.coords), None)

                if lat_key and lon_key and len(ds.data_vars) > 0:
                    if ds[lat_key].ndim == 2:
                        lat_arr = ds[lat_key].values
                        lon_arr = ds[lon_key].values
                        lon_arr_180 = (lon_arr + 180) % 360 - 180
                        target_lon_180 = (target_lon + 180) % 360 - 180
                        dlon = np.abs(lon_arr_180 - target_lon_180)
                        dlon = np.minimum(dlon, 360.0 - dlon)
                        dlat = lat_arr - target_lat
                        dist_sq = dlat**2 + dlon**2
                        y_idx, x_idx = np.unravel_index(np.argmin(dist_sq), dist_sq.shape)
                        y_dim, x_dim = ds[lat_key].dims
                        point = ds.isel({y_dim: y_idx, x_dim: x_idx}).load()
                    else:
                        target_lon_360 = target_lon % 360
                        point = ds.sel({lat_key: target_lat, lon_key: target_lon_360}, method="nearest").load()
                    ds.close()
                    return {'data': point, 'cycle': f"{date_str} {cycle}Z", 'model': model}
                ds.close()
            except Exception as e:
                print(f"Data Corrupted: Skipping -> {str(e)}")
                return None
        finally:
            time.sleep(0.05)
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass
    return None

# --- FLASK APP ---
app = Flask(__name__)

# SECRET_KEY signs the session cookie. Must be set (and IDENTICAL across all
# gunicorn workers/restarts) or users will get randomly logged out every time
# a different worker handles their request. Set this in Railway's env vars.
app.secret_key = os.environ.get('SECRET_KEY')
APP_PASSWORD = os.environ.get('APP_PASSWORD')

LOGIN_FORM = """
<!DOCTYPE html><html>
<head><title>AutoSpot Login</title>{{ css|safe }}</head>
<body>
<div class='container'>
    <h2>NWSI 10-813 Spot Forecast Generator</h2>
    {% if error %}<p style='color:#dc3545; font-weight:bold;'>{{ error }}</p>{% endif %}
    <form action='/login' method='post'>
        <label>Password:</label>
        <input type='password' name='password' required autofocus>
        <button type='submit'>Sign In</button>
    </form>
</div>
</body></html>
"""

def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    return wrapped

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not APP_PASSWORD or not app.secret_key:
        return "Server misconfigured: APP_PASSWORD and SECRET_KEY environment variables must both be set.", 500
    error = None
    if request.method == 'POST':
        entered = request.form.get('password', '')
        # hmac.compare_digest avoids timing attacks that could leak the
        # password one character at a time via response-time differences.
        if hmac.compare_digest(entered, APP_PASSWORD):
            session['authenticated'] = True
            return redirect(url_for('index'))
        error = "Incorrect password."
    return render_template_string(LOGIN_FORM, css=REPORT_CSS, error=error)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(HTML_FORM, css=REPORT_CSS)

@app.route('/submit', methods=['POST'])
@login_required
def submit():
    try:
        lat = float(request.form.get('latitude', 39.1))
        lon = float(request.form.get('longitude', -94.6))
        sar_name = request.form.get('sar_name', 'AUTO-SPOT')
        selected_model = request.form.get('model_choice', 'AUTO')
        duration = int(request.form.get('duration', 24))

        user_date_str = request.form.get('start_date')
        user_hour_str = request.form.get('start_hour')
        try:
            if user_date_str and user_hour_str:
                iso_string = f"{user_date_str}T{user_hour_str}:00:00+00:00"
                taf_start_time = datetime.datetime.fromisoformat(iso_string)
            else:
                taf_start_time = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
        except:
            taf_start_time = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)

        active_model = get_best_model(lat, lon) if selected_model == 'AUTO' else selected_model
        lon_gfs = 360 + lon if lon < 0 else lon
        target_lon = lon_gfs if active_model == 'GFS' else lon
        pad = 3 if active_model == 'GFS' else 2

        results = {}
        with requests.Session() as session:
            retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
            session.mount("https://", HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=retry))

            sentry_hour = min(duration, 18) if active_model == 'HRRR' else duration
            run_date, run_cycle = get_latest_complete_run(active_model, f"{sentry_hour:0{pad}d}", session)
            if not run_date: return f"<html><body><h3>Error: Upstream synchronization in progress. Please try again.</h3></body></html>", 500

            model_init_time = datetime.datetime.strptime(f"{run_date}{run_cycle}", "%Y%m%d%H").replace(tzinfo=datetime.timezone.utc)
            gfs_sentry_hour = duration + 24
            gfs_run_date, gfs_run_cycle = get_latest_complete_run('GFS', f"{gfs_sentry_hour:03d}", session)
            if gfs_run_date:
                gfs_init_time = datetime.datetime.strptime(f"{gfs_run_date}{gfs_run_cycle}", "%Y%m%d%H").replace(tzinfo=datetime.timezone.utc)
            else:
                gfs_init_time = model_init_time
                gfs_run_date, gfs_run_cycle = run_date, run_cycle

            hour_offset = max(0, int((taf_start_time - model_init_time).total_seconds() / 3600))
            taf_hours = [hour_offset + i for i in range(0, duration + 1, 3)]
            fd_hours = [hour_offset + i for i in range(0, duration + 1, 6)]

            tasks = []
            for h in taf_hours:
                valid_time = model_init_time + datetime.timedelta(hours=h)
                model_route = 'GFS' if h > 18 and active_model == 'HRRR' else active_model

                if model_route == 'GFS':
                    task_date, task_cycle = gfs_run_date, gfs_run_cycle
                    task_fh = f"{int((valid_time - gfs_init_time).total_seconds() / 3600):03d}"
                    task_lon = lon_gfs
                else:
                    task_date, task_cycle = run_date, run_cycle
                    task_fh = f"{h:02d}"
                    task_lon = target_lon

                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":UGRD:10 m" if model_route != 'GFS' else ":UGRD:10 m above ground:", f"u_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":VGRD:10 m" if model_route != 'GFS' else ":VGRD:10 m above ground:", f"v_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":GUST:surface:", f"gust_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":VIS:surface:", f"vis_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":PRATE:surface:", f"prate_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":CPRAT:surface:" if model_route != 'GFS' else ":ACPCP:surface:", f"cprat_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":TCDC:entire atmosphere:", f"tcc_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":HGT:cloud base:", f"cbh_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, ":HGT:surface:", f"hgt_sfc_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, [":LCDC:low cloud layer:", ":TCDC:low cloud layer:"], f"lcc_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, [":MCDC:middle cloud layer:", ":TCDC:middle cloud layer:"], f"mcc_{h}", model_route))
                tasks.append((lat, task_lon, task_date, task_cycle, task_fh, [":HCDC:high cloud layer:", ":TCDC:high cloud layer:"], f"hcc_{h}", model_route))

            grid_lvls = {3000:900, 6000:800, 9000:700, 12000:600, 18000:500, 24000:400, 30000:300, 34000:250, 39000:200}
            for h in fd_hours:
                valid_time = model_init_time + datetime.timedelta(hours=h)
                model_route = 'GFS' if h > 18 and active_model == 'HRRR' else active_model
                if model_route == 'GFS':
                    task_date, task_cycle = gfs_run_date, gfs_run_cycle
                    task_fh = f"{int((valid_time - gfs_init_time).total_seconds() / 3600):03d}"
                    task_lon = lon_gfs
                else:
                    task_date, task_cycle = run_date, run_cycle
                    task_fh = f"{h:02d}"
                    task_lon = target_lon

                for ft, mb in grid_lvls.items():
                    tasks.append((lat, task_lon, task_date, task_cycle, task_fh, f":TMP:{mb} mb:", f"t_{h}_{ft}", model_route))
                    tasks.append((lat, task_lon, task_date, task_cycle, task_fh, f":UGRD:{mb} mb:", f"u_{h}_{ft}", model_route))
                    tasks.append((lat, task_lon, task_date, task_cycle, task_fh, f":VGRD:{mb} mb:", f"v_{h}_{ft}", model_route))

            idx_cache = {}
            def prefetch(url):
                try:
                    for _ in range(2):
                        r = session.get(url, timeout=10)
                        if r.status_code == 200: return url, r.text.splitlines()
                        time.sleep(0.5)
                except: pass
                return url, []

            urls_to_fetch = set()
            for task in tasks:
                search_strings, model_route, task_date, task_cycle, task_fh = task[5], task[7], task[2], task[3], task[4]
                if isinstance(search_strings, str): search_strings = [search_strings]
                is_upper_air = any("mb:" in s for s in search_strings)
                if model_route == "HRRR":
                    file_type = "wrfprs" if is_upper_air else "wrfsfc"
                    base_url = f"https://storage.googleapis.com/high-resolution-rapid-refresh/hrrr.{task_date}/conus/hrrr.t{task_cycle}z.{file_type}f{task_fh}.grib2"
                else:
                    base_url = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{task_date}/{task_cycle}/atmos/gfs.t{task_cycle}z.pgrb2.0p25.f{task_fh}"
                urls_to_fetch.add(f"{base_url}.idx")

            with ThreadPoolExecutor(max_workers=10) as ex:
                for url, lines in ex.map(prefetch, urls_to_fetch): idx_cache[url] = lines

            def worker(args):
                return args[6], get_model_variable(args[0], args[1], args[2], args[3], args[4], args[5], args[7], session, idx_cache)

            with ThreadPoolExecutor(max_workers=35) as executor:
                for k, r in executor.map(worker, tasks):
                    if r: results[k] = r

        true_elev_user = get_usgs_elevation(lat, lon)
        final_true_elev = 0

        processed_taf = []
        for h in taf_hours:
            u_r, v_r, tcc_r, vis_r = results.get(f"u_{h}"), results.get(f"v_{h}"), results.get(f"tcc_{h}"), results.get(f"vis_{h}")
            if all([u_r, v_r, tcc_r, vis_r]):
                u = list(u_r['data'].data_vars.values())[0].item()
                v = list(v_r['data'].data_vars.values())[0].item()
                tcc = list(tcc_r['data'].data_vars.values())[0].item()
                vis = list(vis_r['data'].data_vars.values())[0].item()
                spd = int(np.hypot(u, v) * 1.94384)
                dr = int((np.degrees(np.arctan2(u, v)) + 180 + 360) % 360)
                gust_r = results.get(f"gust_{h}")
                gust = (list(gust_r['data'].data_vars.values())[0].item() * 1.94384) if gust_r else 0
                prate_r, cprat_r = results.get(f"prate_{h}"), results.get(f"cprat_{h}")
                prate = list(prate_r['data'].data_vars.values())[0].item() if prate_r else 0
                is_conv = True if (cprat_r and list(cprat_r['data'].data_vars.values())[0].item() > 0) else False

                hgt_sfc_r = results.get(f"hgt_sfc_{h}")
                model_elev_ft = (list(hgt_sfc_r['data'].data_vars.values())[0].item() * 3.28084) if hgt_sfc_r else 0
                active_true_elev = true_elev_user if true_elev_user is not None else model_elev_ft
                final_true_elev = max(final_true_elev, active_true_elev)

                delta_ft = active_true_elev - model_elev_ft
                cbh_r = results.get(f"cbh_{h}")
                cbh = (list(cbh_r['data'].data_vars.values())[0].item() * 3.28084) if cbh_r else 99999
                adj_cbh = cbh - delta_ft

                if adj_cbh < 100 and tcc >= 50:
                    adj_cbh = 100
                    vis = min(vis, 400)
                elif adj_cbh < 100:
                    adj_cbh = 100

                lcc_r, mcc_r, hcc_r = results.get(f"lcc_{h}"), results.get(f"mcc_{h}"), results.get(f"hcc_{h}")
                lcc = list(lcc_r['data'].data_vars.values())[0].item() if lcc_r else 0
                mcc = list(mcc_r['data'].data_vars.values())[0].item() if mcc_r else 0
                hcc = list(hcc_r['data'].data_vars.values())[0].item() if hcc_r else 0

                processed_taf.append({
                    'fhour': h, 'wdir': dr, 'wspd': spd, 'gust': gust,
                    'vis': vis, 'prate': prate, 'is_conv': is_conv,
                    'tcc': tcc, 'cbh': adj_cbh, 'lcc': lcc, 'mcc': mcc, 'hcc': hcc
                })

        grid_res = {h: {} for h in fd_hours}
        for h in fd_hours:
            for ft in grid_lvls:
                tr, ur, vr = results.get(f"t_{h}_{ft}"), results.get(f"u_{h}_{ft}"), results.get(f"v_{h}_{ft}")
                if tr and ur and vr:
                    t = list(tr['data'].data_vars.values())[0].item() - 273.15
                    u = list(ur['data'].data_vars.values())[0].item()
                    v = list(vr['data'].data_vars.values())[0].item()
                    grid_res[h][ft] = {'temp': t, 'wspd': np.hypot(u,v)*1.94, 'wdir': (np.degrees(np.arctan2(u,v))+180)%360}

        cycle_label = f"{active_model} {run_date} {run_cycle}Z"
        if active_model == 'HRRR' and (taf_hours[-1] > 18):
            cycle_label = f"HRRR/GFS BLEND {run_date} {run_cycle}Z"

        taf_engine = FirstGuessTAF(sar_name, lat, lon, processed_taf, grid_res, cycle_label, duration, final_true_elev)
        final_report = taf_engine.generate_report(taf_start_time)
        meteogram_html = generate_meteogram(processed_taf, taf_start_time)

        return f"""
        <html><head>{REPORT_CSS}</head><body>
            <div class='report-wrap'>
                <h3>Forecaster Review & Edit</h3>
                <form action='/transmit' method='post'>
                    <textarea name='taf_content' class='taf-editor' rows='15'>{final_report}</textarea>
                    <button type='submit' class='btn-transmit'>Approve & Save Report</button>
                </form>
                {meteogram_html}
            </div>
        </body></html>
        """
    except Exception as e: return f"Server Error: {str(e)}", 500

@app.route('/transmit', methods=['POST'])
@login_required
def transmit():
    final_taf = request.form.get('taf_content', '')

    escaped_taf = base64.b64encode(final_taf.encode('utf-8')).decode('utf-8')

    return f"""
    <html>
    <head>{REPORT_CSS}</head>
    <body>
        <div class='container'>
            <h2>Transmission Status</h2>
            <p style='color: #28a745; font-weight: bold;'>Report finalized and cleared by Forecaster.</p>
            <p style='color: #007bff; font-weight: bold;'>⬇️ Local hard drive download initiated automatically.</p>
            <hr>
            <pre style='background: #f8f9fa; padding: 15px; border: 1px solid #ccc;'>{final_taf}</pre>
            <a href='/'><button style='background: #6c757d;'>Create Another Spot Forecast</button></a>
        </div>

        <script>
            var base64Data = "{escaped_taf}";
            var decodedText = atob(base64Data);
            var blob = new Blob([decodedText], {{type: "text/plain;charset=utf-8"}});

            var downloadLink = document.createElement("a");
            downloadLink.href = URL.createObjectURL(blob);
            downloadLink.download = "SPOT_TAF_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%Z')}.txt";

            document.body.appendChild(downloadLink);
            downloadLink.click();
            document.body.removeChild(downloadLink);
        </script>
    </body>
    </html>
    """

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    app.run(host='0.0.0.0', port=port)

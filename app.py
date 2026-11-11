from flask import Flask, abort, request, render_template_string, session, redirect, url_for, send_file
import datetime
import numpy as np
import os
import re
import hmac
import secrets
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

from request_validation import RequestValidationError, parse_forecast_request, validate_report

# --- UI AND CSS LAYER ---
REPORT_CSS = """
<style>
    body { font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }
    .container { background-color: #fff; padding: 30px; border-radius: 8px; max-width: 700px; margin: auto; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
    .report-wrap { background: #fff; border: 1px solid #ccc; padding: 25px; max-width: 950px; margin: 20px auto; box-shadow: 0 2px 5px rgba(0,0,0,0.1); overflow-x: auto; }
    .model-guidance { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
    .model-guidance th, .model-guidance td { border: 1px solid #ccc; padding: 6px; text-align: left; }
    .model-guidance thead { background: #eef3f7; }
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
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <label>Model: GFS (Global 25km)</label>
        <input type='hidden' name='model_choice' value='GFS'>

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
        if vis_meters is None: return "[VISIBILITY REQUIRED]"
        vis_sm = float(vis_meters) / 1609.34
        if vis_sm > 6: return "P6SM"
        elif vis_sm == 6: return "6SM"
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
        if prate is None: return "[WEATHER REVIEW REQUIRED]"
        wx_string = ""
        intensity = "-" if 0 < prate < 0.0005 else ("+" if prate > 0.002 else "")
        if prate > 0: wx_string = f"{intensity}TSRA" if is_convective else f"{intensity}RA"
        if not wx_string and vis_string != "P6SM":
            return "[WEATHER REVIEW REQUIRED]"
        return wx_string if wx_string else "NSW"

    @staticmethod
    def evaluate_clouds(tcdc, ceiling_ft):
        # GFS supplies one ceiling height, not bases for each cloud-cover layer.
        # A near-zero total cloud fraction supports SKC even when GFS encodes its
        # no-ceiling sentinel as a very high geopotential height.
        if tcdc is not None and np.isfinite(tcdc) and 0 <= tcdc < 6.25:
            return "SKC"
        # Leave other sky conditions unresolved without a usable layer height.
        if ceiling_ft is None or not np.isfinite(ceiling_ft) or not 0 <= ceiling_ft <= 30000:
            return "[SKY REQUIRED]"
        if tcdc is None or not np.isfinite(tcdc) or tcdc < 50:
            return "[SKY REQUIRED]"
        height = max(100, round(ceiling_ft / 100) * 100)
        return f"BKN{int(height / 100):03d}"

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
            if base["ceiling"] is not None and current["ceiling"] is not None and (base["ceiling"] >= t) != (current["ceiling"] >= t):
                return True
        return False

    def process_hour(self, data):
        wind = AviationTranslator.format_wind(data['wdir'], data['wspd'], data['gust'])
        vis = AviationTranslator.extract_visibility(data['vis'])
        wx = AviationTranslator.extract_present_weather(data['prate'], data['is_conv'], vis)
        clouds = AviationTranslator.evaluate_clouds(data['tcc'], data['ceiling'])
        wx_str = f" {wx}" if wx != "NSW" else ""
        return f"{wind} {vis}{wx_str} {clouds}".strip()

    def generate_report(self, valid_start_time):
        if not self.sfc_data: return "Error: Model data retrieval failed or timed out."
        end_time = valid_start_time + datetime.timedelta(hours=self.duration)

        lat_dir = "N" if self.lat >= 0 else "S"
        lon_dir = "E" if self.lon >= 0 else "W"
        elev_str = f"{int(self.true_elev_ft)}FT MSL" if self.true_elev_ft is not None else "UNKNOWN"
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
                if self.true_elev_ft is None or ft - self.true_elev_ft <= 1500:
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

    ceiling_plot = []
    for d in processed_taf:
        c_val = d['ceiling']

        if c_val is None:
            ceiling_plot.append(np.nan)
            continue
        ceiling_plot.append(min(c_val, 12000) if np.isfinite(c_val) and c_val >= 0 else np.nan)

    vis = [min(float(d['vis']) / 1609.34, 10) if d['vis'] is not None else np.nan for d in processed_taf]

    vis_colors = []
    for v in vis:
        if v < 1.0: vis_colors.append('purple')
        elif v < 3.0: vis_colors.append('red')
        elif v <= 5.0: vis_colors.append('blue')
        else: vis_colors.append('green')

    wspd = [d['wspd'] for d in processed_taf]
    gust = [d['gust'] if d['gust'] > d['wspd'] else np.nan for d in processed_taf]

    # NEW: Precipitation Array (converted from kg/m^2/s to inches/hour)
    prates_in_hr = [d['prate'] * 3600 / 25.4 if d['prate'] is not None else np.nan for d in processed_taf]
    is_conv_list = [d['is_conv'] for d in processed_taf]
    precip_colors = ['red' if c else '#1f77b4' for c in is_conv_list]

    # Updated to 4 subplots
    fig = Figure(figsize=(11, 10))
    ax1, ax2, ax3, ax4 = fig.subplots(4, 1, sharex=True)
    fig.tight_layout(pad=3.0)

    # Plot 1: Ceilings
    ax1.plot(time_labels, ceiling_plot, marker='o', color='#7f7f7f', linewidth=2)
    ax1.axhline(500, color='purple', linestyle='--', alpha=0.5, label='LIFR (500ft)')
    ax1.axhline(1000, color='red', linestyle='--', alpha=0.5, label='IFR (1000ft)')
    ax1.axhline(3000, color='blue', linestyle='--', alpha=0.5, label='MVFR (3000ft)')
    ax1.set_ylabel('Ceiling (ft AGL)')
    ax1.set_ylim(bottom=0, top=12000)
    ax1.margins(y=0)
    ax1.set_yticks(np.arange(0, 12001, 2500))
    ax1.set_title('GFS Cloud Ceiling')
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
    max_p = max((p for p in prates_in_hr if np.isfinite(p)), default=0)
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
    return encoded

# --- LEGACY GFS DATA FETCHER ---
GFS_FIELDS = {
    'u': ":UGRD:10 m above ground:",
    'v': ":VGRD:10 m above ground:",
    'gust': ":GUST:surface:",
    'vis': ":VIS:surface:",
    'prate': ":PRATE:surface:",
    'cprat': ":ACPCP:surface:",
    'tcc': ":TCDC:entire atmosphere:",
    'ceiling': ":HGT:cloud ceiling:",
    'hgt_sfc': ":HGT:surface:",
    'lcc': [":LCDC:low cloud layer:", ":TCDC:low cloud layer:"],
    'mcc': [":MCDC:middle cloud layer:", ":TCDC:middle cloud layer:"],
    'hcc': [":HCDC:high cloud layer:", ":TCDC:high cloud layer:"],
}
GFS_LEVELS = {3000:900, 6000:800, 9000:700, 12000:600, 18000:500, 24000:400, 30000:300, 34000:250, 39000:200}
GFS_PROFILE_FIELDS = {"t": "TMP", "u": "UGRD", "v": "VGRD"}


def build_gfs_urls(date_str, cycle, fhour):
    base = (f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{date_str}/{cycle}/"
            f"atmos/gfs.t{cycle}z.pgrb2.0p25.f{int(fhour):03d}")
    return base, f"{base}.idx"


def locate_bytes(records, field_spec):
    search_strings = [field_spec] if isinstance(field_spec, str) else field_spec
    for i, line in enumerate(records):
        if any(s in line for s in search_strings):
            start_byte = int(line.split(':')[1])
            for j in range(i + 1, len(records)):
                next_byte = int(records[j].split(':')[1])
                if next_byte > start_byte:
                    return start_byte, next_byte - 1
            return start_byte, ''
    return None, ''


def ceiling_agl_ft(ceiling_height_m, surface_height_m):
    """Convert GFS geopotential heights at ceiling and surface to feet AGL."""
    if ceiling_height_m is None or surface_height_m is None:
        return None
    if not np.isfinite(ceiling_height_m) or not np.isfinite(surface_height_m):
        return None
    height_m = ceiling_height_m - surface_height_m
    return height_m * 3.28084 if 0 <= height_m <= 9144 else None

def get_latest_gfs_run(start, end, session):
    now = datetime.datetime.now(datetime.timezone.utc)
    latest = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    for i in range(8):
        run = latest - datetime.timedelta(hours=6 * i)
        if run > start:
            continue
        first = int((start - run).total_seconds() / 3600)
        last = int((end - run).total_seconds() / 3600)
        try:
            if all(
                session.head(build_gfs_urls(run.strftime('%Y%m%d'), run.strftime('%H'), hour)[1], timeout=5).status_code == 200
                for hour in (first, last)
            ):
                return run, first
        except requests.RequestException:
            continue
    return None, None


def get_gfs_variable(target_lat, target_lon, date_str, cycle, fhour, field_spec, session, idx_cache):
    base_url, idx_url = build_gfs_urls(date_str, cycle, fhour)
    records = idx_cache.get(idx_url, [])
    if not records:
        return None

    start_byte, end_byte = locate_bytes(records, field_spec)
    if start_byte is None:
        return None

    try:
        with session.get(
            base_url,
            headers={"Range": f"bytes={start_byte}-{end_byte}", "Accept-Encoding": "identity"},
            stream=True,
            timeout=(4, 8),
        ) as response:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not match:
                return None
            first, last, size = map(int, match.groups())
            length = last - first + 1
            if (
                first != start_byte
                or last != (end_byte if end_byte != "" else size - 1)
                or last >= size
                or not 20 <= length <= 8 * 1024 * 1024
            ):
                return None
            message = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(message) + len(chunk) > length:
                    return None
                message.extend(chunk)
            if (
                len(message) != length
                or not message.startswith(b"GRIB")
                or not message.endswith(b"7777")
                or message[7] != 2
                or int.from_bytes(message[8:16], "big") != length
            ):
                return None
    except requests.RequestException:
        return None

    tmp = f"/tmp/atomic_{uuid.uuid4().hex}.grib2"
    try:
        with open(tmp, "wb") as stream:
            stream.write(message)
        with xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""}) as ds:
            if len(ds.data_vars) != 1 or "latitude" not in ds.coords or "longitude" not in ds.coords:
                return None
            if ds.latitude.ndim != 1 or ds.longitude.ndim != 1:
                return None
            longitude = np.asarray(ds.longitude)
            distances = np.abs((longitude - target_lon + 180) % 360 - 180)
            lon_index = int(np.argmin(distances))
            lat_index = int(np.argmin(np.abs(np.asarray(ds.latitude) - target_lat)))
            point = next(iter(ds.data_vars.values())).isel(latitude=lat_index, longitude=lon_index)
            value = float(point.item())
            return value if np.isfinite(value) else None
    except Exception as error:
        app.logger.warning("GFS point decode failed at step %s (%s)", fhour, type(error).__name__)
        return None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- FLASK APP ---
app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=128_000,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT_ID")),
)


@app.context_processor
def csrf_context():
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]
    return {"csrf_token": csrf_token}


@app.before_request
def check_csrf():
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.getlist("csrf_token")
        if not expected or len(supplied) != 1 or not hmac.compare_digest(expected.encode(), supplied[0].encode()):
            abort(400, "Form expired. Reload the page and try again.")


@app.get("/healthz")
def health():
    if not app.secret_key or not APP_PASSWORD:
        return {"status": "unavailable"}, 503
    return {"status": "ok"}


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
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
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
        if hmac.compare_digest(entered.encode(), APP_PASSWORD.encode()):
            session.clear()
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

def render_report(report, meteogram, guidance_note=None, model_rows=(), gfs_rows=()):
    return render_template_string(
        """<html><head>{{ css|safe }}</head><body>
        <div class="report-wrap">
            <h3>Forecaster Review &amp; Edit</h3>
            {% if guidance_note %}<p>{{ guidance_note }}</p>{% endif %}
            <form action="{{ url_for('transmit') }}" method="post">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <textarea name="taf_content" class="taf-editor" rows="15" required>{{ report }}</textarea>
                <button type="submit" class="btn-transmit">Approve &amp; Save Report</button>
            </form>
            {% if model_rows %}
            {% macro value(number, format="%.1f") -%}
              {{ format|format(number) if number is not none else "—" }}
            {%- endmacro %}
            <h3>ECMWF guidance by valid time</h3>
            <p>Gusts are maxima over the indicated preceding interval. Precipitation is instantaneous. Cloud cover gives no cloud-base height.</p>
            <table class="model-guidance">
                <thead><tr>
                    <th scope="col">Valid time (UTC)</th><th scope="col">10 m wind</th>
                    <th scope="col">Gust (kt) / source interval (UTC)</th>
                    <th scope="col">Precipitation type / mm/h</th>
                    <th scope="col">Cloud cover %<br>Total / low / mid / high</th>
                    <th scope="col">2 m temp / dewpoint °C</th>
                </tr></thead>
                <tbody>{% for row in model_rows %}<tr>
                    <th scope="row">{{ row.valid_utc }}{% if row.support_only %} (support only){% endif %}</th>
                    <td>{{ row.wind or "—" }}</td>
                    <td>{{ value(row.gust) }}{% if row.gust is not none and row.gust_interval[0] and row.gust_interval[1] %}
                        <br>{{ row.gust_interval[0] }} to {{ row.gust_interval[1] }}{% endif %}</td>
                    <td>{{ row.phase or "Type unavailable" }} / {{ value(row.rate, "%.3g") }}
                        {% if row.rain_candidate %}<br>Rain-rate candidate {{ row.rain_candidate }}{% endif %}</td>
                    <td>{{ value(row.cloud) }}{% for fraction in row.layer_cloud %} / {{ value(fraction) }}{% endfor %}</td>
                    <td>{{ value(row.temperature_c[0]) }} / {{ value(row.temperature_c[1]) }}</td>
                </tr>{% endfor %}</tbody>
            </table>
            {% endif %}
            {% if gfs_rows %}
            <h3>GFS guidance by valid time</h3>
            <p>Cloud cover is the fraction of each broad layer. The ceiling is one model height above ground, not the base of every layer.</p>
            <table class="model-guidance">
                <thead><tr><th scope="col">Valid time (UTC)</th>
                    <th scope="col">Visibility (SM)</th><th scope="col">Ceiling (ft AGL)</th>
                    <th scope="col">Cloud cover %<br>Total / low / mid / high</th>
                </tr></thead>
                <tbody>{% for row in gfs_rows %}<tr>
                    <th scope="row">{{ row.valid_utc }}</th>
                    <td>{{ row.visibility if row.visibility is not none else "—" }}</td>
                    <td>{% if row.ceiling is not none %}{{ "%.0f"|format(row.ceiling) }}{% elif row.clouds[0] is not none and 0 <= row.clouds[0] < 6.25 %}No ceiling (SKC){% else %}—{% endif %}</td>
                    <td>{% for cloud in row.clouds %}{{ "%.0f"|format(cloud) if cloud is not none else "—" }}{% if not loop.last %} / {% endif %}{% endfor %}</td>
                </tr>{% endfor %}</tbody>
            </table>
            {% endif %}
            {% if meteogram %}<h3>Forecaster Meteogram</h3><img src="data:image/png;base64,{{ meteogram }}" style="width:100%;max-width:900px" alt="Model forecast time series">{% endif %}
        </div></body></html>""",
        css=REPORT_CSS, report=report, meteogram=meteogram, guidance_note=guidance_note,
        model_rows=model_rows, gfs_rows=gfs_rows,
    )


@app.route('/submit', methods=['POST'])
@login_required
def submit():
    try:
        forecast = parse_forecast_request(request.form)
    except RequestValidationError as error:
        abort(400, str(error))

    if forecast.model != "GFS":
        abort(400, "Only GFS is available for this pilot.")
    return submit_gfs(forecast)


def submit_gfs(forecast):
    lat, lon, sar_name = forecast.latitude, forecast.longitude, forecast.label
    duration, taf_start_time = forecast.duration, forecast.start
    taf_end_time = taf_start_time + datetime.timedelta(hours=duration)
    target_lon = lon % 360

    try:
        results = {}
        with requests.Session() as session:
            retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
            session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retry))
            run, hour_offset = get_latest_gfs_run(taf_start_time, taf_end_time, session)
            if run is None:
                abort(502, "GFS guidance was unavailable for the requested UTC period.")
            run_date, run_cycle = run.strftime('%Y%m%d'), run.strftime('%H')
            taf_hours = list(range(hour_offset, hour_offset + duration, 3))
            fd_hours = list(range(hour_offset, hour_offset + duration, 6))

            tasks = []
            for h in taf_hours:
                for key, spec in GFS_FIELDS.items():
                    tasks.append((lat, target_lon, run_date, run_cycle, h, spec, f"{key}_{h}"))

            for h in fd_hours:
                for ft, mb in GFS_LEVELS.items():
                    for field in ('t', 'u', 'v'):
                        tasks.append((lat, target_lon, run_date, run_cycle, h, f":{GFS_PROFILE_FIELDS[field]}:{mb} mb:", f"{field}_{h}_{ft}"))

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
                _, idx_url = build_gfs_urls(task[2], task[3], task[4])
                urls_to_fetch.add(idx_url)

            with ThreadPoolExecutor(max_workers=10) as ex:
                for url, recs in ex.map(prefetch, urls_to_fetch): idx_cache[url] = recs

            def worker(args):
                lat_, lon_, date_, cyc_, fh_, spec_, key_ = args
                return key_, get_gfs_variable(lat_, lon_, date_, cyc_, fh_, spec_, session, idx_cache)

            with ThreadPoolExecutor(max_workers=8) as executor:
                for k, r in executor.map(worker, tasks):
                    if r is not None: results[k] = r

        true_elev_user = get_usgs_elevation(lat, lon)
        final_true_elev = None

        processed_taf = []
        for h in taf_hours:
            u_r, v_r, tcc_r, vis_r = results.get(f"u_{h}"), results.get(f"v_{h}"), results.get(f"tcc_{h}"), results.get(f"vis_{h}")
            if all(value is not None for value in (u_r, v_r, tcc_r)):
                u = u_r
                v = v_r
                tcc = tcc_r
                vis = vis_r
                spd = int(np.hypot(u, v) * 1.94384)
                dr = int((np.degrees(np.arctan2(u, v)) + 180 + 360) % 360)
                gust_r = results.get(f"gust_{h}")
                gust = (gust_r * 1.94384) if gust_r is not None else 0
                prate_r, cprat_r = results.get(f"prate_{h}"), results.get(f"cprat_{h}")
                prate = prate_r
                conv_val = cprat_r if cprat_r is not None else 0
                is_conv = conv_val > 0

                hgt_sfc_r = results.get(f"hgt_sfc_{h}")
                model_elev_ft = (hgt_sfc_r * 3.28084) if hgt_sfc_r is not None else None
                active_true_elev = true_elev_user if true_elev_user is not None else model_elev_ft
                if active_true_elev is not None and final_true_elev is None:
                    final_true_elev = active_true_elev

                ceiling_r = results.get(f"ceiling_{h}")
                ceiling_ft = ceiling_agl_ft(ceiling_r, hgt_sfc_r)

                lcc_r, mcc_r, hcc_r = results.get(f"lcc_{h}"), results.get(f"mcc_{h}"), results.get(f"hcc_{h}")
                lcc, mcc, hcc = lcc_r, mcc_r, hcc_r

                processed_taf.append({
                    'fhour': h, 'wdir': dr, 'wspd': spd, 'gust': gust,
                    'vis': vis, 'prate': prate, 'is_conv': is_conv,
                    'tcc': tcc, 'ceiling': ceiling_ft, 'lcc': lcc, 'mcc': mcc, 'hcc': hcc
                })

        grid_res = {h: {} for h in fd_hours}
        for h in fd_hours:
            for ft in GFS_LEVELS:
                tr, ur, vr = results.get(f"t_{h}_{ft}"), results.get(f"u_{h}_{ft}"), results.get(f"v_{h}_{ft}")
                if all(value is not None for value in (tr, ur, vr)):
                    t = tr - 273.15
                    u = ur
                    v = vr
                    grid_res[h][ft] = {'temp': t, 'wspd': np.hypot(u,v)*1.94, 'wdir': (np.degrees(np.arctan2(u,v))+180)%360}

        if not processed_taf or processed_taf[0]['fhour'] != hour_offset:
            abort(502, "GFS surface guidance was unavailable at the requested start.")
        cycle_label = f"GFS {run_date} {run_cycle}Z"
        engine = FirstGuessTAF(sar_name, lat, lon, processed_taf, grid_res, cycle_label, duration, final_true_elev)
        return render_report(
            engine.generate_report(taf_start_time),
            generate_meteogram(processed_taf, taf_start_time),
            gfs_rows=[{
                "valid_utc": (taf_start_time + datetime.timedelta(hours=row["fhour"] - hour_offset)).strftime("%Y-%m-%d %H:%MZ"),
                "visibility": round(row["vis"] / 1609.34, 1) if row["vis"] is not None else None,
                "ceiling": row["ceiling"],
                "clouds": (row["tcc"], row["lcc"], row["mcc"], row["hcc"]),
            } for row in processed_taf],
        )
    except (requests.RequestException, ValueError, OSError) as error:
        app.logger.warning("GFS request failed (%s)", type(error).__name__)
        abort(502, "GFS guidance could not be processed for this request.")


@app.route('/transmit', methods=['POST'])
@login_required
def transmit():
    try:
        text = validate_report(request.form)
    except RequestValidationError as error:
        abort(400, str(error))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%SZ')
    return send_file(
        io.BytesIO(text.encode("utf-8")), mimetype="text/plain", as_attachment=True,
        download_name=f"SPOT_TAF_{stamp}.txt", max_age=0,
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    app.run(host='0.0.0.0', port=port)

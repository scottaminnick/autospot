from flask import Blueprint, Flask, abort, current_app, g, request, render_template, session, redirect, url_for, send_file
import datetime
import json
import numpy as np
import os
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
    return encoded

# --- CORE DATA FETCHER ---
# Model routing. Both supported models are global and expose a single GRIB2
# file per forecast step (surface + pressure levels together) on public AWS
# buckets, fetched via HTTP byte-range requests. They differ in URL layout,
# index format, longitude convention, and step granularity - captured here.
MODEL_CYCLES = {
    'GFS': ['18', '12', '06', '00'],
    'ECMWF': ['18', '12', '06', '00'],
}
# Forecast step granularity (hours). GFS publishes hourly steps; ECMWF
# open-data oper is 3-hourly, so every requested step must be a multiple of 3.
MODEL_STEP_HOURS = {'GFS': 1, 'ECMWF': 3}

# Legacy surface selection. None means this adapter does not request the field;
# it does not establish model capability. See the Phase 0 matrix for the inputs
# PR 2 will add. Missing-value/weather interpretation is addressed in PR 4.
SFC_FIELDS = {
    'u':       {'GFS': ":UGRD:10 m above ground:",  'ECMWF': {'param': '10u',    'levtype': 'sfc'}},
    'v':       {'GFS': ":VGRD:10 m above ground:",  'ECMWF': {'param': '10v',    'levtype': 'sfc'}},
    'gust':    {'GFS': ":GUST:surface:",            'ECMWF': {'param': '10fg',   'levtype': 'sfc'}},
    'vis':     {'GFS': ":VIS:surface:",             'ECMWF': None},
    'prate':   {'GFS': ":PRATE:surface:",           'ECMWF': {'param': 'tprate', 'levtype': 'sfc'}},
    'cprat':   {'GFS': ":ACPCP:surface:",           'ECMWF': {'param': 'mucape', 'levtype': 'sfc'}},
    'tcc':     {'GFS': ":TCDC:entire atmosphere:",  'ECMWF': {'param': 'tcc',    'levtype': 'sfc'}},
    'cbh':     {'GFS': ":HGT:cloud base:",          'ECMWF': None},
    'hgt_sfc': {'GFS': ":HGT:surface:",             'ECMWF': None},
    'lcc':     {'GFS': [":LCDC:low cloud layer:", ":TCDC:low cloud layer:"],       'ECMWF': None},
    'mcc':     {'GFS': [":MCDC:middle cloud layer:", ":TCDC:middle cloud layer:"], 'ECMWF': None},
    'hcc':     {'GFS': [":HCDC:high cloud layer:", ":TCDC:high cloud layer:"],     'ECMWF': None},
}

# Winds-aloft pressure levels (ft -> mb). ECMWF open data has no 900/800 mb
# levels, so the two lowest use the nearest available (925/850 mb).
GRID_LVLS = {
    'GFS':   {3000:900, 6000:800, 9000:700, 12000:600, 18000:500, 24000:400, 30000:300, 34000:250, 39000:200},
    'ECMWF': {3000:925, 6000:850, 9000:700, 12000:600, 18000:500, 24000:400, 30000:300, 34000:250, 39000:200},
}

def aloft_spec(model, field, mb):
    """Field spec for a winds-aloft variable ('t'/'u'/'v') at a pressure level."""
    if model == 'ECMWF':
        return {'param': field, 'levtype': 'pl', 'levelist': str(mb)}
    return {'t': f":TMP:{mb} mb:", 'u': f":UGRD:{mb} mb:", 'v': f":VGRD:{mb} mb:"}[field]

def build_grib_urls(model, date_str, cycle, fhour):
    """Return (grib_url, index_url) for a model/cycle/forecast-hour."""
    if model == 'ECMWF':
        stem = (f"https://ecmwf-forecasts.s3.amazonaws.com/{date_str}/{cycle}z/ifs/"
                f"0p25/oper/{date_str}{cycle}0000-{int(fhour)}h-oper-fc")
        return f"{stem}.grib2", f"{stem}.index"
    base = (f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{date_str}/{cycle}/"
            f"atmos/gfs.t{cycle}z.pgrb2.0p25.f{int(fhour):03d}")
    return base, f"{base}.idx"

def parse_index(model, text):
    """Normalise an index file's text into a list of records for byte lookup.
    GFS -> list of raw wgrib2 '.idx' lines. ECMWF -> list of JSON dicts."""
    if model == 'ECMWF':
        recs = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try: recs.append(json.loads(line))
                except Exception: pass
        return recs
    return text.splitlines()

def locate_bytes(model, records, field_spec):
    """Find the (start_byte, end_byte) of a field within an index.
    GFS field_spec is a list of substrings; ECMWF is a dict of index keys."""
    if model == 'ECMWF':
        for rec in records:
            if all(str(rec.get(k)) == str(v) for k, v in field_spec.items()):
                start = int(rec['_offset'])
                return start, start + int(rec['_length']) - 1
        return None, ''
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

def get_latest_complete_run(model, max_fhour, session):
    now = datetime.datetime.now(datetime.timezone.utc)
    cycles = MODEL_CYCLES[model]

    for day_offset in range(3):
        date_str = (now - datetime.timedelta(days=day_offset)).strftime('%Y%m%d')
        for cycle in cycles:
            if day_offset == 0 and int(cycle) > now.hour: continue
            _, idx_url = build_grib_urls(model, date_str, cycle, max_fhour)
            try:
                if session.head(idx_url, timeout=5).status_code == 200:
                    return date_str, cycle
            except: continue
    return None, None

def get_model_variable(target_lat, target_lon, date_str, cycle, fhour, field_spec, model, session, idx_cache):
    base_url, idx_url = build_grib_urls(model, date_str, cycle, fhour)
    records = idx_cache.get(idx_url, [])
    if not records: return None

    start_byte, end_byte = locate_bytes(model, records, field_spec)

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
                        # Normalise the target longitude to the dataset's own
                        # convention: GFS uses 0..360, ECMWF uses -180..180.
                        lon_vals = ds[lon_key].values
                        if float(np.nanmax(lon_vals)) > 180.0:
                            tgt_lon = target_lon % 360
                        else:
                            tgt_lon = (target_lon + 180) % 360 - 180
                        point = ds.sel({lat_key: target_lat, lon_key: tgt_lon}, method="nearest").load()
                    ds.close()
                    return {'data': point, 'cycle': f"{date_str} {cycle}Z", 'model': model}
                ds.close()
            except Exception:
                # Per-field diagnostics and metadata validation arrive in PR 2.
                # Do not print upstream response/error text or suppress global warnings.
                return None
        finally:
            time.sleep(0.05)
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass
    return None

# --- WEB APPLICATION ---
from werkzeug.exceptions import HTTPException
import sqlite3
from request_validation import parse_forecast_request, check_work_budget, validate_report, RequestValidationError
from web_security import (install_security, activate_security, configured, credential_revision,
                          private_bucket, csrf_token, csrf_valid, rate_limit)

web = Blueprint("web", __name__)


class ForecastUnavailable(Exception):
    pass


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not g.auth_sid:
            return redirect(url_for("web.login"))
        return view_func(*args, **kwargs)
    return wrapped


def require_csrf():
    if not csrf_valid():
        abort(400, description="The form expired or could not be verified. Reload the page and try again.")


@web.route("/healthz")
def health():
    if not configured():
        return {"status": "unavailable"}, 503
    try:
        current_app.extensions["controls"].ready()
    except (sqlite3.Error, OSError):
        return {"status": "unavailable"}, 503
    return {"status": "ok"}


@web.route("/login", methods=["GET", "POST"])
def login():
    if not configured():
        abort(503, description="Application configuration is incomplete. Contact the maintainer.")
    error = None
    if request.method == "POST":
        require_csrf()
        rate_limit([("login_global", "all", 100, 900),
                    ("login_address", private_bucket(request.remote_addr or "unknown"), 20, 900)])
        passwords = request.form.getlist("password")
        entered = passwords[0] if len(passwords) == 1 else ""
        if len(entered.encode("utf-8")) <= 1024 and hmac.compare_digest(
                entered.encode("utf-8"), current_app.config["APP_PASSWORD"].encode("utf-8")):
            controls = current_app.extensions["controls"]
            if g.auth_sid:
                controls.revoke(g.auth_sid)
            session.clear()
            session.permanent = True
            session["sid"] = controls.issue(credential_revision(), 8 * 3600)
            csrf_token()
            return redirect(url_for("web.index"))
        error = "Incorrect password."
    return render_template("login.html", error=error), 401 if error else 200


@web.route("/logout", methods=["POST"])
@login_required
def logout():
    require_csrf()
    current_app.extensions["controls"].revoke(g.auth_sid)
    session.clear()
    return redirect(url_for("web.login"))


@web.route("/")
@login_required
def index():
    now = datetime.datetime.now(datetime.timezone.utc)
    return render_template("request.html", values={
        "model_choice": "ECMWF", "start_date": now.strftime("%Y-%m-%d"),
        "start_hour": now.strftime("%H"), "duration": "24", "sar_name": "AUTO-SPOT"})


@web.route("/submit", methods=["POST"])
@login_required
def submit():
    require_csrf()
    forecast = parse_forecast_request(request.form)
    check_work_budget(forecast.duration,
                      sum(fields[forecast.model] is not None for fields in SFC_FIELDS.values()),
                      len(GRID_LVLS[forecast.model]))
    rate_limit([("submit_global", "all", 20, 300),
                ("submit_session", g.auth_sid, 3, 300)])
    report, chart = generate_forecast(forecast)
    return render_template("review.html", report=report, chart=chart)


@web.route("/transmit", methods=["POST"])
@login_required
def transmit():
    require_csrf()
    final_taf = validate_report(request.form)
    rate_limit([("export_session", g.auth_sid, 30, 60)])
    # Direct attachment preserves the exact UTF-8 text and removes the HTML/JS sink.
    filename = f"SPOT_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%SZ')}.txt"
    return send_file(io.BytesIO(final_taf.encode("utf-8")), mimetype="text/plain",
                     as_attachment=True, download_name=filename, max_age=0)


def generate_forecast(forecast):
    lat, lon, sar_name = forecast.latitude, forecast.longitude, forecast.label
    duration, taf_start_time, active_model = forecast.duration, forecast.start, forecast.model
    step_gran = MODEL_STEP_HOURS[active_model]
    # GFS grid is 0..360; ECMWF is -180..180 (native signed longitude).
    target_lon = (360 + lon if lon < 0 else lon) if active_model == 'GFS' else lon
    grid_lvls = GRID_LVLS[active_model]

    results = {}
    with requests.Session() as session:
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=retry))

        # Sentry step confirms a run is published far enough out to cover the
        # whole request; nudge past the last needed step by one granule.
        sentry_hour = duration + step_gran
        run_date, run_cycle = get_latest_complete_run(active_model, sentry_hour, session)
        if not run_date:
            raise ForecastUnavailable("A suitable model run is not available. Try again later.")

        model_init_time = datetime.datetime.strptime(f"{run_date}{run_cycle}", "%Y%m%d%H").replace(tzinfo=datetime.timezone.utc)

        # Snap the start offset to the model's step granularity so every
        # requested forecast step actually exists (ECMWF is 3-hourly).
        raw_offset = max(0, (taf_start_time - model_init_time).total_seconds() / 3600)
        hour_offset = int(round(raw_offset / step_gran) * step_gran)
        taf_hours = [hour_offset + i for i in range(0, duration + 1, 3)]
        fd_hours = [hour_offset + i for i in range(0, duration + 1, 6)]

        tasks = []
        for h in taf_hours:
            for key, spec_map in SFC_FIELDS.items():
                spec = spec_map[active_model]
                if spec is None: continue
                tasks.append((lat, target_lon, run_date, run_cycle, h, spec, f"{key}_{h}"))

        for h in fd_hours:
            for ft, mb in grid_lvls.items():
                for field in ('t', 'u', 'v'):
                    tasks.append((lat, target_lon, run_date, run_cycle, h, aloft_spec(active_model, field, mb), f"{field}_{h}_{ft}"))

        idx_cache = {}
        def prefetch(url):
            try:
                for _ in range(2):
                    r = session.get(url, timeout=10)
                    if r.status_code == 200: return url, parse_index(active_model, r.text)
                    time.sleep(0.5)
            except: pass
            return url, []

        urls_to_fetch = set()
        for task in tasks:
            _, idx_url = build_grib_urls(active_model, task[2], task[3], task[4])
            urls_to_fetch.add(idx_url)

        with ThreadPoolExecutor(max_workers=10) as ex:
            for url, recs in ex.map(prefetch, urls_to_fetch): idx_cache[url] = recs

        def worker(args):
            lat_, lon_, date_, cyc_, fh_, spec_, key_ = args
            return key_, get_model_variable(lat_, lon_, date_, cyc_, fh_, spec_, active_model, session, idx_cache)

        with ThreadPoolExecutor(max_workers=35) as executor:
            for k, r in executor.map(worker, tasks):
                if r: results[k] = r

    if not results:
        raise ForecastUnavailable("Model data retrieval failed or timed out. Try again later.")

    true_elev_user = get_usgs_elevation(lat, lon)
    final_true_elev = 0

    # ECMWF reports total cloud cover as a 0-1 fraction; GFS as a percent.
    tcc_scale = 100.0 if active_model == 'ECMWF' else 1.0
    processed_taf = []
    for h in taf_hours:
        u_r, v_r, tcc_r, vis_r = results.get(f"u_{h}"), results.get(f"v_{h}"), results.get(f"tcc_{h}"), results.get(f"vis_{h}")
        if all([u_r, v_r, tcc_r]):
            u = list(u_r['data'].data_vars.values())[0].item()
            v = list(v_r['data'].data_vars.values())[0].item()
            tcc = list(tcc_r['data'].data_vars.values())[0].item() * tcc_scale
            # Visibility is absent from ECMWF open data; default to
            # unrestricted (>6SM) when the field is unavailable.
            vis = list(vis_r['data'].data_vars.values())[0].item() if vis_r else 9999.0
            spd = int(np.hypot(u, v) * 1.94384)
            dr = int((np.degrees(np.arctan2(u, v)) + 180 + 360) % 360)
            gust_r = results.get(f"gust_{h}")
            gust = (list(gust_r['data'].data_vars.values())[0].item() * 1.94384) if gust_r else 0
            prate_r, cprat_r = results.get(f"prate_{h}"), results.get(f"cprat_{h}")
            prate = list(prate_r['data'].data_vars.values())[0].item() if prate_r else 0
            # Convection flag: GFS keys off convective precip accumulation;
            # ECMWF has no such field, so most-unstable CAPE stands in.
            conv_val = list(cprat_r['data'].data_vars.values())[0].item() if cprat_r else 0
            if active_model == 'ECMWF':
                is_conv = conv_val >= 300 and prate > 0
            else:
                is_conv = conv_val > 0

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

    if not processed_taf:
        raise ForecastUnavailable("No usable surface forecast was retrieved. Try again later.")

    cycle_label = f"{active_model} {run_date} {run_cycle}Z"

    taf_engine = FirstGuessTAF(sar_name, lat, lon, processed_taf, grid_res, cycle_label, duration, final_true_elev)
    final_report = taf_engine.generate_report(taf_start_time)
    meteogram_html = generate_meteogram(processed_taf, taf_start_time)

    return final_report, meteogram_html


def create_app(test_config=None):
    application = Flask(__name__)
    install_security(application)
    if test_config:
        application.config.update(test_config)
    activate_security(application)
    application.register_blueprint(web)
    application.logger.setLevel("INFO")

    @application.errorhandler(RequestValidationError)
    def invalid_request(error):
        return render_template("error.html", message=str(error)), 400

    @application.errorhandler(ForecastUnavailable)
    def unavailable(error):
        return render_template("error.html", message=str(error)), 502

    @application.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: error.description, 401: "Sign in to continue.",
                    404: "Page not found.", 405: "This action requires the correct form submission.",
                    413: "The submitted form is too large.", 429: "Too many requests. Try again later.",
                    503: "The service is temporarily unavailable. Contact the maintainer if this continues."}
        response = error.get_response()
        response.set_data(render_template("error.html", message=messages.get(error.code, "The request could not be completed.")))
        response.content_type = "text/html; charset=utf-8"
        return response

    @application.errorhandler(Exception)
    def server_error(error):
        # Error strings, request bodies and credentials must never enter logs or HTML.
        application.logger.error(json.dumps({"event": "request_failed", "request_id": g.get("request_id"),
                                             "error_type": type(error).__name__}))
        code = 503 if isinstance(error, (sqlite3.Error, OSError)) else 500
        return render_template("error.html", message="The request could not be completed. Contact the maintainer with the reference below."), code

    return application


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5005))
    app.run(host="0.0.0.0", port=port)

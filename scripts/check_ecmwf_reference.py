"""Compare frozen IFS messages with GDAL's independent GRIB driver."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import shutil
import subprocess

import eccodes

from weather.fields import OROGRAPHY, SPECS, decode_message, pressure_spec


FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures"
PHASE0 = FIXTURES / "phase0/ecmwf-20261109/messages"
BAHAMAS = (24.71, -77.76)


def gdal(*command):
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout


def compare(path, spec, run, start_step, end_step, point, element, level, code=None):
    raw = path.read_bytes()
    result = decode_message(raw, spec, run, end_step, *point)
    provenance = result.provenance
    lat = provenance["sample_latitude"]
    lon = (provenance["sample_longitude"] + 180) % 360 - 180

    info = json.loads(gdal("gdalinfo", "-json", str(path)))
    (band,) = info["bands"]
    metadata = band["metadata"][""]
    expected = {
        "GRIB_ELEMENT": element,
        "GRIB_SHORT_NAME": level,
        "GRIB_REF_TIME": int(run.timestamp()),
        "GRIB_VALID_TIME": int((run + timedelta(hours=end_step)).timestamp()),
        "GRIB_FORECAST_SECONDS": start_step * 3600,
    }
    for key, value in expected.items():
        if metadata.get(key) != str(value):
            raise ValueError(f"{path.name}: GDAL {key} differs from {value}")
    if provenance["metadata"]["startStep"] != start_step:
        raise ValueError(f"{path.name}: AutoSpot interval start differs")

    x0, dx, rx, y0, ry, dy = info["geoTransform"]
    request_lat, request_lon = point
    x = math.floor(((request_lon - x0) % 360) / dx)
    y = math.floor((request_lat - y0) / dy)
    ref_lon, ref_lat = x0 + (x + 0.5) * dx, y0 + (y + 0.5) * dy
    if rx or ry or (ref_lat, ref_lon) != (lat, lon):
        raise ValueError(f"{path.name}: AutoSpot and GDAL select different grid points")
    reference = float(gdal("gdallocationinfo", "-valonly", str(path), str(x), str(y)))

    # One source quantization interval, in native units. Codes must match exactly.
    gid = eccodes.codes_new_from_message(raw)
    try:
        interval = math.ldexp(1.0, eccodes.codes_get(gid, "binaryScaleFactor"))
        interval *= 10 ** -eccodes.codes_get(gid, "decimalScaleFactor")
    finally:
        eccodes.codes_release(gid)
    tolerance = interval if code is None else 0
    difference = abs(reference - result.value)
    if (
        not math.isfinite(difference)
        or difference > tolerance
        or (code is not None and reference != code)
    ):
        raise ValueError(
            f"{path.name}: {result.value} vs GDAL {reference}; tolerance {tolerance}"
        )
    return {
        "fixture": path.name,
        "field": spec.name,
        "cycle_utc": run.isoformat(),
        "step_hours": [start_step, end_step],
        "grid_point": [lat, lon],
        "units": result.unit,
        "autospot": result.value,
        "gdal": reference,
        "abs_difference": difference,
        "tolerance": tolerance,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    if not all(shutil.which(program) for program in ("gdalinfo", "gdallocationinfo")):
        parser.error("Install GDAL CLI tools (gdal-bin on Debian/Ubuntu)")
    specs = {spec.param: spec for spec in SPECS}
    nov9 = datetime(2026, 11, 9, tzinfo=timezone.utc)
    rows = []
    for param, start, end, element, level in (
        ("10u", 12, 12, "UGRD", "10-HTGL"),
        ("10v", 12, 12, "VGRD", "10-HTGL"),
        ("10fg", 11, 12, "GUST", "10-HTGL"),
        ("z", 0, 0, "GP", "0-SFC"),
    ):
        path = PHASE0 / f"20261109-00-f{end:03d}-{param}-sfc.grib2"
        spec = OROGRAPHY if param == "z" else specs[param]
        rows.append(compare(path, spec, nov9, start, end, BAHAMAS, element, level))
    for param, pressure, element in (("u", 925, "UGRD"), ("v", 100, "VGRD")):
        path = FIXTURES / "pr3" / f"ifs-20261109-00-f012-{param}-{pressure}.grib2"
        spec = pressure_spec(param, pressure)
        rows.append(
            compare(
                path, spec, nov9, 12, 12, BAHAMAS, element, f"{pressure * 100}-ISBL"
            )
        )

    precipitation = FIXTURES / "pr4"
    samples = json.loads((precipitation / "nonzero-precipitation.json").read_text())
    run = datetime.fromisoformat(samples["cycle_utc"])
    step = samples["step"]
    for sample in samples["samples"]:
        point = (sample["latitude"], sample["longitude"])
        for param, element, code in (
            ("ptype", "PTYPE", sample["code"]),
            ("tprate", "TPRATE", None),
        ):
            path = precipitation / samples["sources"][param]["path"]
            rows.append(
                compare(
                    path, specs[param], run, step, step, point, element, "0-SFC", code
                )
            )

    result = json.dumps(
        {
            "reference_decoder": gdal("gdalinfo", "--version").strip(),
            "comparisons": rows,
        },
        indent=2,
    )
    if args.summary:
        args.summary.write_text(result + "\n")
    print(f"{len(rows)} frozen samples agree with GDAL within source packing precision")


if __name__ == "__main__":
    main()

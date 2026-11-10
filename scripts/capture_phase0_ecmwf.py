#!/usr/bin/env python3
"""Explicit, bounded research capture; never imported by the web application.

Run from the repository root with requests and eccodes installed. The dated
rolling archive may expire; committed snapshots remain the regression source.
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import eccodes as ec

PARAM_IDS = {"10u": 165, "10v": 166, "10fg": 49, "tcc": 164,
             "lcc": 186, "mcc": 187, "hcc": 188, "tprate": 260048,
             "ptype": 260015, "tp": 228, "cp": 143, "mucape": 228235,
             "2t": 167, "2d": 168, "skt": 235, "sp": 134, "z": 129,
             "u": 131, "v": 132, "t": 130, "gh": 156}

BASES = {
    "ecmwf": "https://data.ecmwf.int/forecasts",
    "aws": "https://ecmwf-forecasts.s3.amazonaws.com",
}
SURFACE = ["10u", "10v", "10fg", "tcc", "lcc", "mcc", "hcc", "tprate",
           "ptype", "tp", "cp", "mucape", "2t", "2d", "skt", "sp"]
KEYS = ["shortName", "paramId", "name", "units", "typeOfLevel", "level",
        "dataDate", "dataTime", "validityDate", "validityTime", "stepType",
        "startStep", "endStep", "stepUnits", "typeOfStatisticalProcessing",
        "gridType", "Ni", "Nj", "iDirectionIncrementInDegrees",
        "jDirectionIncrementInDegrees", "packingType", "bitsPerValue",
        "binaryScaleFactor", "decimalScaleFactor", "numberOfMissing"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def stem(provider, date, cycle, step):
    return f"{BASES[provider]}/{date}/{cycle:02d}z/ifs/0p25/oper/{date}{cycle:02d}0000-{step}h-oper-fc"


def get_index(provider, date, cycle, step, out, observations):
    url = stem(provider, date, cycle, step) + ".index"
    observation = {"provider": provider, "cycle": cycle, "step": step, "url": url,
                   "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        r = requests.get(url, timeout=(10, 25))
        observation.update(status=r.status_code, bytes=len(r.content))
        if r.status_code != 200:
            observation["error_excerpt"] = r.text[:180]
            return None
        records = [json.loads(line) for line in r.text.splitlines() if line]
        assert records and all(str(x["date"]) == date and int(x["time"]) == cycle * 100
                               for x in records)
        name = f"indexes/{provider}-{date}-{cycle:02d}-f{step:03d}.index"
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(r.content)
        observation.update(path=name, sha256=sha(r.content), records=len(records),
                           nonmatching_step_records=[x for x in records if int(x["step"]) != step],
                           surface_parameters=sorted({x["param"] for x in records if x["levtype"] == "sfc"}),
                           pressure_levels=sorted({int(x["levelist"]) for x in records if x["levtype"] == "pl"}))
        return records
    except (requests.RequestException, ValueError, AssertionError, KeyError) as exc:
        observation["error"] = f"{type(exc).__name__}: {exc}"
        return None
    finally:
        observations.append(observation)
        dump(out / "inventory-observations.json", observations)
        print(provider, cycle, step, observation.get("status", observation.get("error")), flush=True)


def capture_record(record, date, cycle, step, out, keep_raw, provider="ecmwf"):
    start, length = record["_offset"], record["_length"]
    end = start + length - 1
    assert int(record["step"]) == step
    url = stem(provider, date, cycle, step) + ".grib2"
    r = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=(10, 30))
    assert r.status_code == 206, (r.status_code, url)
    assert r.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/")
    assert len(r.content) == length
    assert r.content[:4] == b"GRIB" and r.content[-4:] == b"7777"
    assert int.from_bytes(r.content[8:16], "big") == length
    gid = ec.codes_new_from_message(r.content)
    try:
        meta = {key: ec.codes_get(gid, key) for key in KEYS if ec.codes_is_defined(gid, key)}
        assert meta["dataDate"] == int(date) and meta["dataTime"] == cycle * 100
        assert meta["endStep"] == step
        assert meta["paramId"] == PARAM_IDS[record["param"]]
        if record["levtype"] == "pl":
            assert meta["typeOfLevel"] == "isobaricInhPa"
            assert meta["level"] == int(record["levelist"])
        nearest = dict(ec.codes_grib_find_nearest(gid, 24.71, -77.76, npoints=1)[0])
        assert math.isfinite(nearest["value"])
        result = {"index_record": record, "metadata": meta, "nearest": nearest,
                  "source_url": url, "content_range": r.headers["Content-Range"],
                  "source_message_sha256": sha(r.content),
                  "retrieved_at": datetime.now(timezone.utc).isoformat()}
        if keep_raw:
            filename = f"messages/{date}-{cycle:02d}-f{step:03d}-{record['param']}-{record.get('levelist', 'sfc')}.grib2"
            (out / filename).parent.mkdir(parents=True, exist_ok=True)
            (out / filename).write_bytes(r.content)
            result["raw_path"] = filename
        return result
    finally:
        ec.codes_release(gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="20261109")
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory; refuses to replace frozen evidence")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new directory")
    args.output.mkdir(parents=True)
    run = datetime.strptime(args.date, "%Y%m%d").replace(tzinfo=timezone.utc)
    observations, indexes, samples, failures = [], {}, [], []
    # Full original interval plus cycle/cadence probes, no retries or fan-out.
    probes = [(0, s) for s in [0, 3, *range(12, 37, 3), 144, 150]]
    probes += [(c, s) for c in [6, 12, 18] for s in [0, 12, 144]]
    for cycle, step in probes:
        records = get_index("ecmwf", args.date, cycle, step, args.output, observations)
        if records:
            indexes[(cycle, step)] = records
    # A single AWS probe avoids amplifying the rate-limit failure under study.
    get_index("aws", args.date, 0, 12, args.output, observations)
    wanted = []
    for step in range(12, 37, 3):
        for record in indexes.get((0, step), []):
            surface = record["levtype"] == "sfc" and record["param"] in (SURFACE if step == 12 else ["10u", "10v"])
            profile = step == 12 and record["levtype"] == "pl" and record["param"] in ["u", "v", "t", "gh"] and int(record["levelist"]) in [925, 850]
            if surface or profile:
                wanted.append((step, record))
    wanted.extend((0, r) for r in indexes.get((0, 0), []) if r["param"] == "z" and r["levtype"] == "sfc")
    for step, record in wanted:
        try:
            samples.append(capture_record(record, args.date, 0, step, args.output,
                           step == 12 and record["levtype"] == "sfc" and record["param"] in ["10u", "10v", "10fg", "tprate", "ptype", "tcc"]))
        except Exception as exc:
            failures.append({"step": step, "record": record, "error": f"{type(exc).__name__}: {exc}"})
        dump(args.output / "point-samples.json", {
            "kind": "live_model_extraction_not_observations", "request": {"latitude": 24.71, "longitude": -77.76,
            "start_utc": (run + timedelta(hours=12)).isoformat(),
            "end_utc": (run + timedelta(hours=36)).isoformat()},
            "ecCodes_version": ec.codes_get_api_version(), "samples": samples, "failures": failures})
        print("field", step, record["param"], record.get("levelist"), "captured", len(samples), "failed", len(failures), flush=True)
    if failures or len(samples) < 40:
        raise SystemExit("Capture incomplete; inspect explicit failures and do not present as complete")


if __name__ == "__main__":
    main()

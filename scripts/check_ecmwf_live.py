"""Explicit, bounded live check. Run as `python -m scripts.check_ecmwf_live`."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import time

from request_validation import ForecastRequest
from weather.cache import Cache
from weather.fields import DataFault
from weather.pipeline import Pipeline
from weather.transport import Transport


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("A UTC offset is required")
    return parsed.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--start", type=utc, required=True)
    parser.add_argument("--duration", type=int, default=24)
    parser.add_argument(
        "--flight-levels",
        default="",
        help="Comma-separated integer flight levels, e.g. 30,450",
    )
    parser.add_argument(
        "--as-of",
        type=utc,
        help="Replay candidate discovery time; not proof of past availability",
    )
    parser.add_argument(
        "--warm", action="store_true", help="Repeat once against the same cache"
    )
    parser.add_argument("--summary", type=Path, help="Optional summary JSON path")
    args = parser.parse_args()
    try:
        levels = tuple(sorted(int(v) for v in args.flight_levels.split(",") if v))
        if (
            len(levels) > 12
            or len(set(levels)) != len(levels)
            or any(not 10 <= fl <= 450 for fl in levels)
        ):
            raise ValueError()
    except ValueError:
        parser.error("Invalid flight levels")
    if (
        not math.isfinite(args.latitude)
        or not -90 <= args.latitude <= 90
        or not math.isfinite(args.longitude)
        or not -180 <= args.longitude <= 180
        or not 1 <= args.duration <= 72
    ):
        parser.error("Invalid location or duration")
    request = ForecastRequest(
        args.latitude,
        args.longitude,
        "LIVE CHECK",
        "ECMWF",
        args.duration,
        args.start,
        levels,
    )
    summary = []
    with tempfile.TemporaryDirectory(prefix="autospot-live-cache-") as folder:
        cache = Cache(Path(folder) / "cache.sqlite3")
        for n in range(2 if args.warm else 1):
            transport = Transport(seconds=75)
            start = time.monotonic()
            try:
                data = Pipeline(transport, cache).fetch(request, now=args.as_of)
                fields = [v for r in data["rows"] for v in r["fields"].values()]
                item = {
                    "check": "warm" if n else "cold",
                    "elapsed_seconds": round(time.monotonic() - start, 3),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "cycle_utc": data["cycle_utc"],
                    "request_start_utc": data["start_utc"],
                    "request_end_utc": data["end_utc"],
                    "counts": data["counts"],
                    "cache_hits": sum(bool(f.get("cache_hit")) for f in fields),
                    "providers": dict(
                        Counter(
                            (v.get("provenance") or {}).get("provider")
                            for v in fields
                            if v["status"] == "direct"
                        )
                    ),
                    "failure_reasons": dict(
                        Counter(
                            v["reason"]
                            for v in fields
                            if v["status"] in {"missing", "invalid"}
                        )
                    ),
                    "diagnostics": data["diagnostics"],
                    "complete_forecast": False,
                    "domain": data["domain"],
                    "model_elevation": data["model_elevation"],
                    "requested_time_guidance": [
                        {
                            "valid_utc": r["valid_utc"],
                            "support_only": r["support_only"],
                            "u10": r["fields"].get("u10"),
                            "v10": r["fields"].get("v10"),
                            "flight_levels": r["flight_levels"],
                        }
                        for r in data["window_rows"]
                    ],
                }
                if data["cycle_utc"] == "2026-11-09T00:00:00+00:00" and (
                    args.latitude,
                    args.longitude,
                ) == (24.71, -77.76):
                    frozen = json.loads(
                        (
                            Path(__file__).resolve().parents[1]
                            / "tests/fixtures/phase0/ecmwf-20261109/point-samples.json"
                        ).read_text()
                    )
                    expected = [s for s in frozen["samples"] if "raw_path" in s]
                    actual = [*fields, data["orography"]]
                    matched = []
                    for sample in expected:
                        for value in actual:
                            p = value.get("provenance") or {}
                            if (
                                p.get("metadata", {}).get("paramId")
                                == sample["metadata"]["paramId"]
                                and p.get("metadata", {}).get("endStep")
                                == sample["metadata"]["endStep"]
                            ):
                                matched.append(
                                    p["message_sha256"]
                                    == sample["source_message_sha256"]
                                )
                    item["retained_raw_messages_matched"] = matched.count(True)
                    item["retained_raw_messages_mismatched"] = matched.count(False)
                summary.append(item)
                print(json.dumps(item), flush=True)
            except DataFault as error:
                summary.append(
                    {
                        "check": "warm" if n else "cold",
                        "error": error.code,
                        "state": str(error.state),
                        "elapsed_seconds": round(time.monotonic() - start, 3),
                        "details": error.details,
                    }
                )
                print(json.dumps(summary[-1]), flush=True)
                if not args.warm or n:
                    break
            finally:
                transport.close()
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    if any(
        s.get("error")
        or s.get("failure_reasons")
        or s.get("retained_raw_messages_mismatched")
        for s in summary
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()

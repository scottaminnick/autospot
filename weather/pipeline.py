"""ECMWF-first acquisition with same-run mirror fallback and explicit quality."""

from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import math
import multiprocessing
import os
import signal
import sqlite3
import threading

from weather.cache import Cache, cache_key
from weather.fields import (
    CORE,
    OROGRAPHY,
    SPECS,
    DataFault,
    FieldValue,
    State,
    decode_message,
    locate,
    parse_index,
    pressure_spec,
)
from weather.coordinates import pressure_brackets, enrich
from weather.domain import classify
from weather.transport import Transport
from request_validation import MAX_FIELD_TASKS

PROVIDERS = {
    "ecmwf": "https://data.ecmwf.int/forecasts",
    "aws": "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "google": "https://storage.googleapis.com/ecmwf-open-data",
}
VERSION = "ecmwf-contract-1"
MAX_TASKS = MAX_FIELD_TASKS


def urls(provider, run, step):
    base = PROVIDERS[provider]
    stem = f"{base}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc"
    return stem + ".grib2", stem + ".index"


def requested_steps(run, start, end):
    native = list(range(0, 145, 3))
    if run.hour in {0, 12}:
        native += list(range(150, 361, 6))
    first, last = ((t - run).total_seconds() / 3600 for t in (start, end))
    if first < 0 or last > native[-1]:
        raise DataFault("run_outside_period", State.MISSING)
    lo, hi = bisect_right(native, first) - 1, bisect_left(native, last)
    return native[lo : hi + 1]


def candidate_runs(now):
    latest = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return [latest - timedelta(hours=6 * n) for n in range(4)]


class NullCache:
    def get(self, key):
        return None

    def put(self, key, value, ttl):
        pass

    def delete(self, key):
        pass


class Pipeline:
    def __init__(self, transport, cache=None, decoder=decode_message):
        self.transport, self.cache, self.decoder = (
            transport,
            cache or NullCache(),
            decoder,
        )
        self.lock = threading.Lock()
        self.objects, self.object_locks, self.diagnostics = {}, {}, []

    def context(self, provider, run, step):
        key = cache_key(VERSION, "object", provider, run.isoformat(), step)
        with self.lock:
            mutex = self.object_locks.setdefault(key, threading.Lock())
        with mutex:
            if key in self.objects:
                value = self.objects[key]
                if isinstance(value, DataFault):
                    raise value
                return value
            try:
                self.transport.remaining()
                grib_url, index_url = urls(provider, run, step)
                cached = self.cache.get(key)
                if cached:
                    try:
                        if (
                            not isinstance(cached, dict)
                            or not isinstance(cached.get("index"), str)
                            or type(cached.get("size")) is not int
                            or not 20 <= cached["size"] <= 512 * 1024 * 1024
                            or cached.get("provider") != provider
                            or cached.get("grib_url") != grib_url
                            or cached.get("index_url") != index_url
                            or not isinstance(cached.get("etag"), (str, type(None)))
                            or not isinstance(
                                cached.get("last_modified"), (str, type(None))
                            )
                            or hashlib.sha256(cached["index"].encode()).hexdigest()
                            != cached.get("index_sha256")
                        ):
                            raise ValueError()
                        cached["rows"] = parse_index(
                            cached["index"].encode(), run, step, cached["size"]
                        )
                        self.objects[key] = cached
                        return cached
                    except (DataFault, KeyError, TypeError, ValueError):
                        self.cache.delete(key)
                index = self.transport.request(provider, "GET", index_url)
                identity = self.transport.request(provider, "HEAD", grib_url)
                try:
                    size = int(identity.headers["Content-Length"])
                    if not 20 <= size <= 512 * 1024 * 1024:
                        raise ValueError()
                except (ValueError, KeyError):
                    raise DataFault("object_size") from None
                rows = parse_index(index.body, run, step, size)
                context = {
                    "provider": provider,
                    "grib_url": grib_url,
                    "index_url": index_url,
                    "index": index.body.decode(),
                    "index_sha256": hashlib.sha256(index.body).hexdigest(),
                    "etag": identity.headers.get("ETag"),
                    "last_modified": identity.headers.get("Last-Modified"),
                    "size": size,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                self.cache.put(key, context, 300)
                context["rows"] = rows
                self.objects[key] = context
                return context
            except DataFault as error:
                self.objects[key] = error
                raise

    def available(self, run, step, specs=None):
        required = [s for s in (specs or SPECS) if s.name in CORE or s.pressure]
        found, failures = {}, []
        for provider in PROVIDERS:
            try:
                context = self.context(provider, run, step)
                found[provider] = context
                if all(locate(context["rows"], s, step) for s in required):
                    break
            except DataFault as error:
                failures.append(
                    {"provider": provider, "step": step, "reason": error.code}
                )
        complete = all(
            any(locate(c["rows"], spec, step) for c in found.values())
            for spec in required
        )
        return {"contexts": found, "complete": complete, "failures": failures}

    def select(self, start, end, now, specs=None):
        specs = specs or SPECS
        partial = None
        for run in candidate_runs(now):
            try:
                self.transport.remaining()
                steps = requested_steps(run, start, end)
                if len(steps) * len(specs) + 1 > MAX_TASKS:
                    raise DataFault("task_budget")
                # Check both boundaries, then every interior slot. No end-only sentry.
                plan = {steps[0]: self.available(run, steps[0], specs)}
                if not plan[steps[0]]["contexts"]:
                    self.diagnostics.append(
                        {
                            "cycle_utc": run.isoformat(),
                            "reason": "boundary_objects_unavailable",
                        }
                    )
                    continue
                if steps[-1] != steps[0]:
                    plan[steps[-1]] = self.available(run, steps[-1], specs)
                middle = [step for step in steps if step not in plan]
                with ThreadPoolExecutor(max_workers=4) as executor:
                    for step, item in zip(
                        middle,
                        executor.map(lambda h: self.available(run, h, specs), middle),
                    ):
                        plan[step] = item
                result = {"run": run, "steps": steps, "plan": plan}
                if all(p["complete"] for p in plan.values()):
                    return result
                if partial is None:
                    partial = result
                self.diagnostics.append(
                    {
                        "cycle_utc": run.isoformat(),
                        "reason": "incomplete_core_inventory",
                        "missing_core_steps": [
                            step
                            for step, entry in plan.items()
                            if not entry["complete"]
                        ],
                    }
                )
            except DataFault as error:
                if error.code == "task_budget":
                    raise
                self.diagnostics.append(
                    {"cycle_utc": run.isoformat(), "reason": error.code}
                )
                if error.code == "deadline":
                    break
        if partial:
            return partial
        if self.diagnostics and self.diagnostics[-1]["reason"] == "deadline":
            raise DataFault("deadline", State.MISSING)
        raise DataFault("no_run_covers_requested_period", State.MISSING)

    def field(self, spec, run, step, lat, lon):
        if spec.processing == "max" and step == 0:
            return FieldValue(
                spec.name,
                State.NOT_APPLICABLE,
                unit=spec.unit,
                reason="no_preceding_interval",
            ).as_dict()
        failures = []
        for provider in PROVIDERS:
            try:
                self.transport.remaining()
                context = self.context(provider, run, step)
                record = locate(context["rows"], spec, step)
                if record is None:
                    raise DataFault("field_absent_from_index", State.MISSING)
                offset, length = record["_offset"], record["_length"]
                identity = (context["etag"], context["last_modified"], context["size"])
                key = cache_key(
                    VERSION,
                    "point",
                    "ECMWF_IFS",
                    "0p25",
                    run.isoformat(),
                    step,
                    provider,
                    context["index_sha256"],
                    identity,
                    offset,
                    length,
                    spec.__dict__,
                    float(lat),
                    float(lon),
                )
                cached = self.cache.get(key)
                if isinstance(cached, dict) and cached.get("status") == State.DIRECT:
                    # Only validated direct values were written; enforce finite values
                    # and provenance identity again in case storage is corrupted.
                    p = cached.get("provenance")
                    if not isinstance(p, dict):
                        p = {}
                    metadata = p.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    if (
                        type(cached.get("value")) in (int, float)
                        and math.isfinite(cached["value"])
                        and p.get("index_sha256") == context["index_sha256"]
                        and p.get("object_identity") == list(identity)
                        and cached.get("unit") == spec.unit
                        and metadata.get("paramId") == spec.param_id
                        and metadata.get("typeOfLevel") == spec.level_type
                        and metadata.get("level") == spec.level
                        and p.get("cycle_utc") == run.isoformat()
                        and p.get("valid_utc")
                        == (run + timedelta(hours=step)).isoformat()
                        and p.get("requested_latitude") == lat
                        and p.get("requested_longitude") == lon
                        and (spec.minimum is None or cached["value"] >= spec.minimum)
                        and (spec.maximum is None or cached["value"] <= spec.maximum)
                    ):
                        cached["cache_hit"] = True
                        cached["attempts"] = failures
                        return cached
                    self.cache.delete(key)
                headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
                if context["etag"]:
                    headers["If-Match"] = context["etag"]
                download = self.transport.request(
                    provider,
                    "GET",
                    context["grib_url"],
                    headers=headers,
                    maximum=length,
                    expected=206,
                )
                expected_range = (
                    f"bytes {offset}-{offset + length - 1}/{context['size']}"
                )
                if (
                    download.headers.get("Content-Range") != expected_range
                    or len(download.body) != length
                ):
                    raise DataFault("wrong_content_range")
                if context["etag"] and download.headers.get("ETag") != context["etag"]:
                    raise DataFault("object_changed")
                value = self.decoder(download.body, spec, run, step, lat, lon).as_dict()
                value["provenance"].update(
                    {
                        "provider": provider,
                        "source_url": context["grib_url"],
                        "index_url": context["index_url"],
                        "index_sha256": context["index_sha256"],
                        "object_identity": list(identity),
                        "content_range": expected_range,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                if context["etag"] or context["last_modified"]:
                    self.cache.put(key, value, 1800)
                value["cache_hit"], value["attempts"] = False, failures
                return value
            except DataFault as error:
                failures.append(
                    {"provider": provider, "reason": error.code, "status": error.state}
                )
                if error.code == "object_changed":
                    object_key = cache_key(
                        VERSION, "object", provider, run.isoformat(), step
                    )
                    self.cache.delete(object_key)
                    self.objects[object_key] = error
        state = (
            State.INVALID
            if any(x["status"] == State.INVALID for x in failures)
            else State.MISSING
        )
        return FieldValue(
            spec.name,
            state,
            unit=spec.unit,
            reason=failures[-1]["reason"],
            attempts=failures,
        ).as_dict()

    def fetch(self, forecast, now=None):
        if forecast.model != "ECMWF":
            raise DataFault("gfs_adapter_not_validated", State.UNSUPPORTED)
        now = now or datetime.now(timezone.utc)
        end = forecast.start + timedelta(hours=forecast.duration)
        levels = sorted(
            {p for fl in forecast.flight_levels for p in pressure_brackets(fl)}
        )
        specs = SPECS + [
            pressure_spec(param, p) for p in levels for param in ("u", "v")
        ]
        plan = self.select(forecast.start, end, now, specs)
        run, steps = plan["run"], plan["steps"]
        jobs = [(step, spec) for step in steps for spec in specs] + [(0, OROGRAPHY)]
        slots = {
            step: {
                name: FieldValue(
                    name, State.UNSUPPORTED, reason="not_in_selected_feed"
                ).as_dict()
                for name in ["visibility", "cloud_base", "ceiling"]
            }
            for step in steps
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            values = executor.map(
                lambda item: self.field(
                    item[1], run, item[0], forecast.latitude, forecast.longitude
                ),
                jobs,
            )
            orography = None
            for (step, spec), value in zip(jobs, values):
                if spec == OROGRAPHY:
                    orography = value
                else:
                    slots[step][spec.name] = value
        if not any(
            v["status"] == State.DIRECT
            for fields in slots.values()
            for v in fields.values()
        ):
            reasons = Counter(
                f"{a['provider']}:{a['reason']}"
                for fields in slots.values()
                for value in fields.values()
                for a in (value.get("attempts") or [])
            )
            raise DataFault("no_validated_fields", State.MISSING, dict(reasons))
        counts = {str(s): 0 for s in State}
        for fields in slots.values():
            for value in fields.values():
                counts[value["status"]] += 1
        data = {
            "schema_version": 2,
            "model": "ECMWF_IFS",
            "grid": "0p25",
            "cycle_utc": run.isoformat(),
            "start_utc": forecast.start.isoformat(),
            "end_utc": end.isoformat(),
            "latitude": forecast.latitude,
            "longitude": forecast.longitude,
            "core_inventory_complete": all(
                p["complete"] for p in plan["plan"].values()
            ),
            "counts": counts,
            "diagnostics": self.diagnostics,
            "rows": [
                {
                    "step": step,
                    "valid_utc": (run + timedelta(hours=step)).isoformat(),
                    "support_only": not forecast.start
                    <= run + timedelta(hours=step)
                    < end,
                    "fields": slots[step],
                }
                for step in steps
            ],
            "orography": orography,
            "complete_forecast": False,
            "expected_steps": steps,
            "domain": classify(
                forecast.latitude, forecast.longitude, forecast.domain_disposition
            ),
        }
        return enrich(data, forecast.flight_levels)


def _worker(connection, forecast, cache_path, seconds):
    # A kernel alarm also bounds an orphan if the web worker exits unexpectedly.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(math.ceil(seconds) + 2)
    transport = Transport(seconds=seconds)
    try:
        try:
            cache = Cache(cache_path)
        except (sqlite3.Error, OSError):
            cache = NullCache()
        result = Pipeline(transport, cache).fetch(forecast)
        connection.send({"data": result})
    except DataFault as error:
        connection.send(
            {"error": error.code, "state": str(error.state), "details": error.details}
        )
    except Exception:
        connection.send({"error": "pipeline_failed", "state": "invalid"})
    finally:
        transport.close()
        connection.close()


def retrieve_forecast(forecast, cache_path=None, timeout=80, worker=_worker):
    """Isolated child: slow streams/native decode cannot outlive the web deadline."""
    if not 1 <= timeout <= 120:
        raise ValueError("Invalid request deadline")
    if forecast.model != "ECMWF":
        raise DataFault("gfs_adapter_not_validated", State.UNSUPPORTED)
    cache_path = cache_path or os.environ.get(
        "AUTOSPOT_FORECAST_CACHE", "/tmp/autospot-state/forecast-cache.sqlite3"
    )
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=worker,
        args=(child, forecast, cache_path, max(0.1, timeout - 5)),
        daemon=True,
    )
    try:
        process.start()
        child.close()
        if not parent.poll(timeout):
            raise DataFault("deadline", State.MISSING)
        try:
            message = parent.recv()
        except EOFError:
            raise DataFault("pipeline_interrupted", State.MISSING) from None
        if "error" in message:
            raise DataFault(
                message["error"], State(message["state"]), message.get("details")
            )
        return message["data"]
    finally:
        if process.pid:
            process.join(0.2)
            if process.is_alive():
                process.terminate()
                process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
            process.close()
        parent.close()
        child.close()

"""Frozen-byte decoding and deterministic pipeline fault simulations."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import eccodes as ec
import requests

from request_validation import ForecastRequest
from weather.cache import Cache
from weather.draft import build_draft, chart
from weather.fields import (
    DataFault,
    FieldValue,
    OROGRAPHY,
    SPECS,
    State,
    decode_message,
    locate,
    parse_index,
    pressure_spec,
)
from weather.pipeline import PROVIDERS, Pipeline, requested_steps, retrieve_forecast
from weather.transport import Download, Transport

ROOT = Path(__file__).parent / "fixtures/phase0/ecmwf-20261109"
RUN = datetime(2026, 11, 9, tzinfo=timezone.utc)


def forecast(start=None, duration=24):
    return ForecastRequest(
        24.71,
        -77.76,
        "Fixture test",
        "ECMWF",
        duration,
        start or RUN + timedelta(hours=12),
    )


def slow_worker(connection, request, path, seconds):
    time.sleep(10)


def success_worker(connection, request, path, seconds):
    connection.send({"data": {"model": "ECMWF_IFS"}})
    connection.close()


def synthetic_decode(body, spec, run, step, lat, lon):
    values = {
        "u10": -2.45025634765625,
        "v10": -2.776123046875,
        "gust10": 5.7,
        "tcc": 0.671875,
        "precip_rate": 0,
        "precip_type": 0,
    }
    return FieldValue(
        spec.name,
        State.DIRECT,
        values.get(spec.name, 1.0),
        spec.unit,
        provenance={
            "model": "ECMWF_IFS",
            "grid": "0p25",
            "sample_latitude": lat,
            "sample_longitude": lon,
            "cycle_utc": run.isoformat(),
            "valid_utc": (run + timedelta(hours=step)).isoformat(),
            "interval_start_utc": (
                run
                + timedelta(
                    hours=max(0, step - 1) if spec.processing == "max" else step
                )
            ).isoformat(),
            "interval_end_utc": (run + timedelta(hours=step)).isoformat(),
            "metadata": {
                "paramId": spec.param_id,
                "typeOfLevel": spec.level_type,
                "level": spec.level,
            },
            "requested_latitude": lat,
            "requested_longitude": lon,
        },
    )


class FakeTransport:
    """Synthetic object service. AWS offsets deliberately differ from ECMWF."""

    def __init__(self, runs=None):
        self.runs = runs or {RUN}
        self.calls, self.blocked, self.missing = [], set(), set()
        self.faults, self.etag_version = {}, 1
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def remaining(self):
        return 1000

    def rows(self, provider, run, step):
        filename = (
            "ecmwf-20261109-00-f000.index"
            if step == 0
            else "ecmwf-20261109-00-f012.index"
        )
        rows = [
            json.loads(line)
            for line in (ROOT / "indexes" / filename).read_text().splitlines()
        ]
        for row in rows:
            row.update(date=run.strftime("%Y%m%d"), time=run.strftime("%H00"))
            if step != 0:
                row["step"] = str(step)
            if provider == "aws":
                row["_offset"] += 100
        return [
            r for r in rows if (provider, run, step, r["param"]) not in self.missing
        ]

    def request(
        self,
        provider,
        method,
        url,
        *,
        headers=None,
        maximum=2 * 1024 * 1024,
        expected=200,
    ):
        match = re.search(r"/(\d{8})/(\d{2})z/.*/\d{14}-(\d+)h-oper-fc", url)
        run = datetime.strptime(match[1] + match[2], "%Y%m%d%H").replace(
            tzinfo=timezone.utc
        )
        step = int(match[3])
        with self.lock:
            self.calls.append((provider, method, run, step, headers))
        if run not in self.runs or (provider, run, step) in self.blocked:
            raise DataFault("http_404", State.MISSING)
        rows = self.rows(provider, run, step)
        # Removed fields leave gaps, and the underlying object remains unchanged.
        size = max(r["_offset"] + r["_length"] for r in rows)
        etag = f'"{provider}-{run:%Y%m%d%H}-{step}-{self.etag_version}"'
        if url.endswith(".index"):
            return Download(("\n".join(json.dumps(r) for r in rows)).encode(), {}, 200)
        if method == "HEAD":
            return Download(
                b"",
                {"Content-Length": str(size), "ETag": etag, "Last-Modified": "fixed"},
                200,
            )
        start, end = (int(v) for v in headers["Range"][6:].split("-"))
        fault = self.faults.get((provider, run, step))
        if isinstance(fault, DataFault):
            raise fault
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.001)
            length = end - start + 1
            body = b"x" * (length - 1 if fault == "truncated" else length)
            content_range = f"bytes {start}-{end}/{size}"
            if fault == "wrong_range":
                content_range = f"bytes {start + 1}-{end + 1}/{size}"
            return Download(
                body,
                {
                    "Content-Range": content_range,
                    "ETag": '"changed"' if fault == "changed" else etag,
                },
                206,
            )
        finally:
            with self.lock:
                self.active -= 1


class FrozenDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((ROOT / "point-samples.json").read_text())
        cls.raw = [s for s in cls.snapshot["samples"] if "raw_path" in s]

    def test_all_retained_messages_match_frozen_points_and_intervals(self):
        for sample in self.raw:
            spec = (
                OROGRAPHY
                if sample["index_record"]["param"] == "z"
                else next(
                    s for s in SPECS if s.param == sample["index_record"]["param"]
                )
            )
            with self.subTest(param=spec.param):
                result = decode_message(
                    (ROOT / sample["raw_path"]).read_bytes(),
                    spec,
                    RUN,
                    sample["metadata"]["endStep"],
                    24.71,
                    -77.76,
                )
                self.assertEqual(result.status, State.DIRECT)
                self.assertAlmostEqual(
                    result.value, sample["nearest"]["value"], places=9
                )
                self.assertEqual(
                    result.provenance["message_sha256"], sample["source_message_sha256"]
                )
                self.assertEqual(
                    {
                        k: result.provenance["metadata"][k]
                        for k in sample["metadata"]
                        if k in result.provenance["metadata"]
                    },
                    {
                        k: v
                        for k, v in sample["metadata"].items()
                        if k in result.provenance["metadata"]
                    },
                )

    def test_wrong_parameter_level_units_time_grid_and_processing_rejected(self):
        sample = next(s for s in self.raw if s["index_record"]["param"] == "10u")
        payload = (ROOT / sample["raw_path"]).read_bytes()
        mutations = [
            ("paramId", 166),
            ("level", 2),
            ("dataDate", 20000101),
            ("jDirectionIncrementInDegrees", 0.5),
            ("longitudeOfFirstGridPointInDegrees", 0),
            ("forecastTime", 15),
        ]
        for key, value in mutations:
            with self.subTest(key=key):
                gid = ec.codes_new_from_message(payload)
                try:
                    ec.codes_set(gid, key, value)
                    modified = ec.codes_get_message(gid)
                finally:
                    ec.codes_release(gid)
                with self.assertRaises(DataFault):
                    decode_message(modified, SPECS[0], RUN, 12, 24.71, -77.76)
        for spec in [
            replace(SPECS[0], source_unit="K"),
            replace(SPECS[0], processing="max"),
        ]:
            with self.assertRaises(DataFault):
                decode_message(payload, spec, RUN, 12, 24.71, -77.76)
        original_get = ec.codes_get

        def wrong_model(gid, key, *args, **kwargs):
            return (
                "AIFS"
                if key == "modelName"
                else original_get(gid, key, *args, **kwargs)
            )

        with (
            patch.object(ec, "codes_get", side_effect=wrong_model),
            self.assertRaises(DataFault) as error,
        ):
            decode_message(payload, SPECS[0], RUN, 12, 24.71, -77.76)
        self.assertEqual(error.exception.code, "wrong_modelName")

    def test_bad_framing_and_nonfinite_nearest_value_rejected(self):
        sample = next(s for s in self.raw if s["index_record"]["param"] == "10u")
        payload = (ROOT / sample["raw_path"]).read_bytes()
        for body in [payload[:-1], payload + b"extra", b"not grib"]:
            with self.assertRaises(DataFault):
                decode_message(body, SPECS[0], RUN, 12, 24.71, -77.76)
        with (
            patch.object(
                ec, "codes_grib_find_nearest", return_value=[{"value": float("nan")}]
            ),
            self.assertRaises(DataFault),
        ):
            decode_message(payload, SPECS[0], RUN, 12, 24.71, -77.76)

    def test_index_is_strict_and_uses_record_time(self):
        body = (ROOT / "indexes/ecmwf-20261109-00-f000.index").read_bytes()
        size = max(
            r["_offset"] + r["_length"]
            for r in (json.loads(l) for l in body.splitlines())
        )
        rows = parse_index(body, RUN, 0, size)
        self.assertIsNotNone(locate(rows, OROGRAPHY, 0))
        self.assertIsNone(locate(rows, SPECS[0], 3))
        for bad in [
            body + b"\nmalformed",
            body.replace(b'"_offset": 0', b'"_offset": -1', 1),
            body.replace(b'"date": "20261109"', b'"date": "20000101"', 1),
        ]:
            with self.assertRaises(DataFault):
                parse_index(bad, RUN, 0, size)
        with self.assertRaises(DataFault):
            parse_index(body, RUN, 0, size - 1)

    def test_pressure_specs_use_actual_pressure_metadata(self):
        spec = pressure_spec("u", 925)
        self.assertEqual(
            (spec.level, spec.level_type, spec.param_id), (925, "isobaricInhPa", 131)
        )
        for args in [("u", 900), ("wrong", 925)]:
            with self.assertRaises(ValueError):
                pressure_spec(*args)


class PipelineTests(unittest.TestCase):
    def pipeline(self, transport=None, cache=None, decoder=synthetic_decode):
        return Pipeline(transport or FakeTransport(), cache, decoder)

    def test_full_original_window_and_expiration_are_preserved(self):
        service = FakeTransport()
        data = self.pipeline(service).fetch(forecast(), now=RUN)
        self.assertEqual([r["step"] for r in data["rows"]], list(range(12, 37, 3)))
        self.assertEqual(data["counts"]["direct"], 144)
        self.assertEqual(data["counts"]["unsupported"], 27)
        self.assertFalse(data["complete_forecast"])
        text = build_draft(data, "Scenario")
        self.assertIn("FM091200 04007KT", text)
        self.assertNotIn("FM101200", text)
        self.assertIn("[VISIBILITY REQUIRED]", text)
        self.assertNotIn("P6SM", text)
        self.assertNotIn("OVC025", text)
        self.assertNotIn("PROB30", text)
        self.assertLessEqual(service.peak, 4)

    def test_missing_interior_inventory_does_not_pass_end_sentry(self):
        service = FakeTransport({RUN, RUN + timedelta(hours=6)})
        newer = RUN + timedelta(hours=6)
        for provider in PROVIDERS:
            service.missing.add((provider, newer, 15, "10v"))
        data = self.pipeline(service).fetch(forecast(), now=newer)
        self.assertEqual(data["cycle_utc"], RUN.isoformat())
        self.assertTrue(
            any(d["reason"] == "incomplete_core_inventory" for d in data["diagnostics"])
        )

    def test_unpublished_first_object_skips_candidate_without_end_probe(self):
        newer = RUN + timedelta(hours=6)
        service = FakeTransport({RUN, newer})
        for provider in PROVIDERS:
            service.blocked.add((provider, newer, 6))
        data = self.pipeline(service).fetch(forecast(), now=newer)
        self.assertEqual(data["cycle_utc"], RUN.isoformat())
        self.assertFalse(
            any(run == newer and step == 30 for _, _, run, step, _ in service.calls)
        )

    def test_zero_hour_gust_is_not_applicable_not_zero(self):
        data = self.pipeline().fetch(forecast(RUN, 1), now=RUN)
        gust = data["rows"][0]["fields"]["gust10"]
        self.assertEqual(gust["status"], "not_applicable")
        self.assertIsNone(gust["value"])
        self.assertTrue(
            all(
                v["provenance"]["cycle_utc"] == RUN.isoformat()
                for r in data["rows"]
                for v in r["fields"].values()
                if v["status"] == "direct"
            )
        )

    def test_partial_run_keeps_missing_middle_and_never_changes_model(self):
        service = FakeTransport()
        for provider in PROVIDERS:
            service.missing.add((provider, RUN, 24, "10u"))
        data = self.pipeline(service).fetch(forecast(), now=RUN)
        middle = next(r for r in data["rows"] if r["step"] == 24)
        self.assertEqual(middle["fields"]["u10"]["status"], "missing")
        self.assertIsNone(middle["fields"]["u10"]["value"])
        self.assertFalse(data["core_inventory_complete"])
        self.assertIn("FM100000 [WIND REVIEW REQUIRED]", build_draft(data, "Partial"))
        self.assertEqual(data["model"], "ECMWF_IFS")

    def test_mirror_uses_own_index_offsets_and_recovers_invalid_ranges(self):
        for fault in [
            DataFault("http_503", State.MISSING),
            "wrong_range",
            "truncated",
            "changed",
        ]:
            with self.subTest(fault=str(fault)):
                service = FakeTransport()
                service.faults["ecmwf", RUN, 12] = fault
                value = self.pipeline(service).field(SPECS[0], RUN, 12, 24.71, -77.76)
                self.assertEqual(value["status"], "direct")
                self.assertEqual(value["provenance"]["provider"], "aws")
                requests_by_provider = {
                    p: h["Range"]
                    for p, m, r, s, h in service.calls
                    if h and "Range" in h
                }
                first = int(requests_by_provider["ecmwf"].split("=")[1].split("-")[0])
                second = int(requests_by_provider["aws"].split("=")[1].split("-")[0])
                self.assertEqual(second - first, 100)

    def test_failed_decodes_are_invalid_and_never_cached_as_zero(self):
        def invalid(*args):
            raise DataFault("wrong_units")

        result = self.pipeline(decoder=invalid).field(SPECS[0], RUN, 12, 24.71, -77.76)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["value"])
        with self.assertRaises(DataFault):
            self.pipeline(decoder=invalid).fetch(forecast(duration=1), now=RUN)

    def test_non_native_start_keeps_brackets_and_requires_initial_review(self):
        data = self.pipeline().fetch(forecast(RUN + timedelta(hours=13), 2), now=RUN)
        self.assertEqual([r["step"] for r in data["rows"]], [12, 15])
        text = build_draft(data, "Off hour")
        self.assertIn("FM091300 04007KT", text)
        self.assertIn("Time interpolation at 2026-11-09T13:00:00+00:00", text)
        self.assertNotIn("FM091200", text)
        self.assertEqual(data["start_utc"], (RUN + timedelta(hours=13)).isoformat())

    def test_native_step_cadence_and_cross_year_coverage(self):
        run = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
        self.assertEqual(
            requested_steps(
                run, run + timedelta(hours=143), run + timedelta(hours=151)
            ),
            [141, 144, 150, 156],
        )
        with self.assertRaises(DataFault):
            requested_steps(
                run.replace(hour=6),
                run + timedelta(hours=144),
                run + timedelta(hours=150),
            )
        with self.assertRaises(DataFault):
            requested_steps(RUN, RUN - timedelta(hours=1), RUN)

    def test_gfs_cannot_fall_back_to_ecmwf_silently(self):
        service = FakeTransport()
        with self.assertRaises(DataFault):
            self.pipeline(service).fetch(replace(forecast(), model="GFS"), now=RUN)
        self.assertEqual(service.calls, [])

    def test_task_budget_is_checked_before_any_network(self):
        service = FakeTransport()
        with (
            patch("weather.pipeline.MAX_TASKS", 10),
            self.assertRaises(DataFault) as error,
        ):
            self.pipeline(service).fetch(forecast(), now=RUN)
        self.assertEqual(error.exception.code, "task_budget")
        self.assertEqual(service.calls, [])

    def test_google_recovers_same_field_when_both_earlier_providers_fail(self):
        service = FakeTransport()
        for provider in ["ecmwf", "aws"]:
            service.blocked.add((provider, RUN, 12))
        result = self.pipeline(service).field(SPECS[0], RUN, 12, 24.71, -77.76)
        self.assertEqual(result["status"], "direct")
        self.assertEqual(result["provenance"]["provider"], "google")
        self.assertEqual(result["provenance"]["cycle_utc"], RUN.isoformat())
        self.assertEqual(len(result["attempts"]), 2)

    def test_corrupt_cached_provenance_is_evicted_and_refetched(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Cache(Path(folder) / "cache.sqlite3")
            self.pipeline(cache=cache).field(SPECS[0], RUN, 12, 24.71, -77.76)
            with cache.connection() as db:
                rows = db.execute("SELECT key,value FROM cache").fetchall()
                for key, raw in rows:
                    item = json.loads(raw)
                    if item.get("status") == "direct":
                        item["provenance"] = ["corrupt"]
                        db.execute(
                            "UPDATE cache SET value=? WHERE key=?",
                            (json.dumps(item), key),
                        )
            transport = FakeTransport()
            value = self.pipeline(transport, cache).field(
                SPECS[0], RUN, 12, 24.71, -77.76
            )
            self.assertEqual(value["status"], "direct")
            self.assertFalse(value["cache_hit"])
            self.assertEqual(len(transport.calls), 1)

    def test_failed_fields_are_not_negatively_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Cache(Path(folder) / "cache.sqlite3")
            bad = FakeTransport()
            for provider in PROVIDERS:
                bad.faults[provider, RUN, 12] = DataFault("http_503", State.MISSING)
            missing = self.pipeline(bad, cache).field(SPECS[0], RUN, 12, 24.71, -77.76)
            self.assertIsNone(missing["value"])
            good = FakeTransport()
            restored = self.pipeline(good, cache).field(
                SPECS[0], RUN, 12, 24.71, -77.76
            )
            self.assertEqual(restored["status"], "direct")
            self.assertFalse(restored["cache_hit"])

    def test_positive_cache_is_shared_and_keyed_by_point_and_identity(self):
        with tempfile.TemporaryDirectory() as path:
            cache = Cache(Path(path) / "cache.sqlite3")
            first = FakeTransport()
            a = self.pipeline(first, cache).field(SPECS[0], RUN, 12, 24.71, -77.76)
            second = FakeTransport()
            b = self.pipeline(second, Cache(Path(path) / "cache.sqlite3")).field(
                SPECS[0], RUN, 12, 24.71, -77.76
            )
            self.assertFalse(a["cache_hit"])
            self.assertTrue(b["cache_hit"])
            self.assertEqual(second.calls, [])
            c = self.pipeline(second, cache).field(SPECS[0], RUN, 12, 25, -77.76)
            self.assertFalse(c["cache_hit"])
            with cache.connection() as db:
                db.execute(
                    "DELETE FROM cache WHERE CAST(value AS TEXT) LIKE '%index_sha256%' AND CAST(value AS TEXT) LIKE '%\"index\":%'"
                )
            third = FakeTransport()
            third.etag_version = 2
            d = self.pipeline(third, cache).field(SPECS[0], RUN, 12, 24.71, -77.76)
            self.assertFalse(d["cache_hit"])
            self.assertEqual(
                d["provenance"]["object_identity"][0], '"ecmwf-2026110900-12-2"'
            )

    def test_chart_uses_raw_fields_and_renders_a_png(self):
        import base64

        data = self.pipeline().fetch(forecast(duration=1), now=RUN)
        image = base64.b64decode(chart(data))
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(data["rows"][0]["fields"]["visibility"]["value"], None)

    def test_process_deadline_terminates_work_and_success_protocol(self):
        before = {p.pid for p in multiprocessing.active_children()}
        start = time.monotonic()
        with self.assertRaises(DataFault) as error:
            retrieve_forecast(forecast(), timeout=1, worker=slow_worker)
        self.assertEqual(error.exception.code, "deadline")
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        self.assertEqual(
            retrieve_forecast(forecast(), timeout=5, worker=success_worker),
            {"model": "ECMWF_IFS"},
        )


class FakeResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = iter(responses), {}, []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


class TransportCacheTests(unittest.TestCase):
    def test_retry_backoff_and_retry_after_circuit_are_bounded(self):
        fake = FakeSession([FakeResponse(503), FakeResponse(200)])
        transport = Transport(session_factory=lambda: fake, sleep=lambda _: None)
        self.assertEqual(
            transport.request("ecmwf", "GET", "https://example.invalid").body, b"ok"
        )
        self.assertEqual(len(fake.calls), 2)
        fake = FakeSession([FakeResponse(429, headers={"Retry-After": "60"})])
        transport = Transport(session_factory=lambda: fake, sleep=lambda _: None)
        for _ in range(2):
            with self.assertRaises(DataFault):
                transport.request("ecmwf", "GET", "https://example.invalid")
        self.assertEqual(len(fake.calls), 1)

    def test_404_wrong_status_oversize_truncation_and_encoding(self):
        for response in [
            FakeResponse(404),
            FakeResponse(200),
            FakeResponse(206, body=b"large"),
            FakeResponse(206, headers={"Content-Length": "3"}),
            FakeResponse(206, headers={"Content-Encoding": "gzip"}),
        ]:
            fake = FakeSession([response])
            transport = Transport(session_factory=lambda: fake)
            with self.assertRaises(DataFault):
                transport.request(
                    "ecmwf", "GET", "https://example.invalid", maximum=2, expected=206
                )
            self.assertEqual(len(fake.calls), 1)

    def test_timeout_and_deadline_have_no_nested_retry(self):
        fake = FakeSession([requests.Timeout(), requests.Timeout()])
        transport = Transport(session_factory=lambda: fake, sleep=lambda _: None)
        with self.assertRaises(DataFault):
            transport.request("ecmwf", "GET", "https://example.invalid")
        self.assertEqual(len(fake.calls), 2)
        times = iter([0, 2])
        transport = Transport(
            seconds=1, clock=lambda: next(times), session_factory=lambda: fake
        )
        with self.assertRaises(DataFault) as error:
            transport.request("ecmwf", "GET", "https://example.invalid")
        self.assertEqual(error.exception.code, "deadline")

    def test_cache_expiry_eviction_and_corruption(self):
        with tempfile.TemporaryDirectory() as path:
            cache = Cache(Path(path) / "cache.sqlite3", max_bytes=1000)
            for n in range(10):
                cache.put(str(n), {"payload": "x" * 200}, 60)
            with cache.connection() as db:
                self.assertLessEqual(
                    db.execute("SELECT SUM(size) FROM cache").fetchone()[0], 1000
                )
                db.execute("UPDATE cache SET expires=0 WHERE key='9'")
            self.assertIsNone(cache.get("0"))
            self.assertIsNone(cache.get("9"))
            cache.put("bad", {"valid": True}, 60)
            with cache.connection() as db:
                db.execute("UPDATE cache SET value='not JSON' WHERE key='bad'")
            self.assertIsNone(cache.get("bad"))


if __name__ == "__main__":
    unittest.main()

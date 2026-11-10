"""Offline evidence checks, not tests of a repaired application.

Run: python -m unittest discover -s tests -p 'test_phase0*.py' -v
Raw decode tests require eccodes; absence is an error, not a silent skip.
"""
import hashlib
import json
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes as ec

ROOT = Path(__file__).parent / "fixtures" / "phase0"
DATA = ROOT / "ecmwf-20261109"


def read(path):
    return json.loads(path.read_text())


class FrozenEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.point = read(DATA / "point-samples.json")
        cls.samples = cls.point["samples"]
        cls.surface = {s["index_record"]["param"]: s for s in cls.samples
                       if s["index_record"]["levtype"] == "sfc"
                       and s["metadata"]["endStep"] == 12}

    def test_manifest_hashes(self):
        for item in read(ROOT / "manifest.json")["files"]:
            with self.subTest(path=item["path"]):
                content = (ROOT / item["path"]).read_bytes()
                self.assertEqual(len(content), item["bytes"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])

    def test_capture_is_complete_for_declared_scope(self):
        self.assertEqual(self.point["failures"], [])
        self.assertEqual(len(self.samples), 41)
        self.assertEqual(len(self.surface), 16)
        self.assertEqual(sum("raw_path" in s for s in self.samples), 7)

    def test_raw_messages_decode_to_frozen_values(self):
        for sample in self.samples:
            if "raw_path" not in sample:
                continue
            with self.subTest(path=sample["raw_path"]):
                payload = (DATA / sample["raw_path"]).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), sample["source_message_sha256"])
                self.assertEqual(payload[:4], b"GRIB")
                self.assertEqual(payload[-4:], b"7777")
                self.assertEqual(int.from_bytes(payload[8:16], "big"), len(payload))
                gid = ec.codes_new_from_message(payload)
                try:
                    for key in ["paramId", "typeOfLevel", "level", "units", "dataDate",
                                "dataTime", "startStep", "endStep", "stepType"]:
                        self.assertEqual(ec.codes_get(gid, key), sample["metadata"][key], key)
                    point = ec.codes_grib_find_nearest(gid, 24.71, -77.76, npoints=1)[0]
                    self.assertAlmostEqual(point["value"], sample["nearest"]["value"], places=9)
                    self.assertEqual((point["lat"], point["lon"]), (24.75, -77.75))
                finally:
                    ec.codes_release(gid)

    def test_valid_times_and_full_wind_timeline(self):
        run = datetime(2026, 11, 9, tzinfo=timezone.utc)
        for sample in self.samples:
            m = sample["metadata"]
            actual = datetime.strptime(f"{m['validityDate']}{m['validityTime']:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            self.assertEqual(actual, run + timedelta(hours=m["endStep"]))
        for param in ["10u", "10v"]:
            steps = sorted(s["metadata"]["endStep"] for s in self.samples if s["index_record"]["param"] == param)
            self.assertEqual(steps, list(range(12, 37, 3)))
        self.assertEqual(self.point["request"]["start_utc"], "2026-11-09T12:00:00Z")
        self.assertEqual(self.point["request"]["end_utc"], "2026-11-10T12:00:00Z")

    def test_wind_reference_and_no_component_swap(self):
        u, v = (self.surface[p]["nearest"]["value"] for p in ["10u", "10v"])
        self.assertAlmostEqual(u, -2.45025634765625)
        self.assertAlmostEqual(v, -2.776123046875)
        self.assertAlmostEqual(math.hypot(u, v) * 3600 / 1852, 7.197637857367922)
        self.assertAlmostEqual(math.degrees(math.atan2(-u, -v)) % 360, 41.43220540186311)

    def test_processing_intervals_not_assumed_from_file_spacing(self):
        gust = self.surface["10fg"]["metadata"]
        self.assertEqual((gust["stepType"], gust["startStep"], gust["endStep"]), ("max", 11, 12))
        for p in ["tp", "cp"]:
            m = self.surface[p]["metadata"]
            self.assertEqual((m["stepType"], m["startStep"], m["endStep"], m["units"]), ("accum", 0, 12, "m"))
        self.assertEqual(self.surface["tprate"]["metadata"]["stepType"], "instant")

    def test_cloud_overlap_and_missing_aviation_fields(self):
        total = self.surface["tcc"]["nearest"]["value"]
        layers = sum(self.surface[p]["nearest"]["value"] for p in ["lcc", "mcc", "hcc"])
        self.assertEqual(total, 0.671875)
        self.assertEqual(layers, 0.828125)
        rows = [json.loads(l) for l in (DATA / "indexes/ecmwf-20261109-00-f012.index").read_text().splitlines()]
        self.assertFalse({"vis", "cbh", "ceil"} & {r["param"] for r in rows})

    def test_cycle_and_provider_inventory_evidence(self):
        obs = read(DATA / "inventory-observations.json")
        for cycle in [0, 6, 12, 18]:
            self.assertTrue(any(o["provider"] == "ecmwf" and o["cycle"] == cycle
                                and o["step"] == 12 and "path" in o for o in obs))
        mirror = read(DATA / "mirror-comparison.json")
        self.assertTrue(mirror["identical_message_to_ecmwf"])
        self.assertEqual(mirror["sample"]["source_message_sha256"], self.surface["10u"]["source_message_sha256"])
        self.assertIn("ecmwf-forecasts.s3.amazonaws.com", mirror["sample"]["source_url"])

    def test_zero_hour_index_has_mixed_record_steps(self):
        rows = [json.loads(l) for l in (DATA / "indexes/ecmwf-20261109-00-f000.index").read_text().splitlines()]
        nonzero = [(r["param"], r["step"]) for r in rows if r["step"] != "0"]
        self.assertCountEqual(nonzero, [("mx2t3", "3"), ("mn2t3", "3")])
        self.assertTrue(any(r["param"] == "z" and r["levtype"] == "sfc" and r["step"] == "0" for r in rows))

    def test_pressure_levels_are_not_altitude_labels(self):
        profile = [s for s in self.samples if s["index_record"]["levtype"] == "pl"]
        self.assertEqual(len(profile), 8)
        for s in profile:
            self.assertEqual(s["metadata"]["typeOfLevel"], "isobaricInhPa")
            self.assertEqual(s["metadata"]["level"], int(s["index_record"]["levelist"]))
        gh850 = next(s for s in profile if s["index_record"]["param"] == "gh" and s["metadata"]["level"] == 850)
        self.assertAlmostEqual(gh850["nearest"]["value"], 1551.288818359375)

    def test_domain_source_preserves_dateline_and_three_aawu_regions(self):
        awc = read(ROOT / "domain/awc_sigmet_area.json")
        coords = awc["features"][0]["geometry"]["coordinates"][0][0]
        self.assertTrue(any(x < -180 for x, y in coords))
        alaska = read(ROOT / "domain/aawu-firs.geojson")
        self.assertEqual({f["properties"]["name"] for f in alaska["features"]},
                         {"Anchorage Continental", "Anchorage Oceanic", "Anchorage Arctic"})
        cases = {c["id"]: c for c in read(ROOT / "domain/test-locations.json")["locations"]}
        self.assertEqual(cases["original_bahamas"]["expected_routing"], "other_office_review")
        self.assertEqual(cases["anchorage"]["expected_routing"], "AAWU")
        self.assertEqual(cases["awc_boundary"]["expected_routing"], "boundary_review")


if __name__ == "__main__":
    unittest.main()

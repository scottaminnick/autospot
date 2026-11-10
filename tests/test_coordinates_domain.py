"""Independent numeric references and source geometry/time regressions; no network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import app
from request_validation import parse_forecast_request, RequestValidationError
from weather.coordinates import (
    enrich,
    flight_level_pressure,
    flight_level_wind,
    pressure_brackets,
    sample_time,
    utc,
)
from weather.domain import classify, regions, load_geometry, location_map
from weather.draft import build_draft, chart, wind
from weather.fields import DataFault
from weather.fields import decode_message, pressure_spec
from weather.pipeline import Pipeline, PROVIDERS
from test_ecmwf_pipeline import FakeTransport, synthetic_decode, forecast, RUN

TIME = datetime(2026, 12, 31, 21, tzinfo=timezone.utc)


def field(name, value, unit="m/s", valid=TIME, **p):
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "status": "direct",
        "reason": None,
        "provenance": {
            "model": "ECMWF_IFS",
            "grid": "0p25",
            "cycle_utc": TIME.isoformat(),
            "valid_utc": valid.isoformat(),
            "sample_latitude": 40,
            "sample_longitude": -100,
            **p,
        },
    }


def row(valid=TIME, u=0, v=-10):
    return {
        "valid_utc": valid.isoformat(),
        "fields": {
            "u10": field("u10", u, valid=valid),
            "v10": field("v10", v, valid=valid),
            "surface_pressure": field("surface_pressure", 101325, "Pa", valid),
            **{
                name: field(name, 1, unit, valid)
                for name, unit in [
                    ("gust10", "m/s"),
                    ("tcc", "fraction"),
                    ("precip_type", "code_4.201"),
                    ("precip_total", "m"),
                    ("precip_rate", "kg/m2/s"),
                    ("cape", "J/kg"),
                ]
            },
        },
    }


def profile(fl=30, sp=101325, valid=TIME):
    low, high = pressure_brackets(fl)
    return {
        "surface_pressure": field("surface_pressure", sp, "Pa", valid),
        **{
            f"{param}_{p}": field(f"{param}_{p}", value, valid=valid)
            for p, u, v in [(low, -10, -20), (high, 10, -40)]
            for param, value in [("u", u), ("v", v)]
        },
    }


class TimeTests(unittest.TestCase):
    def test_utc_requires_offset_and_normalizes(self):
        self.assertEqual(utc("2027-01-01T00:00:00+03:00"), TIME)
        with self.assertRaises(ValueError):
            utc("2026-12-31T21:00:00")

    def test_north_crossing_interpolates_components_and_keeps_sources(self):
        speed = 10
        rows = [
            row(
                TIME,
                speed * math.sin(math.radians(10)),
                -speed * math.cos(math.radians(10)),
            ),
            row(
                TIME + timedelta(hours=3),
                -speed * math.sin(math.radians(10)),
                -speed * math.cos(math.radians(10)),
            ),
        ]
        middle = sample_time(rows, TIME + timedelta(minutes=90))
        self.assertEqual(wind(middle["fields"]), "36019KT")
        self.assertEqual(middle["fields"]["u10"]["status"], "derived")
        self.assertEqual(
            middle["fields"]["u10"]["provenance"]["source_times_utc"],
            [r["valid_utc"] for r in rows],
        )
        self.assertAlmostEqual(middle["fields"]["u10"]["provenance"]["weight"], 0.5)
        self.assertEqual(rows[0]["fields"]["u10"]["status"], "direct")

    def test_noncontinuous_fields_are_never_time_interpolated(self):
        result = sample_time(
            [row(), row(TIME + timedelta(hours=3))], TIME + timedelta(hours=1)
        )
        for name in [
            "gust10",
            "tcc",
            "precip_type",
            "precip_total",
            "precip_rate",
            "cape",
        ]:
            self.assertIsNone(result["fields"][name]["value"])
            self.assertEqual(
                result["fields"][name]["reason"], "time_interpolation_not_permitted"
            )

    def test_no_extrapolation_or_long_gap_interpolation(self):
        rows = [row(), row(TIME + timedelta(hours=6))]
        for valid in [
            TIME - timedelta(hours=1),
            TIME + timedelta(hours=3),
            TIME + timedelta(hours=7),
        ]:
            self.assertIsNone(sample_time(rows, valid)["fields"]["u10"]["value"])

    def test_missing_native_value_not_filled_and_sources_must_match(self):
        rows = [row(), row(TIME + timedelta(hours=3))]
        rows[0]["fields"]["u10"].update(status="missing", value=None)
        self.assertEqual(sample_time(rows, TIME)["fields"]["u10"]["status"], "missing")
        self.assertIsNone(
            sample_time(rows, TIME + timedelta(hours=1))["fields"]["u10"]["value"]
        )
        for key, value in [
            ("cycle_utc", "2026-01-01T00:00:00+00:00"),
            ("grid", "other"),
            ("sample_latitude", 41),
            ("valid_utc", TIME.isoformat()),
        ]:
            rows = [row(), row(TIME + timedelta(hours=3))]
            rows[1]["fields"]["u10"]["provenance"][key] = value
            self.assertIsNone(
                sample_time(rows, TIME + timedelta(hours=1))["fields"]["u10"]["value"]
            )

    def test_duplicate_native_time_rejected(self):
        with self.assertRaises(ValueError):
            sample_time([row(), row()], TIME)

    def test_missing_first_middle_last_slots_retained_across_year(self):
        expected = [0, 3, 6, 9]
        for missing in [0, 3, 9]:
            data = {
                "cycle_utc": TIME.isoformat(),
                "start_utc": TIME.isoformat(),
                "end_utc": (TIME + timedelta(hours=9)).isoformat(),
                "expected_steps": expected,
                "rows": [
                    row(TIME + timedelta(hours=n)) for n in expected if n != missing
                ],
                "latitude": 40,
                "longitude": -100,
            }
            enrich(data)
            self.assertEqual(
                [r["valid_utc"] for r in data["rows"]],
                [(TIME + timedelta(hours=n)).isoformat() for n in expected],
            )
            self.assertEqual(
                data["rows"][expected.index(missing)]["fields"]["u10"]["status"],
                "missing",
            )
            text = build_draft(dict(data, cycle_utc=TIME.isoformat()), "Rollover")
            self.assertNotIn("FM010600", text)
            self.assertIn("FM010000", text)
            self.assertTrue(data["window_rows"][-1]["support_only"])

    def test_negative_model_elevation_and_unknown_are_distinct(self):
        for z, expected in [(-98.0665, -10), (None, None)]:
            data = {
                "cycle_utc": TIME.isoformat(),
                "start_utc": TIME.isoformat(),
                "end_utc": (TIME + timedelta(hours=1)).isoformat(),
                "expected_steps": [0, 3],
                "rows": [row(), row(TIME + timedelta(hours=3))],
                "orography": field("surface_geopotential", z, "m2/s2")
                if z is not None
                else {},
            }
            enrich(data)
            if expected is None:
                self.assertIsNone(data["model_elevation"]["value"])
            else:
                self.assertAlmostEqual(data["model_elevation"]["value"], expected)

    def test_profile_time_interpolation_checks_surface_at_both_sources(self):
        a, b = row(), row(TIME + timedelta(hours=3))
        a["fields"].update(profile(fl=10, valid=TIME))
        b["fields"].update(profile(fl=10, sp=99000, valid=TIME + timedelta(hours=3)))
        result = sample_time([a, b], TIME + timedelta(hours=1))
        self.assertIsNone(result["fields"]["u_1000"]["value"])
        self.assertEqual(
            result["fields"]["u_1000"]["reason"], "time_profile_surface_check_failed"
        )


class FlightLevelTests(unittest.TestCase):
    def test_retained_pressure_messages_redecode_and_reject_wrong_levels(self):
        root = Path(__file__).parent / "fixtures/pr3"
        for sample in json.loads((root / "pressure-messages.json").read_text()):
            payload = (root / sample["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), sample["sha256"])
            spec = pressure_spec(sample["parameter"], sample["level_hpa"])
            result = decode_message(
                payload,
                spec,
                utc(sample["cycle_utc"]),
                sample["step"],
                sample["latitude"],
                sample["longitude"],
            )
            self.assertEqual(result.value, sample["value"])
            self.assertEqual(result.provenance["metadata"], sample["metadata"])
            wrong = 850 if sample["level_hpa"] == 925 else 150
            with self.assertRaises(DataFault):
                decode_message(
                    payload,
                    pressure_spec(sample["parameter"], wrong),
                    utc(sample["cycle_utc"]),
                    sample["step"],
                    sample["latitude"],
                    sample["longitude"],
                )

    def test_standard_atmosphere_table_pressures(self):
        # NOAA USSA 1976 Table IV: geopotential feet; hPa, rounded at source.
        reference = json.loads(
            (
                Path(__file__).parent / "fixtures/pr3/standard-atmosphere.json"
            ).read_text()
        )
        for item in reference["values"]:
            self.assertAlmostEqual(
                flight_level_pressure(item["flight_level"]) / 100,
                item["pressure_hpa"],
                delta=reference["maximum_absolute_difference_hpa"],
            )
        self.assertEqual(pressure_brackets(30), (850, 925))
        self.assertEqual(pressure_brackets(450), (100, 150))
        for invalid in [0, 451, 30.0, True]:
            with self.assertRaises(ValueError):
                flight_level_pressure(invalid)

    def test_log_pressure_uv_result_with_independent_weight(self):
        fl = 30
        result = flight_level_wind(profile(fl), fl, TIME)
        # Independently evaluated reference, checked from NOAA FL030 pressure.
        w = math.log(90811 / 85000) / math.log(925 / 850)
        self.assertAlmostEqual(result["u"], -10 + 20 * w, delta=0.002)
        self.assertAlmostEqual(result["v"], -20 - 20 * w, delta=0.002)
        self.assertEqual(result["status"], "derived")
        self.assertEqual(
            result["provenance"]["vertical_reference"], "standard_pressure_altitude"
        )
        self.assertEqual(len(result["provenance"]["sources"]), 5)

    def test_below_surface_bracket_and_unknown_are_explicit(self):
        for sp, reason in [
            (None, "surface_pressure_unknown"),
            (90000, "target_below_model_surface"),
            (92000, "bracket_below_model_surface"),
            (0, "surface_pressure_unknown"),
        ]:
            result = flight_level_wind(profile(sp=sp), 30, TIME)
            self.assertEqual(result["reason"], reason)
            self.assertIsNone(result["u"])

    def test_missing_wrong_unit_nan_metadata_or_time_never_yield_wind(self):
        for mutate in [
            lambda f: f["u_850"].update(value=None),
            lambda f: f["v_925"].update(unit="kt"),
            lambda f: f["u_925"].update(value=float("nan")),
            lambda f: f["u_925"]["provenance"].update(cycle_utc="wrong"),
            lambda f: f["u_925"]["provenance"].update(valid_utc=None),
            lambda f: f["u_925"]["provenance"].update(sample_longitude=0),
        ]:
            fields = profile()
            mutate(fields)
            self.assertIsNone(flight_level_wind(fields, 30, TIME)["u"])

    def test_vertical_north_crossing(self):
        fields = profile()
        for p, direction in [(850, 350), (925, 10)]:
            fields[f"u_{p}"]["value"] = -10 * math.sin(math.radians(direction))
            fields[f"v_{p}"]["value"] = -10 * math.cos(math.radians(direction))
        result = flight_level_wind(fields, 30, TIME)
        self.assertLess(abs(math.degrees(math.atan2(-result["u"], -result["v"]))), 11)


class DomainTests(unittest.TestCase):
    def test_all_twelve_frozen_locations_using_production_classifier(self):
        cases = json.loads(
            (
                Path(__file__).parent / "fixtures/phase0/domain/test-locations.json"
            ).read_text()
        )["locations"]
        for case in cases:
            result = classify(case["latitude"], case["longitude"])
            self.assertEqual(
                result["classification"], case["expected_routing"], case["id"]
            )
            self.assertEqual(
                sorted(m["name"] for m in result["aawu"]), sorted(case["aawu_firs"])
            )
            self.assertFalse(result["automatic_service_assignment"])

    def test_dateline_equivalence_and_arctic_pole(self):
        for lon in [-180, 180]:
            self.assertEqual(classify(40, lon)["classification"], "AWC")
            self.assertEqual(classify(30, lon)["classification"], "boundary_review")
        self.assertEqual(classify(85, -160)["classification"], "AAWU")
        for lon in [-180, -160, 0, 180]:
            self.assertEqual(classify(90, lon)["classification"], "boundary_review")
        self.assertEqual(classify(-90, 0)["classification"], "other_office_review")

    def test_holes_hole_edges_and_shifted_longitudes(self):
        features = [
            {
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[170, 0], [-170, 0], [-170, 20], [170, 20], [170, 0]],
                        [[175, 5], [-175, 5], [-175, 15], [175, 15], [175, 5]],
                    ],
                },
            }
        ]
        self.assertEqual(regions(features, 10, 180), [])
        self.assertEqual(regions(features, 10, -180), [])
        self.assertEqual(regions(features, 2, -179)[0]["relation"], "inside")
        self.assertEqual(regions(features, 5, 180)[0]["relation"], "boundary")
        self.assertEqual(regions(features, 10, 0), [])

    def test_conflict_is_not_assigned_to_either_office(self):
        features = [
            {
                "properties": {},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]],
                },
            }
        ]
        with patch(
            "weather.domain.load_geometry",
            return_value={
                "awc_sigmet_area.json": features,
                "aawu-firs.geojson": features,
            },
        ):
            self.assertEqual(classify(5, 5)["classification"], "conflict_review")

    def test_missing_corrupt_geometry_unknown_and_map_still_shows_point(self):
        for error in [OSError(), ValueError("bad snapshot")]:
            with patch("weather.domain.load_geometry", side_effect=error):
                self.assertEqual(classify(40, -100)["classification"], "unknown_review")
                self.assertTrue(location_map(40, -100).startswith("iVBOR"))
        load_geometry.cache_clear()
        with patch("weather.domain.ROOT", Path("/nonexistent-autospot-domain")):
            self.assertEqual(classify(40, -100)["classification"], "unknown_review")
        load_geometry.cache_clear()

    def test_hash_failure_never_becomes_inside(self):
        load_geometry.cache_clear()
        with patch("weather.domain.HASHES", {"awc_sigmet_area.json": "wrong"}):
            self.assertEqual(classify(40, -100)["classification"], "unknown_review")
        load_geometry.cache_clear()


class IntegratedTests(unittest.TestCase):
    def test_optional_profiles_acquired_and_same_run_time_interpolated(self):
        request = replace(
            forecast(RUN + timedelta(hours=13), 2),
            flight_levels=(30, 450),
            domain_disposition="awc_coordinated",
        )

        def decode(*args):
            f = synthetic_decode(*args)
            if f.name == "surface_pressure":
                f.value = 101325
            return f

        data = Pipeline(FakeTransport(), decoder=decode).fetch(request, now=RUN)
        self.assertEqual(data["requested_flight_levels"], [30, 450])
        first = data["window_rows"][0]
        self.assertEqual(first["valid_utc"], request.start.isoformat())
        self.assertTrue(all(f["status"] == "derived" for f in first["flight_levels"]))
        self.assertEqual(data["domain"]["disposition"], "awc_coordinated")
        self.assertEqual(data["domain"]["classification"], "other_office_review")
        self.assertTrue(chart(data).startswith("iVBOR"))
        text = build_draft(data, "Integrated")
        self.assertIn("FL450:", text)
        self.assertNotIn("FM091500", text)
        # Exercise real Jinja review rendering with data, map, chart and text.
        with tempfile.TemporaryDirectory() as folder:
            application = app.create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "s" * 64,
                    "APP_PASSWORD": "test",
                    "SECURITY_DB_PATH": folder + "/security.sqlite3",
                }
            )
            with application.test_request_context("/"):
                from flask import render_template

                html = render_template(
                    "review.html",
                    report=text,
                    data=data,
                    chart="",
                    guidance=[],
                    location_map=location_map(data["latitude"], data["longitude"]),
                    wind_text=wind,
                    wind_components=__import__(
                        "weather.draft", fromlist=["wind_components"]
                    ).wind_components,
                )
                self.assertIn("147.", html)
                self.assertIn("end support only", html)
                self.assertIn("2026-11-09T12:00:00+00:00", html)

    def test_missing_requested_profile_is_visible_not_silently_dropped(self):
        service = FakeTransport()
        for p in PROVIDERS:
            service.missing.add((p, RUN, 12, "u"))
        data = Pipeline(service, decoder=synthetic_decode).fetch(
            replace(forecast(duration=1), flight_levels=(30,)), now=RUN
        )
        self.assertFalse(data["core_inventory_complete"])
        self.assertEqual(
            data["window_rows"][0]["flight_levels"][0]["status"], "missing"
        )

    def test_request_levels_and_budget_before_network(self):
        form = MultiDict(
            {
                "latitude": "40",
                "longitude": "-100",
                "start_date": "2026-12-31",
                "start_hour": "21",
                "duration": "24",
                "flight_levels": "FL030, 450",
                "domain_disposition": "awc_coordinated",
            }
        )
        result = parse_forecast_request(form, TIME)
        self.assertEqual(result.flight_levels, (30, 450))
        for levels in [
            "FL030,030",
            "MSL030",
            "nan",
            "FL451",
            ",".join(str(i) for i in range(10, 23)),
        ]:
            with self.assertRaises(RequestValidationError):
                parse_forecast_request(
                    MultiDict(dict(form, flight_levels=levels)), TIME
                )
        with self.assertRaises(RequestValidationError):
            parse_forecast_request(
                MultiDict(
                    dict(
                        form,
                        flight_levels="30,60,90,120,180,240,300,340,390",
                        duration="72",
                    )
                ),
                TIME,
            )
        service = FakeTransport()
        with self.assertRaises(DataFault) as caught:
            Pipeline(service, decoder=synthetic_decode).fetch(
                replace(
                    forecast(duration=72),
                    flight_levels=(30, 60, 90, 120, 180, 240, 300, 340, 390),
                ),
                now=RUN,
            )
        self.assertEqual(caught.exception.code, "task_budget")
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()

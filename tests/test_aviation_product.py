"""Aviation normalization, explicit manual completion and representative raw weather."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest

from weather.aviation import (
    ElementError,
    wind_components,
    normalize_wind,
    normalize_visibility,
    normalize_weather,
    normalize_sky,
    precipitation_context,
    gust_context,
)
from weather.coordinates import enrich, utc
from weather.draft import build_draft
from weather.fields import decode_message, SPECS
from weather.product import (
    initialize_product,
    apply_elements,
    prevailing_rows,
    format_prevailing,
    editor_rows,
)
from test_coordinates_domain import TIME, field, row

ROOT = Path(__file__).parent / "fixtures/pr4"


def data(duration=9):
    native = [row(TIME + timedelta(hours=i)) for i in range(0, duration + 3, 3)]
    return enrich(
        {
            "cycle_utc": TIME.isoformat(),
            "start_utc": TIME.isoformat(),
            "end_utc": (TIME + timedelta(hours=duration)).isoformat(),
            "expected_steps": list(range(0, duration + 3, 3)),
            "rows": native,
            "latitude": 40,
            "longitude": -100,
        }
    )


def entries(product, **changes):
    return [
        dict(
            wind="23020KT",
            wind_reviewed=True,
            visibility="P6SM",
            weather="NONE",
            sky="SKC",
            **changes,
        )
        if not changes
        else {
            "wind": "23020KT",
            "wind_reviewed": True,
            "visibility": "P6SM",
            "weather": "NONE",
            "sky": "SKC",
            **changes,
        }
        for _ in product["rows"]
    ]


def reviewed(d=None, **changes):
    d = d or data()
    p = initialize_product(d)
    return d, apply_elements(p, entries(p, **changes), [], d["start_utc"], d["end_utc"])


class NormalizationTests(unittest.TestCase):
    def test_wind_north_calm_half_knots_and_three_digits(self):
        self.assertEqual(wind_components(0, 0), "00000KT")
        self.assertEqual(wind_components(0, -0.5 * 1852 / 3600), "36001KT")
        self.assertEqual(wind_components(0.0001, -10), "36019KT")
        self.assertEqual(normalize_wind("080100G140KT"), "080100G140KT")
        self.assertEqual(normalize_wind("vrb03kt"), "VRB03KT")
        for invalid in [
            "00005KT",
            "36000KT",
            "00000G10KT",
            "27020G15KT",
            "27120KT",
            "36120KT",
            "NAN",
            "<script>",
        ]:
            with self.subTest(value=invalid), self.assertRaises(ElementError):
                normalize_wind(invalid)
        self.assertIsNone(wind_components(float("nan"), 0))

    def test_visibility_exact_boundaries_and_flooring(self):
        cases = {
            ".75": "3/4SM",
            "1.5": "1 1/2SM",
            "2.5": "2SM",
            "6": "6SM",
            "6.01": "P6SM",
            "P6SM": "P6SM",
            ".2499": "0SM",
            ".25": "1/4SM",
            ".5": "1/2SM",
            "1.49": "1SM",
            "1 1/4SM": "1SM",
            "1 1/2SM": "1 1/2SM",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_visibility(raw)[0], expected)
        self.assertIn("rounded down", normalize_visibility("2.5")[1])
        for raw in ["-1", "NaN", "Inf", "1/0SM", "1000", "<img>"]:
            with self.assertRaises(ElementError):
                normalize_visibility(raw)
        self.assertEqual(normalize_visibility(""), (None, None))

    def test_weather_combines_independent_precipitation_and_obscuration(self):
        for raw, expected in [
            ("RA FG", ["RA", "FG"]),
            ("-RA BR", ["-RA", "BR"]),
            ("-SHRA BR", ["-SHRA", "BR"]),
            ("-FZRA", ["-FZRA"]),
            ("-FZDZ", ["-FZDZ"]),
            ("-RASN", ["-RASN"]),
            ("PL BR", ["PL", "BR"]),
            ("+TSRA BR", ["+TSRA", "BR"]),
            ("NONE", []),
        ]:
            self.assertEqual(normalize_weather(raw), expected)
        self.assertIsNone(normalize_weather(""))
        for raw in ["NONE RA", "UP", "PROB30", "-FZSN", "<script>", "RA RA"]:
            with self.assertRaises(ElementError):
                normalize_weather(raw)

    def test_ceiling_is_distinct_from_cloud_base_and_unknown(self):
        for raw in ["FEW010", "SCT020", "FEW010 SCT020", "SKC"]:
            result = normalize_sky(raw)
            self.assertEqual(result["ceiling_state"], "no_ceiling")
            self.assertIsNone(result["ceiling_ft_agl"])
        result = normalize_sky("SCT010 BKN020 OVC050")
        self.assertEqual(
            (result["lowest_base_ft_agl"], result["ceiling_ft_agl"]), (1000, 2000)
        )
        self.assertEqual(normalize_sky("VV003")["ceiling_ft_agl"], 300)
        self.assertEqual(normalize_sky("VV///")["ceiling_state"], "unknown")
        self.assertIsNone(normalize_sky(""))
        for raw in [
            "SKC BKN030",
            "BKN",
            "BKN///",
            "VV003 BKN030",
            "FEW010 FEW010",
            "<svg>",
        ]:
            with self.assertRaises(ElementError):
                normalize_sky(raw)


class ModelContextTests(unittest.TestCase):
    def precipitation(self, code, rate):
        return {
            "precip_type": field("precip_type", code, "code_4.201"),
            "precip_rate": field("precip_rate", rate / 3600, "kg/m2/s"),
        }

    def test_rain_rate_thresholds_not_legacy_point5_and_four_mm(self):
        for rate, expected in [
            (1e-9, "-RA"),
            (0.5, "-RA"),
            (2.54, "-RA"),
            (2.54001, "RA"),
            (4, "RA"),
            (7.62, "RA"),
            (7.62001, "+RA"),
        ]:
            self.assertEqual(
                precipitation_context(self.precipitation(1, rate), TIME)["suggestion"],
                expected,
            )

    def test_snow_and_freezing_types_never_become_rain_or_SWE_intensity(self):
        for code, phase in [
            (3, "FZRA"),
            (5, "SN"),
            (6, "SN"),
            (7, "RASN"),
            (8, "PL"),
            (12, "FZDZ"),
        ]:
            context = precipitation_context(self.precipitation(code, 10), TIME)
            self.assertEqual(context["phase_code"], phase)
            self.assertIsNone(context["suggestion"])

    def test_missing_wrong_code_units_grid_cycle_or_time_remain_unresolved(self):
        for code in [2, 4, 9, 10, 11, 13, 255]:
            result = precipitation_context(self.precipitation(code, 1), TIME)
            self.assertEqual(result["status"], "unsupported")
            self.assertIsNone(result["suggestion"])
        for mutate in [
            lambda f: f["precip_rate"].update(value=None),
            lambda f: f["precip_rate"].update(unit="mm"),
            lambda f: f["precip_type"]["provenance"].update(cycle_utc="other"),
            lambda f: f["precip_rate"]["provenance"].update(sample_latitude=41),
            lambda f: f["precip_type"]["provenance"].update(valid_utc=None),
        ]:
            fields = self.precipitation(1, 1)
            mutate(fields)
            self.assertIsNone(precipitation_context(fields, TIME)["suggestion"])

    def test_model_dry_is_not_none_weather_and_conflicts_do_not_default(self):
        self.assertIsNone(
            precipitation_context(self.precipitation(0, 0), TIME)["suggestion"]
        )
        for code, rate in [(0, 1), (1, 0), (5, 0)]:
            result = precipitation_context(self.precipitation(code, rate), TIME)
            self.assertEqual(result["status"], "invalid")
            self.assertIsNone(result["suggestion"])

    def test_gust_maximum_keeps_actual_preceding_interval(self):
        gust = field(
            "gust10",
            20,
            interval_start_utc=(TIME - timedelta(hours=1)).isoformat(),
            interval_end_utc=TIME.isoformat(),
        )
        result = gust_context({"gust10": gust}, TIME)
        self.assertAlmostEqual(result["peak_kt"], 20 * 3600 / 1852)
        self.assertEqual(
            result["interval_start_utc"], (TIME - timedelta(hours=1)).isoformat()
        )
        self.assertIsNone(
            gust_context({"gust10": gust}, TIME + timedelta(hours=1))["peak_kt"]
        )
        gust["provenance"]["interval_end_utc"] = (TIME + timedelta(hours=1)).isoformat()
        self.assertIsNone(gust_context({"gust10": gust}, TIME)["peak_kt"])

    def test_all_actual_nonzero_IFS_categories_redecode_from_frozen_bytes(self):
        capture = json.loads((ROOT / "nonzero-precipitation.json").read_text())
        payloads = {
            param: (ROOT / info["path"]).read_bytes()
            for param, info in capture["sources"].items()
        }
        for param, info in capture["sources"].items():
            self.assertEqual(
                hashlib.sha256(payloads[param]).hexdigest(), info["sha256"]
            )
        self.assertEqual(
            {s["code"] for s in capture["samples"]}, {1, 3, 5, 6, 7, 8, 12}
        )
        for sample in capture["samples"]:
            fields = {}
            for param, payload in payloads.items():
                spec = next(s for s in SPECS if s.param == param)
                f = decode_message(
                    payload,
                    spec,
                    utc(capture["cycle_utc"]),
                    capture["step"],
                    sample["latitude"],
                    sample["longitude"],
                ).as_dict()
                self.assertEqual(f["value"], sample["fields"][spec.name]["value"])
                fields[spec.name] = f
            context = precipitation_context(fields, capture["valid_utc"])
            self.assertEqual(context["status"], "derived")
            self.assertEqual(int(fields["precip_type"]["value"]), sample["code"])
            if sample["code"] != 1:
                self.assertIsNone(context["suggestion"])


class ProductTests(unittest.TestCase):
    def test_initial_product_preserves_missing_elements_and_no_automatic_weather(self):
        d = data()
        p = initialize_product(d)
        text = build_draft(d, "Initial", p)
        for token in [
            "[VISIBILITY REQUIRED]",
            "[SKY REQUIRED]",
            "[WEATHER REVIEW REQUIRED]",
            "[WIND/GUST REVIEW REQUIRED]",
        ]:
            self.assertIn(token, text)
        for token in ["P6SM", "OVC025", "TSRA", "PROB30"]:
            self.assertNotIn(token, text)
        self.assertFalse(p["complete"])
        self.assertEqual(len(prevailing_rows(p)), 3)

    def test_common_manual_conditions_can_compress_complete_identical_rows(self):
        d, p = reviewed()
        self.assertTrue(p["complete"])
        self.assertEqual(len(prevailing_rows(p)), 1)
        self.assertIn("FM312100 23020KT P6SM SKC", build_draft(d, "Reviewed", p))
        self.assertEqual(p["rows"][0]["weather"]["value"], [])
        self.assertEqual(p["rows"][0]["weather"]["status"], "manual")
        self.assertEqual(initialize_product(d)["rows"][0]["weather"]["value"], None)

    def test_clear_ceiling_transition_and_gust_onset_change_cessation_survive(self):
        d = data(15)
        p = initialize_product(d)
        e = entries(p)
        e[1]["sky"] = "OVC025"
        e[2].update(sky="OVC025", wind="23020G30KT")
        e[3].update(sky="OVC025", wind="23020G40KT")
        e[4].update(sky="OVC025", wind="23020KT")
        p = apply_elements(p, e, [], d["start_utc"], d["end_utc"])
        self.assertEqual(len(prevailing_rows(p)), 5)
        text = build_draft(d, "Changes", p)
        for token in [
            "FM010000 23020KT P6SM OVC025",
            "FM010300 23020G30KT",
            "FM010600 23020G40KT",
            "FM010900 23020KT",
        ]:
            self.assertIn(token, text)
        self.assertNotIn("FM011200", text)

    def test_missing_middle_is_not_inherited_or_suppressed(self):
        d, p = reviewed()
        p["rows"][1]["visibility"].update(value=None, status="missing", reviewed=False)
        self.assertEqual(len(prevailing_rows(p)), 3)
        self.assertIn(
            "FM010000 23020KT [VISIBILITY REQUIRED]", build_draft(d, "Gap", p)
        )

    def test_cloud_fractions_remain_independent_and_cape_does_not_generate_TS(self):
        d = data()
        for r in d["window_rows"]:
            r["fields"].update(
                {
                    k: field(k, v, "fraction", valid=utc(r["valid_utc"]))
                    for k, v in [("tcc", 0.5), ("lcc", 0.7), ("mcc", 0.4), ("hcc", 0.3)]
                }
            )
            r["fields"]["cape"] = field("cape", 5000, "J/kg", valid=utc(r["valid_utc"]))
        p = initialize_product(d)
        self.assertEqual(
            p["rows"][0]["context"]["cloud_fractions"],
            {"tcc": 0.5, "lcc": 0.7, "mcc": 0.4, "hcc": 0.3},
        )
        self.assertIsNone(p["rows"][0]["sky"]["value"])
        self.assertNotIn("TSRA", build_draft(d, "No diagnosis", p))

    def test_fog_rain_and_unrestricted_vis_manual_changes_keep_provenance(self):
        d, p = reviewed(visibility=".75", weather="RA FG", sky="BKN002")
        self.assertIn("3/4SM RA FG BKN002", build_draft(d, "Fog and rain", p))
        for name in ["visibility", "weather", "sky", "wind"]:
            self.assertEqual(p["rows"][0][name]["status"], "manual")
            self.assertIn("original", p["rows"][0][name]["provenance"])
        self.assertTrue(p["complete"])

    def test_rounding_warning_and_unknown_ceiling_remain_visible(self):
        d, p = reviewed(visibility="2.5", weather="BR", sky="VV///")
        self.assertFalse(p["complete"])
        self.assertIn("sky", p["unresolved"][0]["elements"])
        self.assertIn("rounded down to 2SM", p["rows"][0]["notes"][0])

    def test_invalid_later_row_does_not_mutate_original_product(self):
        d = data()
        p = initialize_product(d)
        before = deepcopy(p)
        e = entries(p)
        e[-1]["sky"] = "BOGUS"
        with self.assertRaises(ElementError):
            apply_elements(p, e, [], d["start_utc"], d["end_utc"])
        self.assertEqual(p, before)

    def test_full_72_hours_month_rollover_and_no_expiration_FM(self):
        d = data(72)
        p = initialize_product(d)
        self.assertEqual(len(p["rows"]), 24)
        self.assertEqual(p["rows"][-1]["valid_utc"], "2027-01-03T18:00:00+00:00")
        self.assertNotIn("FM032100", build_draft(d, "72 hour", p))
        self.assertIn(
            "Valid 2026-12-31 21Z to 2027-01-03 21Z", build_draft(d, "72 hour", p)
        )

    def test_editor_keeps_none_distinct_from_blank_weather(self):
        d, p = reviewed()
        self.assertEqual(editor_rows(p)[0]["weather_input"], "NONE")
        self.assertEqual(editor_rows(initialize_product(d))[0]["weather_input"], "")

    def test_conditional_group_splits_at_FM_without_extending_period(self):
        d = data()
        p = initialize_product(d)
        e = entries(p)
        e[1]["sky"] = "BKN030"
        groups = [
            {
                "kind": "TEMPO",
                "start": "2026-12-31T22:00:00Z",
                "end": "2027-01-01T02:00:00Z",
                "visibility": "1.5",
                "weather": "-TSRA BR",
                "sky": "OVC010CB",
            }
        ]
        p = apply_elements(p, e, groups, d["start_utc"], d["end_utc"])
        lines = format_prevailing(p, d["end_utc"])
        self.assertEqual(
            lines,
            [
                "FM312100 23020KT P6SM SKC",
                "  TEMPO 3122/0100 1 1/2SM -TSRA BR OVC010CB",
                "FM010000 23020KT P6SM BKN030",
                "  TEMPO 0100/0102 1 1/2SM -TSRA BR OVC010CB",
                "FM010300 23020KT P6SM SKC",
            ],
        )
        self.assertEqual(p["groups"][0]["start"], "2026-12-31T22:00:00+00:00")

    def test_probability_requires_explicit_manual_group_and_period(self):
        d, p = reviewed()
        e = entries(p)
        group = {
            "kind": "PROB30",
            "start": "2027-01-01T02:00:00Z",
            "end": d["end_utc"],
            "weather": "TSRA",
        }
        p = apply_elements(p, e, [group], d["start_utc"], d["end_utc"])
        self.assertIn("PROB30 0102/0106 TSRA", build_draft(d, "Manual probability", p))
        for change in [
            {"start": d["end_utc"]},
            {"end": "2027-01-01T07:00:00Z"},
            {"start": "2026-12-31T21:30:00Z"},
            {"kind": "PROB80"},
            {"weather": "", "wind": "", "sky": "", "visibility": ""},
        ]:
            with self.assertRaises(ElementError):
                apply_elements(
                    p, e, [dict(group, **change)], d["start_utc"], d["end_utc"]
                )


if __name__ == "__main__":
    unittest.main()

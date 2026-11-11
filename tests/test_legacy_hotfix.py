from datetime import datetime, timedelta, timezone
import re
import unittest
from unittest.mock import patch

import app as legacy


class LegacyHotfixTests(unittest.TestCase):
    def setUp(self):
        self.stack = patch.multiple(legacy, APP_PASSWORD="Météo")
        self.stack.start()
        self.addCleanup(self.stack.stop)
        secret = patch.dict(legacy.app.config, SECRET_KEY="local-test-key")
        secret.start()
        self.addCleanup(secret.stop)
        self.client = legacy.app.test_client()

    def token(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def login(self):
        self.client.get("/login")
        result = self.client.post(
            "/login", data={"password": "Météo", "csrf_token": self.token()}
        )
        self.assertEqual(result.status_code, 302)
        self.client.get("/")

    def form(self, **changes):
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        return {
            "csrf_token": self.token(),
            "model_choice": "GFS",
            "latitude": "24.71",
            "longitude": "-77.76",
            "start_date": start.strftime("%Y-%m-%d"),
            "start_hour": start.strftime("%H"),
            "duration": "12",
            "sar_name": "Weather & SAR",
            **changes,
        }

    def test_gfs_only_request_routing(self):
        self.login()
        page = self.client.get("/")
        self.assertIn("Model: GFS", page.text)
        self.assertNotIn("value='ECMWF'", page.text)
        with patch.object(legacy, "submit_gfs", return_value="GFS called") as gfs:
            result = self.client.post("/submit", data=self.form())
            self.assertEqual(result.text, "GFS called")
            self.assertEqual(gfs.call_args.args[0].model, "GFS")
            self.assertEqual(self.client.post("/submit", data=self.form(model_choice="ECMWF")).status_code, 400)
            self.assertEqual(self.client.post("/submit", data=self.form(latitude="nan")).status_code, 400)
            self.assertEqual(gfs.call_count, 1)

    def test_editor_escapes_report_and_downloads_exact_text(self):
        self.login()
        with patch.object(legacy, "submit_gfs", side_effect=lambda req: legacy.render_report(f"SAR: {req.label}", "")):
            report = self.client.post("/submit", data=self.form())
        self.assertEqual(report.status_code, 200)
        self.assertIn("SAR: Weather &amp; SAR", report.text)
        self.assertIn("Approve &amp; Save Report", report.text)
        self.assertTrue(re.search(r'name="taf_content"', report.text))

        body = "Forecaster edits — Météo 🙂"
        result = self.client.post(
            "/transmit", data={"csrf_token": self.token(), "taf_content": body}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data, body.encode())
        self.assertIn("SPOT_TAF_", result.headers["Content-Disposition"])
        self.assertEqual(
            self.client.post("/transmit", data={"taf_content": body}).status_code, 400
        )

    def test_missing_gfs_values_stay_unresolved(self):
        engine = legacy.FirstGuessTAF("test", 40, -100, [], {}, "GFS", 12, None)
        row = {
            "wdir": 180,
            "wspd": 10,
            "gust": 0,
            "vis": None,
            "prate": None,
            "ceiling": None,
            "is_conv": False,
            "tcc": 50,
        }
        report = engine.process_hour(row)
        self.assertIn("[VISIBILITY REQUIRED]", report)
        self.assertIn("[WEATHER REVIEW REQUIRED]", report)
        self.assertIn("[SKY REQUIRED]", report)

    def test_gfs_cloud_ceiling_is_used_without_invented_layer_bases(self):
        self.assertEqual(legacy.GFS_FIELDS["ceiling"], ":HGT:cloud ceiling:")
        self.assertEqual(
            legacy.locate_bytes(["1:0:d:VIS:surface:", "2:200:d:HGT:cloud ceiling:", "3:400:d:LCDC:low cloud layer:"], legacy.GFS_FIELDS["ceiling"]),
            (200, 399),
        )
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(75, 850), "BKN008")
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(40, 850), "[SKY REQUIRED]")
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(75, None), "[SKY REQUIRED]")
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(0.7, None), "SKC")
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(0, None), "SKC")
        self.assertEqual(legacy.AviationTranslator.evaluate_clouds(6.25, None), "[SKY REQUIRED]")
        self.assertAlmostEqual(legacy.ceiling_agl_ft(650, 300), 1148.294)
        self.assertIsNone(legacy.ceiling_agl_ft(650, None))
        self.assertIsNone(legacy.ceiling_agl_ft(250, 300))

    def test_gfs_guidance_keeps_missing_layers_distinct_from_clear(self):
        with legacy.app.test_request_context("/"):
            result = legacy.render_report("Editable report", "", gfs_rows=[{
                "valid_utc": "2026-11-11 12:00Z",
                "visibility": 3.2,
                "ceiling": 1148,
                "clouds": (75, None, 0, 20),
            }, {
                "valid_utc": "2026-11-11 15:00Z",
                "visibility": 10,
                "ceiling": None,
                "clouds": (0.7, 0, 0, 0.7),
            }])
        self.assertIn("GFS guidance by valid time", result)
        self.assertIn("1148", result)
        self.assertIn("75 / — / 0 / 20", result)
        self.assertIn("No ceiling (SKC)", result)

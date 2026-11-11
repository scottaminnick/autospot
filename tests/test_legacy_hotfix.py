from datetime import datetime, timedelta, timezone
import re
import unittest
from unittest.mock import patch

import app as legacy
from weather.fields import DataFault


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
            "model_choice": "ECMWF",
            "latitude": "24.71",
            "longitude": "-77.76",
            "start_date": start.strftime("%Y-%m-%d"),
            "start_hour": start.strftime("%H"),
            "duration": "12",
            "sar_name": "Weather & SAR",
            **changes,
        }

    def test_ecmwf_outage_is_explicit_and_gfs_is_still_selectable(self):
        self.login()
        with patch.object(
            legacy, "retrieve_forecast", side_effect=DataFault("deadline")
        ) as fetch:
            rejected = self.client.post("/submit", data=self.form(latitude="nan"))
            self.assertEqual(rejected.status_code, 400)
            fetch.assert_not_called()
            outage = self.client.post("/submit", data=self.form())
            self.assertEqual(outage.status_code, 502)
            self.assertIn("Verified ECMWF guidance was unavailable", outage.text)
        with patch.object(legacy, "submit_gfs", return_value="GFS called") as gfs:
            result = self.client.post("/submit", data=self.form(model_choice="GFS"))
            self.assertEqual(result.text, "GFS called")
            self.assertEqual(gfs.call_args.args[0].model, "GFS")

    def test_editor_escapes_report_and_downloads_exact_text(self):
        self.login()
        with patch.multiple(
            legacy,
            retrieve_forecast=lambda _: {"rows": []},
            chart=lambda _: "abc",
            build_spot_report=lambda _data, label: f"SAR: {label}",
        ):
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
            "cbh": None,
            "is_conv": False,
            "tcc": 50,
        }
        report = engine.process_hour(row)
        self.assertIn("[VISIBILITY REQUIRED]", report)
        self.assertIn("[WEATHER REVIEW REQUIRED]", report)
        self.assertIn("[SKY REQUIRED]", report)

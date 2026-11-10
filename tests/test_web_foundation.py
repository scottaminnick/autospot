"""Security and request-boundary regressions; no live weather requests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import json
import base64
import tempfile
import time
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import app as module
from request_validation import parse_forecast_request, check_work_budget

BASE = "https://localhost"


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class WebFoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {
            "TESTING": True,
            "SECRET_KEY": "s" * 64,
            "APP_PASSWORD": "forecaster-🙂",
            "SECURITY_DB_PATH": str(Path(self.directory.name) / "controls.sqlite3"),
        }
        self.app = module.create_app(self.config)
        self.client = self.app.test_client()
        # Any accidental upstream call fails every test in this class.
        self.network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected network"),
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def get(self, path, **kwargs):
        return self.client.get(path, base_url=BASE, **kwargs)

    def post(self, path, data, **kwargs):
        return self.client.post(path, data=data, base_url=BASE, **kwargs)

    def token(self):
        with self.client.session_transaction(base_url=BASE) as s:
            return s["csrf"]

    def login(self):
        self.assertEqual(self.get("/login").status_code, 200)
        r = self.post(
            "/login",
            {"csrf_token": self.token(), "password": self.config["APP_PASSWORD"]},
        )
        self.assertEqual(r.status_code, 302)
        return r

    def form(self, **changes):
        start = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=1)
        value = {
            "latitude": "24.71",
            "longitude": "-77.76",
            "sar_name": "Test label",
            "model_choice": "ECMWF",
            "start_date": start.strftime("%Y-%m-%d"),
            "start_hour": start.strftime("%H"),
            "duration": "24",
            "csrf_token": self.token(),
        }
        value.update(changes)
        return value

    def test_routes_require_authentication(self):
        for path in ["/", "/submit", "/transmit"]:
            with self.subTest(path=path):
                r = self.get(path) if path == "/" else self.post(path, {})
                self.assertEqual(r.status_code, 302)
                self.assertTrue(r.location.endswith("/login"))

    def test_login_csrf_unicode_and_cookie_flags(self):
        self.get("/login")
        self.assertEqual(self.post("/login", {"password": "🙂"}).status_code, 400)
        self.assertEqual(
            self.post(
                "/login", {"csrf_token": self.token(), "password": "wrong-🙂"}
            ).status_code,
            401,
        )
        previous = self.token()
        r = self.post(
            "/login", {"csrf_token": previous, "password": self.config["APP_PASSWORD"]}
        )
        self.assertEqual(r.status_code, 302)
        self.assertNotEqual(self.token(), previous)
        cookie = r.headers["Set-Cookie"]
        for flag in ["Secure", "HttpOnly", "SameSite=Lax"]:
            self.assertIn(flag, cookie)
        self.assertNotIn(self.config["APP_PASSWORD"], cookie)

    def test_logout_revokes_replayed_cookie_and_requires_post(self):
        self.login()
        saved = self.client.get_cookie("autospot_session").value
        self.assertEqual(self.get("/logout").status_code, 405)
        self.assertEqual(self.post("/logout", {}).status_code, 400)
        self.assertEqual(
            self.post("/logout", {"csrf_token": self.token()}).status_code, 302
        )
        self.client.set_cookie("autospot_session", saved)
        self.assertEqual(self.get("/").status_code, 302)

    def test_password_rotation_and_expiration_revoke_sessions(self):
        self.login()
        self.app.config["APP_PASSWORD"] = "replacement-password"
        self.assertEqual(self.get("/").status_code, 302)
        self.config["APP_PASSWORD"] = "replacement-password"
        self.login()
        controls = self.app.extensions["controls"]
        with controls.connection() as db:
            db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
        self.assertEqual(self.get("/").status_code, 302)

    def test_session_works_across_workers_and_revocation_is_shared(self):
        self.login()
        second = module.create_app(self.config).test_client()
        second.set_cookie(
            "autospot_session", self.client.get_cookie("autospot_session").value
        )
        self.assertEqual(second.get("/", base_url=BASE).status_code, 200)
        self.post("/logout", {"csrf_token": self.token()})
        self.assertEqual(second.get("/", base_url=BASE).status_code, 302)

    def test_login_limits_shared_and_forwarded_header_not_trusted(self):
        self.get("/login")
        for n in range(20):
            r = self.post(
                "/login",
                {"csrf_token": self.token(), "password": "bad"},
                headers={"X-Forwarded-For": f"198.51.100.{n}"},
            )
            self.assertEqual(r.status_code, 401)
        second = module.create_app(self.config).test_client()
        page = second.get("/login", base_url=BASE)
        token = re.search('name="csrf_token" value="([^"]+)"', page.text)[1]
        blocked = second.post(
            "/login", base_url=BASE, data={"csrf_token": token, "password": "bad"}
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)

    def test_rate_reservations_are_atomic_across_connections(self):
        controls = self.app.extensions["controls"]
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(
                executor.map(
                    lambda _: controls.permit([("atomic", "test", 5, 60)]), range(30)
                )
            )
        self.assertEqual(responses.count(0), 5)

    def test_invalid_forecasts_never_start_generation(self):
        self.login()
        invalid = [{"latitude": v} for v in ["nan", "inf", "-inf", "95", "", "abc"]]
        invalid += [
            {"longitude": "500"},
            {"duration": "-1"},
            {"duration": "73"},
            {"duration": "1.5"},
            {"duration": "0"},
            {"model_choice": "unknown"},
            {"start_date": "2026-02-30"},
            {"start_date": "not-a-date"},
            {"start_hour": "24"},
            {"start_hour": "-1"},
            {"start_hour": "00:00"},
            {"start_date": "2000-01-01"},
            {"start_date": "2099-01-01"},
            {"sar_name": "x" * 121},
            {"sar_name": "label\nforged line"},
        ]
        with patch.object(module, "generate_forecast") as generate:
            for changes in invalid:
                with self.subTest(changes=changes):
                    r = self.post("/submit", self.form(**changes))
                    self.assertEqual(r.status_code, 400)
            duplicate = MultiDict(self.form())
            duplicate.add("latitude", "10")
            self.assertEqual(self.post("/submit", duplicate).status_code, 400)
            generate.assert_not_called()

    def test_csrf_missing_wrong_duplicated_and_expired(self):
        self.login()
        with patch.object(module, "generate_forecast") as generate:
            for supplied in ["", "wrong-🙂"]:
                self.assertEqual(
                    self.post("/submit", self.form(csrf_token=supplied)).status_code,
                    400,
                )
            duplicate = MultiDict(self.form())
            duplicate.add("csrf_token", self.token())
            self.assertEqual(self.post("/submit", duplicate).status_code, 400)
            with self.client.session_transaction(base_url=BASE) as s:
                s["csrf_issued"] = time.time() - 7201
            expired = self.token()
            self.assertEqual(self.post("/submit", self.form()).status_code, 400)
            generate.assert_not_called()
        page = self.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotEqual(self.token(), expired)
        with patch.object(
            module, "generate_forecast", return_value=("review draft", "")
        ):
            self.assertEqual(self.post("/submit", self.form()).status_code, 200)

    def test_valid_request_and_html_report_stay_text(self):
        self.login()
        payload = '</textarea><script id="probe">window.probe=1</script><img src=x onerror="probe()">'
        with patch.object(
            module, "generate_forecast", return_value=(payload, "")
        ) as generate:
            response = self.post("/submit", self.form(sar_name="Météo — SAR"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;/textarea&gt;", response.text)
        tags = Tags(response.text).tags
        self.assertNotIn("script", tags)
        self.assertNotIn("img", tags)
        forecast = generate.call_args.args[0]
        self.assertEqual(
            (forecast.latitude, forecast.longitude, forecast.model),
            (24.71, -77.76, "ECMWF"),
        )
        self.assertEqual(forecast.label, "Météo — SAR")
        self.assertEqual(forecast.start.tzinfo, timezone.utc)

    def test_large_bodies_report_bounds_and_utf8_attachment(self):
        self.login()
        self.assertEqual(
            self.post("/submit", self.form(sar_name="x" * 300_000)).status_code, 413
        )
        for text in ["", "  ", "\x00", "é" * 32_001]:
            self.assertEqual(
                self.post(
                    "/transmit", {"csrf_token": self.token(), "taf_content": text}
                ).status_code,
                400,
            )
        text = "SAR: Météo — 救助 🙂\n<script>literal report text</script>\r\n"
        response = self.post(
            "/transmit", {"csrf_token": self.token(), "taf_content": text}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, text.encode("utf-8"))
        self.assertTrue(
            response.headers["Content-Disposition"].startswith("attachment;")
        )
        self.assertTrue(response.content_type.startswith("text/plain"))
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_generation_rate_and_work_budget(self):
        self.login()
        with patch.object(
            module, "generate_forecast", return_value=("sample draft", "")
        ) as generate:
            for _ in range(3):
                self.assertEqual(self.post("/submit", self.form()).status_code, 200)
            self.assertEqual(self.post("/submit", self.form()).status_code, 429)
            self.assertEqual(generate.call_count, 3)
        fields = {
            f"extra{n}": {"GFS": ":X:", "ECMWF": {"param": "x"}} for n in range(50)
        }
        with (
            patch.dict(module.SFC_FIELDS, fields),
            patch.object(module, "generate_forecast") as generate,
        ):
            self.assertEqual(
                self.post("/submit", self.form(duration="72")).status_code, 400
            )
            generate.assert_not_called()

    def test_empty_model_output_is_failure_without_review_form(self):
        self.login()
        with (
            patch.object(
                module, "get_latest_complete_run", return_value=("20261110", "00")
            ),
            patch.object(module, "get_model_variable", return_value=None),
            patch("requests.Session.get") as upstream,
            patch.object(module, "get_usgs_elevation") as elevation,
        ):
            upstream.return_value.status_code = 200
            upstream.return_value.text = ""
            response = self.post("/submit", self.form(duration="1"))
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('name="taf_content"', response.text)
        elevation.assert_not_called()

    def test_generator_and_chart_run_with_frozen_model_samples(self):
        fixture = json.loads(
            (
                Path(__file__).parent
                / "fixtures/phase0/ecmwf-20261109/point-samples.json"
            ).read_text()
        )

        def field(lat, lon, date, cycle, hour, spec, model, connection, cache):
            for sample in fixture["samples"]:
                record = sample["index_record"]
                if sample["metadata"]["endStep"] == hour and all(
                    str(record.get(k)) == str(v) for k, v in spec.items()
                ):
                    return {
                        "data": module.xr.Dataset({"value": sample["nearest"]["value"]})
                    }
            return None

        form = MultiDict(
            {
                "latitude": "24.71",
                "longitude": "-77.76",
                "duration": "1",
                "start_date": "2026-11-09",
                "start_hour": "12",
            }
        )
        forecast = parse_forecast_request(
            form, datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        )
        with (
            patch.object(
                module, "get_latest_complete_run", return_value=("20261109", "00")
            ),
            patch.object(module, "get_model_variable", side_effect=field),
            patch("requests.Session.get") as upstream,
            patch.object(module, "get_usgs_elevation", return_value=0),
        ):
            upstream.return_value.status_code = 200
            upstream.return_value.text = ""
            report, chart = module.generate_forecast(forecast)
        self.assertIn("DATA BASED ON ECMWF 20261109 00Z", report)
        self.assertTrue(base64.b64decode(chart).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_errors_and_logs_do_not_expose_exception_text(self):
        self.login()
        private = "APP_PASSWORD=secret; operation=PRIVATE-OP"
        with (
            patch.object(
                module, "generate_forecast", side_effect=RuntimeError(private)
            ),
            self.assertLogs(self.app.logger, level="ERROR") as logs,
        ):
            response = self.post("/submit", self.form())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(private, response.text)
        self.assertNotIn(private, " ".join(logs.output))
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertIn(response.headers["X-Request-ID"], response.text)

    def test_health_headers_and_configuration_failure(self):
        response = self.get("/healthz", headers={"X-Request-ID": "untrusted"})
        self.assertEqual(response.json, {"status": "ok"})
        self.assertRegex(response.headers["X-Request-ID"], r"^[a-f0-9]{32}$")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(
            "frame-ancestors 'none'", response.headers["Content-Security-Policy"]
        )
        self.assertNotIn("Set-Cookie", response.headers)
        self.app.config["APP_PASSWORD"] = None
        self.assertEqual(self.get("/healthz").status_code, 503)
        self.assertEqual(self.get("/login").status_code, 503)

    def test_utc_rollover_max_duration_and_default_model(self):
        value = MultiDict(
            {
                "latitude": "-90",
                "longitude": "180",
                "start_date": "2027-01-01",
                "start_hour": "00",
                "duration": "72",
            }
        )
        forecast = parse_forecast_request(
            value, datetime(2026, 12, 31, 23, 55, tzinfo=timezone.utc)
        )
        self.assertEqual(forecast.model, "ECMWF")
        self.assertEqual(
            forecast.start + timedelta(hours=forecast.duration),
            datetime(2027, 1, 4, tzinfo=timezone.utc),
        )
        self.assertEqual(check_work_budget(72, 13, 9), 676)


if __name__ == "__main__":
    unittest.main()

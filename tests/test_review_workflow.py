"""Session ownership, stale writes, bounds and real element-form request paths."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

import app as module
from weather.domain import classify
from weather.reviews import ReviewStore, ReviewMissing, ReviewConflict, ReviewFull
from test_aviation_product import data

BASE = "https://localhost"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "reviews.sqlite3"
        self.store = ReviewStore(self.path)

    def test_private_file_and_owner_isolation(self):
        key = self.store.create("alice", {"label": "Météo 🙂"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.get(key, "alice"), (1, {"label": "Météo 🙂"}))
        with self.assertRaises(ReviewMissing):
            self.store.get(key, "bob")
        with self.assertRaises(ReviewMissing):
            self.store.save(key, "bob", 1, {"label": "other"})
        self.assertEqual(self.store.get(key, "alice")[0], 1)

    def test_two_workers_stale_revision_has_one_winner(self):
        key = self.store.create("owner", {"x": 0})

        def save(n):
            try:
                ReviewStore(self.path).save(key, "owner", 1, {"x": n})
                return "ok"
            except ReviewConflict:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(save, [1, 2]))
        self.assertEqual(sorted(result), ["ok", "stale"])
        self.assertEqual(self.store.get(key, "owner")[0], 2)

    def test_expiry_is_creation_based_and_cleanup_occurs_on_access(self):
        with patch("weather.reviews.time.time", return_value=100):
            key = self.store.create("owner", {})
        with patch("weather.reviews.time.time", return_value=200):
            self.store.save(key, "owner", 1, {"x": 1})
        with patch("weather.reviews.time.time", return_value=86501):
            with self.assertRaises(ReviewMissing):
                self.store.get(key, "owner")
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0
            )

    def test_record_total_and_count_bounds_do_not_evict_existing_reviews(self):
        key = self.store.create("owner", {"x": "kept"})
        for settings in [
            dict(MAX_RECORD_BYTES=2),
            dict(MAX_TOTAL_BYTES=5),
            dict(MAX_RECORDS=1),
        ]:
            with patch.multiple("weather.reviews", **settings):
                with self.assertRaises(ReviewFull):
                    self.store.create("other", {"a": "longer"})
            self.assertEqual(self.store.get(key, "owner")[1], {"x": "kept"})
        with patch("weather.reviews.MAX_TOTAL_BYTES", 20):
            with self.assertRaises(ReviewFull):
                self.store.save(key, "owner", 1, {"x": "x" * 30})
        self.assertEqual(self.store.get(key, "owner")[0], 1)


class WebReviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = {
            "TESTING": True,
            "APP_PASSWORD": "review-test",
            "SECRET_KEY": "s" * 64,
            "SECURITY_DB_PATH": str(Path(self.folder.name) / "controls.sqlite3"),
        }
        self.app = module.create_app(self.config)
        quiet = patch.object(self.app.logger, "disabled", True)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.client = self.app.test_client()
        d = data(6)
        d.update(
            grid="0p25",
            domain=classify(40, -100),
            counts={"direct": 1, "missing": 0, "invalid": 0, "unsupported": 3},
        )
        self.guidance = d
        for name in ["chart", "location_map"]:
            mocked = patch.object(module, name, return_value="")
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(module, "retrieve_forecast", return_value=deepcopy(d))
        mocked.start()
        self.addCleanup(mocked.stop)
        network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected network"),
        )
        network.start()
        self.addCleanup(network.stop)
        self.login(self.client)

    def token(self, client):
        with client.session_transaction(base_url=BASE) as session:
            return session["csrf"]

    def login(self, client):
        client.get("/login", base_url=BASE)
        response = client.post(
            "/login",
            base_url=BASE,
            data={"password": "review-test", "csrf_token": self.token(client)},
        )
        self.assertEqual(response.status_code, 302)

    def create(self):
        now = datetime.now(timezone.utc)
        response = self.client.post(
            "/submit",
            base_url=BASE,
            data={
                "csrf_token": self.token(self.client),
                "latitude": "40",
                "longitude": "-100",
                "start_date": now.strftime("%Y-%m-%d"),
                "start_hour": now.strftime("%H"),
                "duration": "6",
                "sar_name": "Météo — reviewed spot",
            },
        )
        self.assertEqual(response.status_code, 303)
        return response.headers["Location"]

    def workspace(self, url):
        with self.client.session_transaction(base_url=BASE) as session:
            owner = session["sid"]
        return self.app.extensions["reviews"].get(url.rsplit("/", 1)[1], owner)

    def form(self, url, **changes):
        revision, w = self.workspace(url)
        form = {
            "csrf_token": self.token(self.client),
            "revision": str(revision),
            "group_count": "1",
            "common_visibility": "P6SM",
            "common_weather": "NONE",
            "common_sky": "SKC",
            "apply_common": "yes",
            "review_all_winds": "yes",
        }
        for i, row in enumerate(w["product"]["rows"]):
            form.update(
                {
                    f"row_{i}_wind": row["wind"]["value"] or "",
                    f"row_{i}_visibility": "",
                    f"row_{i}_weather": "",
                    f"row_{i}_sky": "",
                }
            )
        form.update(changes)
        return form

    def test_generate_owned_workspace_common_completion_and_original_unchanged(self):
        url = self.create()
        before = deepcopy(self.workspace(url)[1]["data"])
        page = self.client.get(url, base_url=BASE)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Rebuild draft from reviewed elements", page.text)
        self.assertIn("common_visibility", page.text)
        response = self.client.post(
            url,
            base_url=BASE,
            data=self.form(url, row_1_sky="OVC025", row_1_wind="23020G35KT"),
        )
        self.assertEqual(response.status_code, 303)
        revision, w = self.workspace(url)
        self.assertEqual(revision, 2)
        self.assertTrue(w["product"]["complete"])
        self.assertEqual(w["data"], before)
        page = self.client.get(url, base_url=BASE)
        self.assertEqual(page.status_code, 200)
        self.assertIn("23020G35KT P6SM OVC025", page.text)
        self.assertIn("Finalize saved version", page.text)
        self.assertNotIn("[VISIBILITY REQUIRED]", page.text)

    def test_conditional_group_ui_datetime_inputs_and_atomic_bad_input(self):
        url = self.create()
        form = self.form(
            url,
            group_0_kind="TEMPO",
            group_0_start="2026-12-31T22:00",
            group_0_end="2027-01-01T02:00",
            group_0_weather="-RA BR",
            group_0_visibility="1.5",
        )
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=form).status_code, 303
        )
        self.assertEqual(
            self.workspace(url)[1]["product"]["groups"][0]["kind"], "TEMPO"
        )
        good = deepcopy(self.workspace(url))
        for changes in [
            dict(row_0_sky="<script>"),
            dict(group_0_kind="PROB99"),
            dict(group_0_kind="TEMPO", group_0_start="bad"),
            dict(common_visibility="NaN"),
        ]:
            form = self.form(url, **changes)
            response = self.client.post(url, base_url=BASE, data=form)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.workspace(url), good)

    def test_owner_csrf_duplicate_fields_and_stale_tab_rejected(self):
        url = self.create()
        other = self.app.test_client()
        self.login(other)
        self.assertEqual(other.get(url, base_url=BASE).status_code, 404)
        self.assertEqual(
            other.post(url, base_url=BASE, data=self.form(url)).status_code, 404
        )
        form = self.form(url)
        form["csrf_token"] = "bad"
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=form).status_code, 400
        )
        duplicate = MultiDict(self.form(url))
        duplicate.add("row_0_sky", "BKN010")
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=duplicate).status_code, 400
        )
        first = self.form(url)
        second = dict(first)
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=first).status_code, 303
        )
        second["common_sky"] = "BKN002"
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=second).status_code, 409
        )
        self.assertEqual(
            self.workspace(url)[1]["product"]["rows"][0]["sky"]["value"]["tokens"],
            ["SKC"],
        )

    def test_revoked_session_cannot_reopen_review(self):
        url = self.create()
        self.client.post(
            "/logout", base_url=BASE, data={"csrf_token": self.token(self.client)}
        )
        self.assertEqual(self.client.get(url, base_url=BASE).status_code, 302)
        self.login(self.client)
        self.assertEqual(self.client.get(url, base_url=BASE).status_code, 404)

    def test_common_conditions_require_explicit_selection_and_wind_acknowledgement(
        self,
    ):
        url = self.create()
        form = self.form(url)
        del form["apply_common"]
        del form["review_all_winds"]
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=form).status_code, 303
        )
        p = self.workspace(url)[1]["product"]
        self.assertFalse(p["complete"])
        self.assertIsNone(p["rows"][0]["sky"]["value"])
        self.assertFalse(p["rows"][0]["wind"]["reviewed"])

    def test_draft_text_download_remains_exact_unicode(self):
        url = self.create()
        self.client.post(url, base_url=BASE, data=self.form(url))
        page = self.client.get(url, base_url=BASE)
        from html import unescape

        text = (
            unescape(re.search(r"<textarea[^>]*>(.*?)</textarea>", page.text, re.S)[1])
            + "\nMétéo 🙂\n"
        )
        response = self.client.post(
            "/transmit",
            base_url=BASE,
            data={"csrf_token": self.token(self.client), "taf_content": text},
        )
        self.assertEqual(response.data, text.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()

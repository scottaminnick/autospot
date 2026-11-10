"""Approval/version coupling, local text grammar and reproducible download contents."""

from copy import deepcopy
import base64
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from weather.aviation import normalize_sky
from weather.charts import ceiling_series, reviewed_chart
from weather.document import (
    body_from_product,
    new_document,
    parse_body,
    preview_text,
    resolve_group_time,
    contributions,
    FinalizationError,
)
from weather.exports import prepare_finalization, archive_package, provenance_record
from test_aviation_product import data, reviewed
from test_coordinates_domain import TIME
import test_review_workflow as review_helpers

BASE = review_helpers.BASE


def workspace():
    d, p = reviewed()
    d.update(
        grid="0p25",
        model="ECMWF_IFS",
        counts={"direct": 3, "unsupported": 9, "missing": 0, "invalid": 0},
    )
    w = {"data": d, "product": p, "label": "Météo — Unicode 🙂"}
    w["document"] = new_document(
        body_from_product(w), 2, "structured", prepared_at=TIME.isoformat()
    )
    return w


def approve(w):
    return prepare_finalization(
        w,
        "draft-test",
        2,
        True,
        "awc_coordinated",
        "AWC service verified with originating office.",
    )


class TextGrammarTests(unittest.TestCase):
    def test_exact_start_full_fm_and_none_weather_decision(self):
        w = workspace()
        p = parse_body(w["document"]["body"], w["data"])
        self.assertEqual(p["rows"][0]["valid_utc"], TIME.isoformat())
        self.assertEqual(p["rows"][0]["weather"], [])
        self.assertEqual(p["rows"][0]["sky"]["ceiling_state"], "no_ceiling")
        self.assertEqual(len(p["rows"]), 1)

    def test_expiration_duplicate_and_wrong_initial_FM_are_rejected(self):
        d = data()
        bad = [
            "FM312200 23020KT P6SM SKC",
            "FM312100 23020KT P6SM SKC\nFM010600 23020KT P6SM SKC",
            "FM312100 23020KT P6SM SKC\nFM312100 24020KT P6SM SKC",
            "FM312100 23020KT P6SM SKC\nFM010300 23020KT P6SM SKC\nFM010000 23020KT P6SM SKC",
        ]
        for text in bad:
            with self.assertRaises(FinalizationError):
                parse_body(text, d)

    def test_placeholders_missing_required_unknown_ceiling_and_nonreportable_vis(self):
        for conditions in [
            "23020KT P6SM",
            "23020KT SKC",
            "P6SM SKC",
            "23020KT [VISIBILITY REQUIRED] SKC",
            "23020KT 2.5SM RA BKN030",
            "23020KT P6SM VV///",
            "23020KT 6SM SKC",
            "23020KT P6SM NONE SKC",
        ]:
            with (
                self.subTest(conditions=conditions),
                self.assertRaises(FinalizationError),
            ):
                parse_body("FM312100 " + conditions, data())

    def test_rain_fog_and_mixed_winter_codes_can_be_approved(self):
        for weather in ["RA FG", "-FZRA BR", "-RASN", "PL BR", "-FZDZ"]:
            p = parse_body(f"FM312100 23020KT 1 1/2SM {weather} BKN003", data())
            self.assertTrue(p["rows"][0]["weather"])

    def test_tempo_and_prob_parent_period_and_inherited_visibility_context(self):
        text = "FM312100 23020KT P6SM SKC\n TEMPO 3122/0100 1SM -RA BR BKN010\nFM010000 23020KT P6SM BKN030\n PROB30 0101/0105 TSRA"
        self.assertEqual(len(parse_body(text, data())["groups"]), 2)
        for bad in [
            text.replace("3122/0100", "3122/0102"),
            text.replace("0101/0105", "0105/0101"),
            text.replace("PROB30", "PROB40"),
            text.replace("TSRA", "1SM"),
        ]:
            with self.assertRaises(FinalizationError):
                parse_body(bad, data())

    def test_72_hour_rollover_and_conditional_hour_24(self):
        d = data(72)
        p = parse_body(
            "FM312100 23020KT P6SM SKC\nTEMPO 3122/3124 3SM RA BKN020\nFM010000 25020KT P6SM SCT020\nFM032000 27020KT P6SM SKC",
            d,
        )
        self.assertEqual(p["groups"][0]["end"], "2027-01-01T00:00:00+00:00")
        self.assertEqual(p["rows"][-1]["valid_utc"], "2027-01-03T20:00:00+00:00")
        with self.assertRaises(FinalizationError):
            resolve_group_time("012460", d["start_utc"], d["end_utc"], minutes=True)

    def test_plain_language_aloft_requires_exact_requested_time_levels(self):
        w = workspace()
        d = w["data"]
        d["requested_flight_levels"] = [30]
        for r in d["window_rows"]:
            r["flight_levels"] = [
                {"flight_level": 30, "status": "derived", "u": -10, "v": 0}
            ]
        body = body_from_product(w)
        self.assertIn("FL030: from 090 degrees true at 19 kt", body)
        p = parse_body(body, d)
        self.assertEqual(len(p["aloft"]), 3)
        for bad in [
            body.replace("FL030", "FL060", 1),
            body + "\n" + body.splitlines()[-1],
            "\n".join(body.splitlines()[:-1]),
            body.replace("from 090 degrees true at 19 kt", "09019KT", 1),
        ]:
            with self.assertRaises(FinalizationError):
                parse_body(bad, d)

    def test_explicit_optional_unavailable_is_retained_not_a_fabricated_wind(self):
        w = workspace()
        w["data"]["requested_flight_levels"] = [30]
        for row in w["data"]["window_rows"]:
            row["flight_levels"] = [
                {
                    "flight_level": 30,
                    "status": "missing",
                    "reason": "bracket_below_model_surface",
                }
            ]
        body = body_from_product(w)
        p = parse_body(body, w["data"])
        self.assertIsNone(p["aloft"][0]["wind"])
        self.assertIn("below model surface", p["aloft"][0]["reason"])

    def test_remarks_keep_unicode_and_unsupported_text_is_not_silently_ignored(self):
        w = workspace()
        body = w["document"]["body"] + "\r\nRemarks: Météo — reviewed 🙂\r\n"
        self.assertEqual(
            parse_body(body, w["data"])["remarks"], ["Météo — reviewed 🙂"]
        )
        with self.assertRaises(FinalizationError):
            parse_body(body + "Injected unparsed line", w["data"])

    def test_new_FM_at_different_time_is_manual_even_if_same_value(self):
        w = workspace()
        p = parse_body(
            "FM312100 23020KT P6SM SKC\nFM312200 23020KT P6SM SKC", w["data"]
        )
        result = contributions(p, w["product"])
        self.assertEqual(
            result["rows"][1]["element_quality"]["wind"]["provenance"]["method"],
            "final_text_entry",
        )


class ExportTests(unittest.TestCase):
    def test_approval_requires_attestation_and_completed_coordination(self):
        for approved, disposition, note in [
            (False, "awc_coordinated", "done"),
            (True, "pending", "done"),
            (True, "aawu_redirect", "done"),
            (True, "awc_coordinated", ""),
        ]:
            with self.assertRaises(FinalizationError):
                prepare_finalization(workspace(), "x", 2, approved, disposition, note)

    def test_snapshot_text_and_zip_have_exact_matching_unicode_hashes_and_quality(self):
        w = workspace()
        w["document"] = new_document(w["document"]["body"] + "\nRemarks: Météo 🙂", 2)
        w["finalization"] = approve(w)
        self.assertEqual(w["finalization"]["text"], preview_text(w, "draft-test"))
        payload = archive_package(w, "draft-test", "", "")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            text = archive.read("forecast.txt")
            record = json.loads(archive.read("provenance.json"))
            manifest = json.loads(archive.read("manifest.json"))["files"]
            self.assertEqual(text, w["finalization"]["text"].encode("utf-8"))
            self.assertEqual(record["text_sha256"], hashlib.sha256(text).hexdigest())
            self.assertEqual(record["approved_version"], 2)
            self.assertEqual(
                record["approved_forecast"]["rows"][0]["element_quality"]["sky"][
                    "status"
                ],
                "manual",
            )
            self.assertEqual(record["original_model_guidance"], w["data"])
            self.assertNotIn("owner", record)
            for name, info in manifest.items():
                self.assertEqual(
                    hashlib.sha256(archive.read(name)).hexdigest(), info["sha256"]
                )
            self.assertTrue(
                all("/" not in name and ".." not in name for name in archive.namelist())
            )

    def test_modified_source_or_text_rejected_by_export_integrity_checks(self):
        for which in ["body", "text", "data"]:
            w = workspace()
            w["finalization"] = approve(w)
            if which == "body":
                w["document"]["body"] += "edited"
            elif which == "text":
                w["finalization"]["text"] += "edited"
            else:
                w["data"]["latitude"] = 0
            with self.assertRaises(FinalizationError):
                provenance_record(w, "draft-test")

    def test_no_ceiling_unknown_and_above_range_are_distinct(self):
        d = data()
        forecast = {
            "rows": [
                {
                    "valid_utc": d["window_rows"][0]["valid_utc"],
                    "sky": normalize_sky("FEW010 SCT020"),
                },
                {"valid_utc": d["window_rows"][1]["valid_utc"], "sky": None},
                {
                    "valid_utc": d["window_rows"][2]["valid_utc"],
                    "sky": normalize_sky("BKN180"),
                },
            ],
            "groups": [],
        }
        series = ceiling_series(forecast)
        self.assertEqual(
            [r["state"] for r in series], ["no_ceiling", "unknown", "ceiling"]
        )
        self.assertEqual([r["height"] for r in series], [None, None, 18000])
        picture = base64.b64decode(
            reviewed_chart(dict(d, grid="0p25"), forecast, "test", 2)
        )
        self.assertTrue(picture.startswith(b"\x89PNG"))


class FinalizationWebTests(unittest.TestCase):
    setUp = review_helpers.WebReviewTests.setUp
    token = review_helpers.WebReviewTests.token
    login = review_helpers.WebReviewTests.login
    create = review_helpers.WebReviewTests.create
    workspace = review_helpers.WebReviewTests.workspace
    form = review_helpers.WebReviewTests.form

    def complete(self):
        url = self.create()
        self.assertEqual(
            self.client.post(url, base_url=BASE, data=self.form(url)).status_code, 303
        )
        return url

    def text_form(self, url, **changes):
        revision, w = self.workspace(url)
        return {
            "csrf_token": self.token(self.client),
            "revision": str(revision),
            "taf_content": w["document"]["body"],
            "action": "finalize",
            "approve": "yes",
            "service_disposition": "awc_coordinated",
            "coordination_note": "Verified with originating office.",
            **changes,
        }

    def test_finalize_download_and_current_owner_only(self):
        url = self.complete()
        before = self.workspace(url)[1]
        with patch.object(__import__("app"), "reviewed_chart", return_value=""):
            response = self.client.post(
                url + "/text", base_url=BASE, data=self.text_form(url)
            )
        self.assertEqual(response.status_code, 303)
        revision, w = self.workspace(url)
        self.assertEqual(revision, 3)
        version = w["finalization"]["approved_version"]
        self.assertEqual(version, 2)
        result = self.client.get(url + f"/final/{version}.txt", base_url=BASE)
        self.assertEqual(result.data, w["finalization"]["text"].encode("utf-8"))
        self.assertEqual(before["data"], w["data"])
        other = self.app.test_client()
        self.login(other)
        self.assertEqual(
            other.get(url + f"/final/{version}.txt", base_url=BASE).status_code, 404
        )
        self.assertEqual(
            self.client.get(url + "/final/999.txt", base_url=BASE).status_code, 409
        )

    def test_edit_after_finalization_creates_new_draft_and_invalidates_old_download(
        self,
    ):
        url = self.complete()
        self.client.post(url + "/text", base_url=BASE, data=self.text_form(url))
        before = deepcopy(self.workspace(url))
        form = self.text_form(
            url,
            action="save",
            taf_content=before[1]["document"]["body"] + "\nRemarks: amended Météo 🙂",
        )
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=form).status_code, 303
        )
        revision, w = self.workspace(url)
        self.assertEqual(w["document"]["version"], revision)
        self.assertNotIn("finalization", w)
        self.assertEqual(
            self.client.get(url + "/final/2.txt", base_url=BASE).status_code, 409
        )
        self.assertEqual(before[1]["data"], w["data"])

    def test_unsaved_text_cannot_be_approved_and_stale_approval_cannot_overwrite(self):
        url = self.complete()
        before = deepcopy(self.workspace(url))
        form = self.text_form(
            url, taf_content=before[1]["document"]["body"] + "\nRemarks: unsaved"
        )
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=form).status_code, 400
        )
        self.assertEqual(self.workspace(url), before)
        stale = self.text_form(url)
        self.client.post(
            url + "/text",
            base_url=BASE,
            data=self.text_form(url, action="save", taf_content="incomplete draft"),
        )
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=stale).status_code, 409
        )
        self.assertNotIn("finalization", self.workspace(url)[1])

    def test_free_text_completion_can_finalize_without_synthetic_model_defaults(self):
        url = self.create()
        self.client.post(
            url + "/text",
            base_url=BASE,
            data=self.text_form(
                url,
                action="save",
                taf_content="FM312100 23020KT P6SM SKC\nRemarks: Météo 🙂",
            ),
        )
        self.assertEqual(
            self.client.post(
                url + "/text", base_url=BASE, data=self.text_form(url)
            ).status_code,
            303,
        )
        w = self.workspace(url)[1]
        self.assertEqual(
            w["finalization"]["forecast"]["rows"][0]["element_quality"]["sky"][
                "status"
            ],
            "manual",
        )
        self.assertIsNone(w["product"]["rows"][0]["sky"]["value"])

    def test_package_uses_same_approved_text_after_worker_reload(self):
        url = self.complete()
        self.client.post(url + "/text", base_url=BASE, data=self.text_form(url))
        import app

        another = app.create_app(self.config)
        # Same underlying DB, same existing authenticated session cookie.
        client = another.test_client()
        cookie = self.client.get_cookie("autospot_session", domain="localhost")
        client.set_cookie(
            "autospot_session", cookie.value, domain="localhost", secure=True
        )
        with patch.object(app, "reviewed_chart", return_value=""):
            response = client.get(url + "/final/2.zip", base_url=BASE)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(
                archive.read("forecast.txt"),
                self.workspace(url)[1]["finalization"]["text"].encode("utf-8"),
            )
            self.assertIn("provenance.json", archive.namelist())

    def test_rebuild_elements_invalidates_approval_and_records_new_document(self):
        url = self.complete()
        self.client.post(url + "/text", base_url=BASE, data=self.text_form(url))
        self.client.post(url, base_url=BASE, data=self.form(url, row_0_sky="BKN025"))
        revision, w = self.workspace(url)
        self.assertNotIn("finalization", w)
        self.assertEqual(w["document"]["version"], revision)
        self.assertIn("BKN025", w["document"]["body"])

    def test_legacy_review_requires_save_before_approval_and_keeps_original_guidance(
        self,
    ):
        url = self.complete()
        revision, w = self.workspace(url)
        original = deepcopy(w["data"])
        w.pop("document")
        with self.client.session_transaction(base_url=BASE) as session:
            owner = session["sid"]
        self.app.extensions["reviews"].save(url.rsplit("/", 1)[1], owner, revision, w)
        page = self.client.get(url, base_url=BASE)
        self.assertEqual(page.status_code, 200)
        form = {
            "csrf_token": self.token(self.client),
            "revision": str(revision + 1),
            "action": "finalize",
            "taf_content": body_from_product(w),
            "approve": "yes",
            "service_disposition": "awc_coordinated",
            "coordination_note": "Verified.",
        }
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=form).status_code, 400
        )
        form["action"] = "save"
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=form).status_code, 303
        )
        self.assertEqual(
            self.client.post(
                url + "/text", base_url=BASE, data=self.text_form(url)
            ).status_code,
            303,
        )
        self.assertEqual(self.workspace(url)[1]["data"], original)

    def test_missing_csrf_expiry_and_unapproved_downloads_fail(self):
        url = self.complete()
        self.assertEqual(
            self.client.get(url + "/final/2.txt", base_url=BASE).status_code, 409
        )
        form = self.text_form(url)
        form.pop("csrf_token")
        self.assertEqual(
            self.client.post(url + "/text", base_url=BASE, data=form).status_code, 400
        )
        identifier = url.rsplit("/", 1)[1]
        with self.app.extensions["reviews"].connection() as db:
            db.execute("UPDATE reviews SET expires=0 WHERE id=?", (identifier,))
        self.assertEqual(
            self.client.post(
                url + "/text", base_url=BASE, data=self.text_form_for_expired(form)
            ).status_code,
            404,
        )

    def text_form_for_expired(self, form):
        return dict(form, csrf_token=self.token(self.client))


if __name__ == "__main__":
    unittest.main()

"""Version-specific approval records and local-download archive packages."""

from datetime import datetime, timezone
import base64
import hashlib
import io
import json
import zipfile

from weather.document import (
    FinalizationError,
    digest,
    parse_body,
    preview_text,
    contributions,
)
from weather.aviation import wind_components


def canonical_bytes(value):
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
        ).encode("utf-8")
        + b"\n"
    )


def prepare_finalization(workspace, identifier, revision, approval, disposition, note):
    doc = workspace.get("document")
    if not doc:
        raise FinalizationError(
            "Save this legacy review as a text draft before finalizing it."
        )
    if digest(doc["body"]) != doc["body_sha256"]:
        raise FinalizationError(
            "The saved body does not match its document hash. Save a fresh draft."
        )
    if approval is not True:
        raise FinalizationError(
            "Record explicit forecaster approval of this saved version."
        )
    if disposition != "awc_coordinated" or not note.strip():
        raise FinalizationError(
            "Record completed AWC service coordination and a coordination note before finalizing. Redirect/other-office/pending dispositions remain drafts."
        )
    if len(note) > 500 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in note):
        raise FinalizationError(
            "Use a one-line coordination note of at most 500 characters."
        )
    parsed = contributions(
        parse_body(doc["body"], workspace["data"]), workspace["product"]
    )
    original = {
        (r["valid_utc"], f["flight_level"]): f
        for r in workspace["data"].get("window_rows", [])
        for f in r.get("flight_levels", [])
    }
    for value in parsed["aloft"]:
        source = original.get((value["valid_utc"], value["flight_level"]))
        same = (
            source
            and source["status"] == "derived"
            and value["wind"] == wind_components(source["u"], source["v"])
        )
        value["status"] = (
            "derived" if same else "manual" if value["wind"] else "missing"
        )
        value["provenance"] = {
            "method": "reviewed_model_FL" if same else "forecaster_aloft_entry",
            "model_reference": {
                "valid_utc": value["valid_utc"],
                "flight_level": value["flight_level"],
            },
            "original_status": source["status"] if source else None,
        }
    finalized_at = datetime.now(timezone.utc).isoformat()
    text = preview_text(workspace, identifier, issued_at=finalized_at)
    return {
        "approved_version": doc["version"],
        "source_revision": revision,
        "finalized_at": finalized_at,
        "body_sha256": digest(doc["body"]),
        "text": text,
        "text_sha256": digest(text),
        "forecast": parsed,
        "original_guidance_sha256": hashlib.sha256(
            canonical_bytes(workspace["data"])
        ).hexdigest(),
        "attestation": {
            "forecaster_approved": True,
            "weather_including_none_reviewed": True,
            "requested_aloft_reviewed": True,
            "service_disposition": disposition,
            "coordination_note": note.strip(),
            "scope": "Recorded forecaster review, not machine-verified service authority or meteorological truth",
        },
    }


def provenance_record(workspace, identifier):
    final = workspace["finalization"]
    data = workspace["data"]
    if final["text"] != preview_text(workspace, identifier):
        raise FinalizationError(
            "The approved text does not match its saved request/document metadata."
        )
    if (
        digest(final["text"]) != final["text_sha256"]
        or digest(workspace["document"]["body"]) != final["body_sha256"]
    ):
        raise FinalizationError("The saved text does not match its approval record.")
    if (
        hashlib.sha256(canonical_bytes(data)).hexdigest()
        != final["original_guidance_sha256"]
    ):
        raise FinalizationError(
            "Original guidance does not match the approved source record."
        )
    return {
        "schema_version": 1,
        "application_policy_version": "autospot-finalization-1",
        "draft_id": identifier,
        "approved_version": final["approved_version"],
        "source_revision": final["source_revision"],
        "prepared_at": workspace["document"]["prepared_at"],
        "finalized_at": final["finalized_at"],
        "issue_time_utc": final["finalized_at"],
        "operation": workspace["label"],
        "location": {"latitude": data["latitude"], "longitude": data["longitude"]},
        "model": data.get("model", "ECMWF_IFS"),
        "grid": data["grid"],
        "cycle_utc": data["cycle_utc"],
        "start_utc": data["start_utc"],
        "end_utc": data["end_utc"],
        "units": {
            "sky": "feet AGL",
            "wind": "true direction / knots",
            "flight_levels": "standard pressure altitude",
        },
        "text_sha256": final["text_sha256"],
        "body_sha256": final["body_sha256"],
        "original_guidance_sha256": final["original_guidance_sha256"],
        "approval": final["attestation"],
        "domain_reference": data.get("domain"),
        "native_field_quality": data.get("counts", {}),
        "original_model_guidance": data,
        "structured_element_review": workspace["product"],
        "saved_document": workspace["document"],
        "approved_forecast": final["forecast"],
        "archive_status": "Downloaded package only; no NAOC archive write or external transmission performed",
    }


def archive_package(workspace, identifier, original_chart, forecast_chart):
    record = provenance_record(workspace, identifier)
    content = {
        "forecast.txt": workspace["finalization"]["text"].encode("utf-8"),
        "provenance.json": canonical_bytes(record),
        "README.txt": (
            "AWC aviation spot forecast download package\n"
            f"Draft {identifier}; approved version {workspace['finalization']['approved_version']}\n"
            "forecast.txt contains the exact approved UTF-8 text. provenance.json retains the request, original model guidance, derived values and forecaster contributions.\n"
            "original_model_guidance.png is original model guidance; text edits do not change it. reviewed_forecast.png shows the approved text forecast, including ceiling states.\n"
            "Place this package in the established NAOC archive using the local procedure. This download did not write to that folder or transmit a forecast.\n"
            "The application review store is temporary. Retain the downloaded files under the established archive process.\n"
        ).encode("utf-8"),
    }
    for name, picture in [
        ("original_model_guidance.png", original_chart),
        ("reviewed_forecast.png", forecast_chart),
    ]:
        if picture:
            content[name] = base64.b64decode(picture, validate=True)
    manifest = {
        name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        for name, value in content.items()
    }
    content["manifest.json"] = canonical_bytes(
        {
            "files": manifest,
            "hash_algorithm": "SHA-256",
            "manifest_excludes_itself": True,
        }
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in content.items():
            archive.writestr(name, value)
    return output.getvalue()

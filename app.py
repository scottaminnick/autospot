from datetime import datetime, timezone
import hmac
import io
import json
import sqlite3
import os
from pathlib import Path
from functools import wraps

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    g,
    request,
    render_template,
    session,
    redirect,
    url_for,
    send_file,
)
from werkzeug.exceptions import HTTPException

from request_validation import (
    parse_forecast_request,
    validate_report,
    RequestValidationError,
    single,
)
from web_security import (
    install_security,
    activate_security,
    configured,
    credential_revision,
    private_bucket,
    csrf_token,
    csrf_valid,
    rate_limit,
)
from weather.pipeline import retrieve_forecast
from weather.fields import DataFault
from weather.draft import (
    build_draft,
    build_spot_report,
    chart,
    guidance_rows,
    wind,
    wind_components,
)
from weather.domain import location_map
from weather.aviation import ElementError
from weather.product import initialize_product, apply_elements, editor_rows
from weather.reviews import ReviewStore, ReviewMissing, ReviewConflict, ReviewFull
from weather.document import (
    body_from_product,
    new_document,
    preview_text,
    parse_body,
    FinalizationError,
)
from weather.exports import prepare_finalization, provenance_record, archive_package
from weather.charts import reviewed_chart, ceiling_series

web = Blueprint("web", __name__)


class ForecastUnavailable(Exception):
    pass


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not g.auth_sid:
            return redirect(url_for("web.login"))
        return view_func(*args, **kwargs)

    return wrapped


def require_csrf():
    if not csrf_valid():
        abort(
            400,
            description="The form expired or could not be verified. Reload the page and try again.",
        )


@web.route("/healthz")
def health():
    if not configured():
        return {"status": "unavailable"}, 503
    try:
        current_app.extensions["controls"].ready()
        current_app.extensions["reviews"].ready()
    except (sqlite3.Error, OSError):
        return {"status": "unavailable"}, 503
    return {"status": "ok"}


@web.route("/login", methods=["GET", "POST"])
def login():
    if not configured():
        abort(
            503,
            description="Application configuration is incomplete. Contact the maintainer.",
        )
    error = None
    if request.method == "POST":
        require_csrf()
        rate_limit(
            [
                ("login_global", "all", 100, 900),
                (
                    "login_address",
                    private_bucket(request.remote_addr or "unknown"),
                    20,
                    900,
                ),
            ]
        )
        passwords = request.form.getlist("password")
        entered = passwords[0] if len(passwords) == 1 else ""
        if len(entered.encode("utf-8")) <= 1024 and hmac.compare_digest(
            entered.encode("utf-8"), current_app.config["APP_PASSWORD"].encode("utf-8")
        ):
            controls = current_app.extensions["controls"]
            if g.auth_sid:
                controls.revoke(g.auth_sid)
            session.clear()
            session.permanent = True
            session["sid"] = controls.issue(credential_revision(), 8 * 3600)
            csrf_token()
            return redirect(url_for("web.index"))
        error = "Incorrect password."
    return render_template("login.html", error=error), 401 if error else 200


@web.route("/logout", methods=["POST"])
@login_required
def logout():
    require_csrf()
    current_app.extensions["controls"].revoke(g.auth_sid)
    session.clear()
    return redirect(url_for("web.login"))


@web.route("/")
@login_required
def index():
    now = datetime.now(timezone.utc)
    return render_template(
        "request.html",
        values={
            "start_date": now.strftime("%Y-%m-%d"),
            "start_hour": now.strftime("%H"),
            "duration": "24",
            "sar_name": "AUTO-SPOT-PROD",
        },
    )


@web.route("/submit", methods=["POST"], defaults={"simple": False})
@web.route("/generate", methods=["POST"], defaults={"simple": True})
@login_required
def submit(simple):
    require_csrf()
    forecast = parse_forecast_request(request.form)
    if forecast.model != "ECMWF":
        raise RequestValidationError(
            "GFS guidance is temporarily unavailable while its adapter is validated. Choose ECMWF."
        )
    rate_limit(
        [("submit_global", "all", 20, 300), ("submit_session", g.auth_sid, 3, 300)]
    )
    if simple:
        report, image, data = generate_forecast(
            forecast, report_builder=build_spot_report
        )
        return render_template(
            "spot_report.html", report=report, chart=image, data=data
        )
    report, image, data = generate_forecast(forecast)
    if data:
        workspace = {
            "data": data,
            "label": forecast.label,
            "product": initialize_product(data),
        }
        workspace["document"] = new_document(
            body_from_product(workspace), 1, "structured"
        )
        identifier = current_app.extensions["reviews"].create(
            g.auth_sid,
            workspace,
        )
        return redirect(url_for("web.review", identifier=identifier), code=303)
    return render_template(
        "review.html",
        report=report,
        chart=image,
        data=data,
        guidance=guidance_rows(data) if data else [],
        location_map=location_map(data["latitude"], data["longitude"])
        if data
        else None,
        wind_text=wind,
        wind_components=wind_components,
    )


@web.route("/review/<identifier>", methods=["GET", "POST"])
@login_required
def review(identifier):
    store = current_app.extensions["reviews"]
    revision, workspace = store.get(identifier, g.auth_sid)
    if request.method == "POST":
        require_csrf()
        rate_limit([("review_session", g.auth_sid, 20, 60)])
        for key in request.form:
            if len(request.form.getlist(key)) != 1:
                raise RequestValidationError("Duplicate review fields are not allowed.")
        supplied = single(request.form, "revision", "")
        if supplied != str(revision):
            raise ReviewConflict()
        data = workspace["data"]
        entries = []
        common = {
            n: review_value("common_" + n) for n in ("visibility", "weather", "sky")
        }
        use_common = review_value("apply_common") == "yes"
        wind_all = review_value("review_all_winds") == "yes"
        for i, row in enumerate(workspace["product"]["rows"]):
            entry = {
                n: review_value(f"row_{i}_{n}")
                for n in ("wind", "visibility", "weather", "sky")
            }
            if use_common:
                entry.update({n: common[n] for n in common if not entry[n]})
            entry["wind_reviewed"] = (
                wind_all or review_value(f"row_{i}_wind_reviewed") == "yes"
            )
            entries.append(entry)
        count = review_value("group_count")
        if not count.isdecimal() or not 0 <= int(count) <= 12:
            raise RequestValidationError("Invalid number of conditional groups.")
        groups = []
        for i in range(int(count)):
            group = {
                n: review_value(f"group_{i}_{n}")
                for n in (
                    "kind",
                    "start",
                    "end",
                    "wind",
                    "visibility",
                    "weather",
                    "sky",
                )
            }
            # The browser's datetime-local values are explicitly labeled UTC.
            from datetime import datetime as calendar_datetime

            for name in ("start", "end"):
                if group[name]:
                    try:
                        group[name] = (
                            calendar_datetime.strptime(group[name], "%Y-%m-%dT%H:%M")
                            .replace(tzinfo=timezone.utc)
                            .isoformat()
                        )
                    except ValueError:
                        raise RequestValidationError(
                            "Enter a complete UTC date and hour for conditional groups."
                        ) from None
            groups.append(group)
        workspace["product"] = apply_elements(
            workspace["product"], entries, groups, data["start_utc"], data["end_utc"]
        )
        workspace["document"] = new_document(
            body_from_product(workspace), revision + 1, "structured"
        )
        workspace.pop("finalization", None)
        store.save(identifier, g.auth_sid, revision, workspace)
        return redirect(url_for("web.review", identifier=identifier), code=303)
    data = workspace["data"]
    document = workspace.get("document")
    final = workspace.get("finalization")
    forecast = (
        final["forecast"]
        if final
        else {
            "rows": [
                {"valid_utc": r["valid_utc"], "sky": r["sky"]["value"]}
                for r in workspace["product"]["rows"]
            ],
            "groups": workspace["product"]["groups"],
        }
    )
    forecast_basis = (
        "Approved saved text"
        if final
        else "Structured element review; saved text is not yet validated"
    )
    text_validated = bool(final)
    if not final and document:
        try:
            forecast = parse_body(document["body"], data)
            forecast_basis = "Saved text draft; not yet finalized"
            text_validated = True
        except FinalizationError:
            pass
    return render_template(
        "review.html",
        report=document["body"] if document else body_from_product(workspace),
        chart=chart(data),
        data=data,
        guidance=guidance_rows(data),
        location_map=location_map(data["latitude"], data["longitude"]),
        wind_text=wind,
        wind_components=wind_components,
        review_id=identifier,
        revision=revision,
        product=workspace["product"],
        editor=editor_rows(workspace["product"]),
        group_editor=conditional_editor(workspace["product"]["groups"]),
        document=document,
        finalization=final,
        candidate_text=preview_text(workspace, identifier) if document else None,
        forecast_chart=reviewed_chart(
            data,
            forecast,
            identifier,
            document["version"] if document else revision,
            bool(final),
            basis=forecast_basis,
        ),
        forecast_ceiling=ceiling_series(forecast),
        forecast_basis=forecast_basis,
        text_validated=text_validated,
    )


@web.route("/review/<identifier>/text", methods=["POST"])
@login_required
def save_text(identifier):
    require_csrf()
    rate_limit([("review_text_session", g.auth_sid, 20, 60)])
    store = current_app.extensions["reviews"]
    revision, workspace = store.get(identifier, g.auth_sid)
    if single(request.form, "revision", "") != str(revision):
        raise ReviewConflict()
    body = validate_report(request.form)
    action = single(request.form, "action", "save")
    if action not in {"save", "draft", "finalize"}:
        raise RequestValidationError("Choose a valid text action.")
    current = workspace.get("document")
    if action == "finalize":
        if not current or body != current["body"]:
            raise FinalizationError(
                "Save text changes as a new draft, review that saved version, then finalize."
            )
        if not workspace.get("finalization"):
            workspace["finalization"] = prepare_finalization(
                workspace,
                identifier,
                revision,
                single(request.form, "approve", "") == "yes",
                single(request.form, "service_disposition", "pending"),
                single(request.form, "coordination_note", ""),
            )
            store.save(identifier, g.auth_sid, revision, workspace)
        return redirect(url_for("web.review", identifier=identifier), code=303)
    if not current or body != current["body"] or workspace.get("finalization"):
        workspace["document"] = new_document(body, revision + 1)
        workspace.pop("finalization", None)
        store.save(identifier, g.auth_sid, revision, workspace)
    if action == "draft":
        text = "DRAFT — NOT FINALIZED\n" + preview_text(workspace, identifier)
        return text_attachment(
            text, f"DRAFT_SPOT_{identifier[:8]}_v{workspace['document']['version']}.txt"
        )
    return redirect(url_for("web.review", identifier=identifier), code=303)


def approved_workspace(identifier, version):
    _, workspace = current_app.extensions["reviews"].get(identifier, g.auth_sid)
    final = workspace.get("finalization")
    if (
        not final
        or final["approved_version"] != version
        or workspace["document"]["version"] != version
    ):
        raise ReviewConflict()
    provenance_record(workspace, identifier)
    return workspace


def text_attachment(text, filename):
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


@web.route("/review/<identifier>/final/<int:version>.txt")
@login_required
def final_text(identifier, version):
    workspace = approved_workspace(identifier, version)
    rate_limit([("export_session", g.auth_sid, 30, 60)])
    return text_attachment(
        workspace["finalization"]["text"], f"SPOT_{identifier[:8]}_v{version}.txt"
    )


@web.route("/review/<identifier>/final/<int:version>.zip")
@login_required
def final_package(identifier, version):
    workspace = approved_workspace(identifier, version)
    rate_limit([("package_session", g.auth_sid, 10, 60)])
    content = archive_package(
        workspace,
        identifier,
        chart(workspace["data"]),
        reviewed_chart(
            workspace["data"],
            workspace["finalization"]["forecast"],
            identifier,
            version,
            True,
        ),
    )
    return send_file(
        io.BytesIO(content),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"SPOT_{identifier[:8]}_v{version}.zip",
        max_age=0,
    )


def review_value(name):
    value = single(request.form, name, "").strip()
    if len(value) > 120 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise RequestValidationError(
            "Review entries must be one line of at most 120 characters."
        )
    return value


def conditional_editor(groups):
    output = []
    for group in groups:
        output.append(
            dict(
                group,
                start=group["start"][:16],
                end=group["end"][:16],
                wind=group["wind"] or "",
                visibility=group["visibility"] or "",
                weather=" ".join(group["weather"]) if group["weather"] else "",
                sky=" ".join(group["sky"]["tokens"]) if group["sky"] else "",
            )
        )
    return output + [{} for _ in range(min(12 - len(output), max(1, 4 - len(output))))]


@web.route("/transmit", methods=["POST"], defaults={"approved": False})
@web.route("/save-report", methods=["POST"], defaults={"approved": True})
@login_required
def transmit(approved):
    require_csrf()
    text = validate_report(request.form)
    rate_limit([("export_session", g.auth_sid, 30, 60)])
    prefix = "SPOT_TAF" if approved else "DRAFT_SPOT"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    return text_attachment(text, f"{prefix}_{stamp}.txt")


def generate_forecast(forecast, *, report_builder=build_draft):
    try:
        data = retrieve_forecast(forecast)
    except DataFault as error:
        if error.code == "gfs_adapter_not_validated":
            raise RequestValidationError(
                "GFS guidance is temporarily unavailable while its data adapter is validated. Choose ECMWF."
            ) from None
        if error.code == "task_budget":
            raise RequestValidationError(
                "This request exceeds the download budget. Select fewer flight levels or a shorter period."
            ) from None
        current_app.logger.warning(
            json.dumps(
                {
                    "event": "model_retrieval_failed",
                    "request_id": g.get("request_id"),
                    "reason": error.code,
                    "state": str(error.state),
                    "details": error.details,
                }
            )
        )
        raise ForecastUnavailable(
            "Verified model guidance could not be retrieved within the request limits. Try again later."
        ) from None
    return report_builder(data, forecast.label), chart(data), data


def create_app(test_config=None):
    application = Flask(__name__)
    install_security(application)
    if test_config:
        application.config.update(test_config)
    activate_security(application)
    review_path = (
        application.config.get("REVIEW_DB_PATH")
        or os.environ.get("AUTOSPOT_REVIEW_DB")
        or str(
            Path(application.config["SECURITY_DB_PATH"]).with_name("reviews.sqlite3")
        )
    )
    application.extensions["reviews"] = ReviewStore(review_path)
    application.register_blueprint(web)
    application.logger.setLevel("INFO")

    @application.errorhandler(RequestValidationError)
    def invalid_request(error):
        return render_template("error.html", message=str(error)), 400

    @application.errorhandler(ElementError)
    def invalid_element(error):
        return render_template("error.html", message=str(error)), 400

    @application.errorhandler(FinalizationError)
    def invalid_finalization(error):
        return render_template("error.html", message=str(error)), 400

    @application.errorhandler(ReviewMissing)
    def missing_review(error):
        return render_template(
            "error.html",
            message="This review is unavailable in this session or has expired. Generate a new draft if needed.",
        ), 404

    @application.errorhandler(ReviewConflict)
    def stale_review(error):
        return render_template(
            "error.html",
            message="This review changed in another tab. Reload it before applying elements; your newer changes were preserved.",
        ), 409

    @application.errorhandler(ReviewFull)
    def full_reviews(error):
        return render_template(
            "error.html",
            message="Temporary review storage is full. Try again later or contact the maintainer.",
        ), 503

    @application.errorhandler(ForecastUnavailable)
    def unavailable(error):
        return render_template("error.html", message=str(error)), 502

    @application.errorhandler(HTTPException)
    def http_error(error):
        messages = {
            400: error.description,
            401: "Sign in to continue.",
            404: "Page not found.",
            405: "This action requires the correct form submission.",
            413: "The submitted form is too large.",
            429: "Too many requests. Try again later.",
            503: "The service is temporarily unavailable. Contact the maintainer if this continues.",
        }
        response = error.get_response()
        response.set_data(
            render_template(
                "error.html",
                message=messages.get(error.code, "The request could not be completed."),
            )
        )
        response.content_type = "text/html; charset=utf-8"
        return response

    @application.errorhandler(Exception)
    def server_error(error):
        # Error strings, request bodies and credentials must never enter logs or HTML.
        application.logger.error(
            json.dumps(
                {
                    "event": "request_failed",
                    "request_id": g.get("request_id"),
                    "error_type": type(error).__name__,
                }
            )
        )
        code = 503 if isinstance(error, (sqlite3.Error, OSError)) else 500
        return render_template(
            "error.html",
            message="The request could not be completed. Contact the maintainer with the reference below.",
        ), code

    return application


app = create_app()

if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5005))
    app.run(host="0.0.0.0", port=port)

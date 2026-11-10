from datetime import datetime, timezone
import hmac
import io
import json
import sqlite3
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
from weather.draft import build_draft, chart, guidance_rows, wind, wind_components
from weather.domain import location_map

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
            "model_choice": "ECMWF",
            "start_date": now.strftime("%Y-%m-%d"),
            "start_hour": now.strftime("%H"),
            "duration": "24",
            "sar_name": "AUTO-SPOT",
        },
    )


@web.route("/submit", methods=["POST"])
@login_required
def submit():
    require_csrf()
    forecast = parse_forecast_request(request.form)
    if forecast.model != "ECMWF":
        raise RequestValidationError(
            "GFS guidance is temporarily unavailable while its adapter is validated. Choose ECMWF."
        )
    rate_limit(
        [("submit_global", "all", 20, 300), ("submit_session", g.auth_sid, 3, 300)]
    )
    report, image, data = generate_forecast(forecast)
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


@web.route("/transmit", methods=["POST"])
@login_required
def transmit():
    require_csrf()
    final_taf = validate_report(request.form)
    rate_limit([("export_session", g.auth_sid, 30, 60)])
    # Direct attachment preserves the exact UTF-8 text and removes the HTML/JS sink.
    filename = f"SPOT_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%SZ')}.txt"
    return send_file(
        io.BytesIO(final_taf.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


def generate_forecast(forecast):
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
    return build_draft(data, forecast.label), chart(data), data


def create_app(test_config=None):
    application = Flask(__name__)
    install_security(application)
    if test_config:
        application.config.update(test_config)
    activate_security(application)
    application.register_blueprint(web)
    application.logger.setLevel("INFO")

    @application.errorhandler(RequestValidationError)
    def invalid_request(error):
        return render_template("error.html", message=str(error)), 400

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

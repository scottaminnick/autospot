"""Shared, bounded security controls for workers on one host/replica.

SQLite state lives on the host's temporary disk. It is not an archive. Multiple
replicas require a shared control store before enabling this configuration.
"""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from flask import current_app, g, request, session
from werkzeug.middleware.proxy_fix import ProxyFix


class ControlStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Reserve a private file before SQLite opens it (even with a permissive umask).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    sid TEXT PRIMARY KEY, revision TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rates (
                    category TEXT NOT NULL, bucket TEXT NOT NULL,
                    count INTEGER NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(category, bucket)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def ready(self):
        with self.connection() as db:
            db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()

    def permit(self, limits):
        """Atomically apply fixed-window budgets; return retry seconds or zero."""
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM rates WHERE expires <= ?", (now,))
                db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
                for category, bucket, maximum, window in limits:
                    row = db.execute(
                        "SELECT count, expires FROM rates WHERE category=? AND bucket=?",
                        (category, bucket),
                    ).fetchone()
                    if row and row[0] >= maximum:
                        db.commit()
                        return max(1, int(row[1] - now) + 1)
                for category, bucket, maximum, window in limits:
                    db.execute(
                        """INSERT INTO rates VALUES (?, ?, 1, ?)
                        ON CONFLICT(category, bucket) DO UPDATE SET count=count+1""",
                        (category, bucket, now + window),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return 0

    def issue(self, revision, lifetime):
        sid = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (sid, revision, time.time() + lifetime),
            )
        return sid

    def valid(self, sid, revision):
        with self.connection() as db:
            row = db.execute(
                "SELECT revision, expires FROM sessions WHERE sid=?", (sid,)
            ).fetchone()
        return bool(
            row and row[1] > time.time() and hmac.compare_digest(row[0], revision)
        )

    def revoke(self, sid):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def configured():
    key, password = current_app.secret_key, current_app.config.get("APP_PASSWORD")
    return (
        isinstance(key, (str, bytes))
        and len(key) >= 32
        and isinstance(password, str)
        and bool(password)
    )


def credential_revision():
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(
        key, current_app.config["APP_PASSWORD"].encode("utf-8"), hashlib.sha256
    ).hexdigest()


def private_bucket(value):
    return hmac.new(
        credential_revision().encode("ascii"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def csrf_token():
    if "csrf" not in session or time.time() - session.get("csrf_issued", 0) > 7200:
        session["csrf"] = secrets.token_urlsafe(32)
        session["csrf_issued"] = time.time()
    return session["csrf"]


def csrf_valid():
    supplied = request.form.getlist("csrf_token")
    expected = session.get("csrf", "")
    return (
        len(supplied) == 1
        and bool(expected)
        and time.time() - session.get("csrf_issued", 0) <= 7200
        and hmac.compare_digest(supplied[0].encode("utf-8"), expected.encode("utf-8"))
    )


def rate_limit(limits):
    retry = current_app.extensions["controls"].permit(limits)
    if retry:
        from werkzeug.exceptions import TooManyRequests

        raise TooManyRequests("Too many requests. Try again later.", retry_after=retry)


def install_security(app):
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        APP_PASSWORD=os.environ.get("APP_PASSWORD"),
        SESSION_COOKIE_NAME="autospot_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=256 * 1024,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=16,
        SECURITY_DB_PATH=os.environ.get(
            "AUTOSPOT_SECURITY_DB", "/tmp/autospot-state/security.sqlite3"
        ),
        TRUST_PROXY_HOPS=int(os.environ.get("AUTOSPOT_TRUST_PROXY_HOPS", "0")),
    )


def activate_security(app):
    hops = app.config["TRUST_PROXY_HOPS"]
    if not 0 <= hops <= 2:
        raise ValueError("TRUST_PROXY_HOPS must be between 0 and 2")
    if hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=0, x_port=0, x_prefix=0
        )
    app.extensions["controls"] = ControlStore(app.config["SECURITY_DB_PATH"])
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def request_context():
        g.request_id = uuid.uuid4().hex
        g.started = time.monotonic()
        g.auth_sid = None
        if configured() and session.get("sid"):
            if app.extensions["controls"].valid(session["sid"], credential_revision()):
                g.auth_sid = session["sid"]
            else:
                session.clear()

    @app.after_request
    def response_controls(response):
        response.headers["X-Request-ID"] = g.get("request_id", uuid.uuid4().hex)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; object-src 'none'"
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=15552000"
        app.logger.info(
            json.dumps(
                {
                    "event": "http_response",
                    "request_id": response.headers["X-Request-ID"],
                    "endpoint": request.endpoint or "unmatched",
                    "status": response.status_code,
                    "duration_ms": round(
                        (time.monotonic() - g.get("started", time.monotonic())) * 1000
                    ),
                }
            )
        )
        return response

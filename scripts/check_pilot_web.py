"""Time two identical draft requests against an isolated HTTPS pilot."""

import argparse
from datetime import datetime, timedelta, timezone
from getpass import getpass
from html import unescape
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests


def require(condition, message):
    if not condition:
        raise ValueError(message)


def csrf_token(page):
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    require(token, "Expected a CSRF token")
    return token[1]


def check_response(response, expected_status):
    request_id = response.headers.get("X-Request-ID")
    require(
        response.status_code == expected_status and request_id,
        f"Expected HTTP {expected_status}; received {response.status_code}, request ID {request_id}",
    )
    require(
        response.headers.get("Cache-Control") == "no-store",
        "Missing no-store response control",
    )
    return request_id


def check_pilot(base, password, start, latitude, longitude, duration, flight_levels):
    with requests.Session() as session:
        health = session.get(base + "/healthz", timeout=30, allow_redirects=False)
        check_response(health, 200)
        require(health.json() == {"status": "ok"}, "Unexpected readiness response")
        require(
            health.headers.get("Strict-Transport-Security"),
            "Missing HSTS response control",
        )

        login = session.get(base + "/login", timeout=15, allow_redirects=False)
        check_response(login, 200)
        signed_in = session.post(
            base + "/login",
            data={"password": password, "csrf_token": csrf_token(login)},
            timeout=15,
            allow_redirects=False,
        )
        check_response(signed_in, 302)
        cookie = signed_in.headers.get("Set-Cookie", "").lower()
        require(
            all(flag in cookie for flag in ("secure", "httponly", "samesite=lax")),
            "Missing session cookie controls",
        )
        form = session.get(base + "/", timeout=15, allow_redirects=False)
        check_response(form, 200)
        data = {
            "csrf_token": csrf_token(form),
            "model_choice": "ECMWF",
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.strftime("%Y-%m-%d"),
            "start_hour": start.strftime("%H"),
            "duration": duration,
            "sar_name": "PILOT VALIDATION - DRAFT",
            "domain_disposition": "pending",
            "flight_levels": flight_levels,
        }

        checks = []
        for attempt in ("first", "repeat"):
            began = time.monotonic()
            response = session.post(
                base + "/submit", data=data, timeout=110, allow_redirects=False
            )
            request_id = check_response(response, 303)
            review_path = response.headers.get("Location", "")
            require(
                re.fullmatch(r"/review/[A-Za-z0-9_-]+", review_path),
                "Expected a local review URL",
            )
            review = session.get(base + review_path, timeout=30, allow_redirects=False)
            elapsed = time.monotonic() - began
            check_response(review, 200)
            draft = re.search(
                r'<textarea id="taf_content"[^>]*>(.*?)</textarea>', review.text, re.S
            )
            require(draft, "Expected an editable draft body")
            raw_heading = review.text.find('id="raw-guidance"')
            require(
                0 <= raw_heading < draft.start(),
                "Expected raw guidance before the editor",
            )
            body = unescape(draft[1])
            require(f"FM{start:%d%H%M}" in body, "Draft start changed")
            require(
                "[VISIBILITY REQUIRED]" in body and "[SKY REQUIRED]" in body,
                "Expected manual completion markers",
            )
            require(
                "Original model guidance" in review.text, "Missing original guidance"
            )
            require(
                not re.search(r'href="[^"]+/final/[^"]+"', review.text),
                "Unexpected approved download",
            )
            for level in flight_levels.split(",") if flight_levels else ():
                require(
                    f"FL{int(level):03d}:" in body,
                    f"Missing requested flight level {level}",
                )
            cycle = re.search(r"Cycle: (\d{4}-\d{2}-\d{2}T[^< ]+)\.<br>", review.text)
            require(cycle, "Missing model cycle")
            checks.append(
                {
                    "attempt": attempt,
                    "seconds_to_review": round(elapsed, 3),
                    "submit_request_id": request_id,
                    "cycle_utc": cycle[1],
                }
            )
        return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Isolated pilot HTTPS origin")
    parser.add_argument("--latitude", type=float, default=39.30)
    parser.add_argument("--longitude", type=float, default=-94.71)
    parser.add_argument("--duration", type=int, default=24)
    parser.add_argument(
        "--flight-levels", default="", help="Comma-separated numeric flight levels"
    )
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.path
        or url.query
        or url.fragment
    ):
        parser.error(
            "Supply an HTTPS origin without a path, user information, or query"
        )
    password = os.environ.get("AUTOSPOT_PILOT_PASSWORD") or getpass("Pilot password: ")
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start += timedelta(hours=1)
    if args.flight_levels and start.hour % 3 == 0:
        start += timedelta(hours=1)
    checks = check_pilot(
        base,
        password,
        start,
        args.latitude,
        args.longitude,
        args.duration,
        args.flight_levels,
    )
    result = json.dumps(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "request_start_utc": start.isoformat(),
            "duration_hours": args.duration,
            "flight_levels": args.flight_levels,
            "checks": checks,
        },
        indent=2,
    )
    if args.summary:
        args.summary.write_text(result + "\n")
    print(result)


if __name__ == "__main__":
    main()

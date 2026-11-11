"""Bound requests before scheduling any upstream work."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re

MAX_REPORT_BYTES = 64_000
MAX_FIELD_TASKS = 700


class RequestValidationError(ValueError):
    pass


def single(form, key, default=None):
    values = form.getlist(key)
    if len(values) > 1:
        raise RequestValidationError(f"Provide one value for {key}.")
    return values[0] if values else default


@dataclass(frozen=True)
class ForecastRequest:
    latitude: float
    longitude: float
    label: str
    model: str
    duration: int
    start: datetime
    flight_levels: tuple[int, ...] = ()
    domain_disposition: str = "pending"


def parse_forecast_request(form, now=None):
    now = now or datetime.now(timezone.utc)
    coordinates = []
    for key, bound in [("latitude", 90), ("longitude", 180)]:
        raw = single(form, key, "")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise RequestValidationError(f"Enter a valid {key}.") from None
        if not math.isfinite(value) or not -bound <= value <= bound:
            raise RequestValidationError(
                f"{key.capitalize()} must be between {-bound} and {bound} degrees."
            )
        coordinates.append(value)
    model = single(form, "model_choice", "GFS")
    if model != "GFS":
        raise RequestValidationError("Only GFS is available for this pilot.")
    duration = single(form, "duration", "")
    if not re.fullmatch(r"[0-9]{1,2}", duration) or not 1 <= int(duration) <= 72:
        raise RequestValidationError(
            "Duration must be a whole number from 1 to 72 hours."
        )
    date, hour = single(form, "start_date", ""), single(form, "start_hour", "")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3])", hour
    ):
        raise RequestValidationError("Enter a valid date and UTC hour (00–23).")
    try:
        start = datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise RequestValidationError("Enter a valid calendar date.") from None
    if start < now.replace(minute=0, second=0, microsecond=0):
        raise RequestValidationError(
            "Start must be the current UTC hour or later. Historical replay is unavailable here."
        )
    if start + timedelta(hours=int(duration)) > now + timedelta(hours=144):
        raise RequestValidationError(
            "The requested period must end within the next 144 hours. Model availability is checked separately."
        )
    label = single(form, "sar_name", "AUTO-SPOT").strip()
    if len(label) > 120 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in label):
        raise RequestValidationError("Use a one-line label of at most 120 characters.")
    raw_levels = single(form, "flight_levels", "").strip()
    levels = []
    if raw_levels:
        for token in raw_levels.split(","):
            if not re.fullmatch(r"\s*(?:FL)?[0-9]{2,3}\s*", token, re.IGNORECASE):
                raise RequestValidationError(
                    "Use comma-separated flight levels, such as FL030, FL060, FL180."
                )
            level = int(re.sub(r"(?i)fl", "", token).strip())
            if not 10 <= level <= 450 or level in levels:
                raise RequestValidationError(
                    "Use distinct flight levels from FL010 to FL450."
                )
            levels.append(level)
        if len(levels) > 12:
            raise RequestValidationError("Request at most 12 flight levels.")
    disposition = single(form, "domain_disposition", "pending")
    if disposition not in {
        "pending",
        "awc_coordinated",
        "aawu_redirect",
        "other_office_coordination",
    }:
        raise RequestValidationError("Choose a valid service-domain disposition.")
    # Bound the complete work before any index or model request. Phase 3 adds
    # two components at each distinct enclosing pressure level.
    from weather.coordinates import pressure_brackets
    from weather.fields import SPECS

    pressure_levels = {p for fl in levels for p in pressure_brackets(fl)}
    maximum_slots = math.ceil(int(duration) / 3) + 2
    if maximum_slots * (len(SPECS) + 2 * len(pressure_levels)) + 1 > MAX_FIELD_TASKS:
        raise RequestValidationError(
            "This combination exceeds the download budget. Select fewer flight levels or a shorter period."
        )
    return ForecastRequest(
        *coordinates,
        label or "AUTO-SPOT",
        model,
        int(duration),
        start,
        tuple(sorted(levels)),
        disposition,
    )


def validate_report(form):
    text = single(form, "taf_content", "")
    if (
        not text.strip()
        or len(text.encode("utf-8")) > MAX_REPORT_BYTES
        or "\x00" in text
    ):
        raise RequestValidationError(
            "Provide report text of at most 64,000 UTF-8 bytes, without null characters."
        )
    return text

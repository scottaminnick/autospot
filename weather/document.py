"""Saved forecast bodies and the local AWC point-product text grammar."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import re

from weather.aviation import (
    ElementError,
    normalize_wind,
    normalize_visibility,
    normalize_weather,
    normalize_sky,
    wind_components,
)
from weather.coordinates import utc
from weather.product import format_prevailing


class FinalizationError(ValueError):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def aloft_lines(data):
    lines = []
    if data.get("requested_flight_levels"):
        lines = ["", "Winds aloft (standard pressure altitude; true direction, knots):"]
        for row in data["window_rows"]:
            if row["support_only"]:
                continue
            for level in row["flight_levels"]:
                prefix = f"{utc(row['valid_utc']):%Y-%m-%d %H:%MZ} FL{level['flight_level']:03d}: "
                if level["status"] == "derived":
                    code = wind_components(level["u"], level["v"])
                    if code == "00000KT":
                        value = "calm (0 kt)"
                    elif code:
                        value = f"from {code[:3]} degrees true at {int(code[3:-2])} kt"
                    else:
                        value = "[WIND REVIEW REQUIRED]"
                else:
                    value = f"unavailable ({level['reason'].replace('_', ' ')})"
                lines.append(prefix + value)
    return lines


def body_from_product(workspace):
    return "\n".join(
        format_prevailing(workspace["product"], workspace["data"]["end_utc"])
        + aloft_lines(workspace["data"])
    )


def new_document(body, version, origin="free_text", prepared_at=None):
    return {
        "body": body,
        "version": version,
        "origin": origin,
        "body_sha256": digest(body),
        "prepared_at": prepared_at or datetime.now(timezone.utc).isoformat(),
    }


def preview_text(workspace, identifier, issued_at=None):
    data, doc = workspace["data"], workspace["document"]
    issued_at = (
        issued_at
        or (workspace.get("finalization") or {}).get("finalized_at")
        or "Pending forecaster approval"
    )
    return "\n".join(
        [
            "AWC Aviation Spot Forecast",
            f"Operation: {workspace['label']}",
            f"Location: {data['latitude']:.4f}, {data['longitude']:.4f}",
            f"Valid {utc(data['start_utc']):%Y-%m-%d %H:%MZ} to {utc(data['end_utc']):%Y-%m-%d %H:%MZ}",
            f"Guidance: {data.get('model', 'ECMWF_IFS')} {data['grid']}; cycle {data['cycle_utc']}",
            f"Prepared: {doc['prepared_at']}",
            f"Issued: {issued_at}",
            f"Draft: {identifier}; version {doc['version']}",
            "Sky heights: feet AGL. Winds: true direction/knots. FL: standard pressure altitude.",
            "",
            doc["body"],
        ]
    )


def resolve_group_time(code, start, end, minutes=False, allow_end=False):
    width = 6 if minutes else 4
    if not re.fullmatch(r"[0-9]{" + str(width) + r"}", code):
        raise FinalizationError(
            "Use complete DDHHMM FM times or DDHH conditional times."
        )
    day, hour = int(code[:2]), int(code[2:4])
    minute = int(code[4:]) if minutes else 0
    if minute > 59 or hour > 24 or (hour == 24 and (minutes or minute)):
        raise FinalizationError("Invalid forecast-group clock time.")
    candidates = []
    date = utc(start).date() - timedelta(days=1)
    while date <= utc(end).date():
        if date.day == day:
            valid = datetime(
                date.year, date.month, date.day, tzinfo=timezone.utc
            ) + timedelta(hours=hour, minutes=minute)
            if utc(start) <= valid and (
                valid <= utc(end) if allow_end else valid < utc(end)
            ):
                candidates.append(valid)
        date += timedelta(days=1)
    if len(candidates) != 1:
        raise FinalizationError(
            "Forecast-group time is outside the requested period or ambiguous."
        )
    return candidates[0]


def parse_conditions(text, partial=False):
    tokens = text.split()
    result = {"wind": None, "visibility": None, "weather": None, "sky": None}
    try:
        if tokens and tokens[0].endswith("KT"):
            original = tokens.pop(0)
            result["wind"] = normalize_wind(original)
            if result["wind"] != original:
                raise ElementError("Use normalized uppercase wind groups.")
        if tokens and (
            tokens[0].endswith("SM")
            or (len(tokens) > 1 and tokens[0].isdigit() and tokens[1].endswith("SM"))
        ):
            raw = tokens.pop(0)
            if not raw.endswith("SM"):
                raw += " " + tokens.pop(0)
            value, note = normalize_visibility(raw)
            if note or value != raw:
                raise ElementError(
                    "Use reportable TAF visibility groups (e.g. 3/4SM, 1 1/2SM, 2SM, 6SM or P6SM)."
                )
            result["visibility"] = value
        sky_index = next(
            (
                i
                for i, t in enumerate(tokens)
                if t == "SKC" or t.startswith(("FEW", "SCT", "BKN", "OVC", "VV"))
            ),
            len(tokens),
        )
        weather = tokens[:sky_index]
        sky = tokens[sky_index:]
        if weather:
            parsed = normalize_weather(" ".join(weather))
            if parsed == []:
                raise ElementError(
                    "Omit NONE from forecast text after reviewing no significant weather."
                )
            result["weather"] = parsed
        elif not partial:
            result["weather"] = []
        if sky:
            result["sky"] = normalize_sky(" ".join(sky))
            if result["sky"]["ceiling_state"] == "unknown":
                raise ElementError(
                    "Resolve unknown ceiling height before finalization."
                )
        if not partial and any(
            result[n] is None for n in ("wind", "visibility", "sky")
        ):
            raise ElementError("Every FM must include wind, visibility and sky.")
        if partial and all(v is None for v in result.values()):
            raise ElementError("Conditional groups need at least one condition.")
        if result["visibility"] not in {None, "P6SM"} and result["weather"] == []:
            raise ElementError(
                "Visibility of 6 SM or less requires a reviewed weather or obscuration code; do not infer fog automatically."
            )
    except ElementError as error:
        raise FinalizationError(str(error)) from None
    return result


def parse_body(body, data):
    if not body.strip() or len(body.encode("utf-8")) > 64000:
        raise FinalizationError("Provide forecast text of at most 64,000 UTF-8 bytes.")
    if (
        "[" in body
        or "]" in body
        or any(ord(c) < 32 and c not in "\r\n\t" for c in body)
    ):
        raise FinalizationError(
            "Resolve all placeholders and control characters before finalization."
        )
    start, end = utc(data["start_utc"]), utc(data["end_utc"])
    rows = []
    groups = []
    aloft = []
    remarks = []
    section = "surface"
    parent = None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Remarks: "):
            if len(line) > 500 or len(remarks) >= 8:
                raise FinalizationError(
                    "Keep remarks to eight lines of at most 500 characters."
                )
            remarks.append(line[9:])
            continue
        if line == "Winds aloft (standard pressure altitude; true direction, knots):":
            if section == "aloft":
                raise FinalizationError("Duplicate winds-aloft section.")
            section = "aloft"
            continue
        if section == "aloft":
            match = re.fullmatch(
                r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z) FL(\d{3}): (.+)", line
            )
            if not match:
                raise FinalizationError(
                    "Use a full UTC date/time, FL and plain-language wind in the aloft section."
                )
            try:
                valid = datetime.strptime(match[1], "%Y-%m-%d %H:%MZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise FinalizationError("Invalid winds-aloft calendar time.") from None
            fl, value = int(match[2]), match[3]
            wind_match = re.fullmatch(
                r"from (\d{3}) degrees true at (\d{1,3}) kt", value
            )
            unavailable = re.fullmatch(
                r"unavailable \(([^\[\]\x00-\x1f]{1,160})\)", value
            )
            if value == "calm (0 kt)":
                code = "00000KT"
            elif wind_match:
                try:
                    code = normalize_wind(f"{wind_match[1]}{int(wind_match[2]):02d}KT")
                except ElementError as error:
                    raise FinalizationError(str(error)) from None
            elif unavailable:
                code = None
            else:
                raise FinalizationError(
                    'Use "from DDD degrees true at NN kt", "calm (0 kt)" or an explicit "unavailable (reason)" aloft entry.'
                )
            aloft.append(
                {
                    "valid_utc": valid.isoformat(),
                    "flight_level": fl,
                    "wind": code,
                    "status": "manual" if code else "missing",
                    "reason": unavailable[1] if unavailable else None,
                    "forecaster_reviewed": True,
                }
            )
            continue
        fm = re.fullmatch(r"FM([0-9]{6})\s+(.+)", line)
        conditional = re.fullmatch(
            r"(TEMPO|PROB30)\s+([0-9]{4})/([0-9]{4})\s+(.+)", line
        )
        if fm:
            valid = resolve_group_time(fm[1], start, end, minutes=True)
            if rows and valid <= utc(rows[-1]["valid_utc"]):
                raise FinalizationError(
                    "FM times must increase without duplicate groups."
                )
            if not rows and valid != start:
                raise FinalizationError(
                    "The initial FM must match the exact requested start."
                )
            parent = len(rows)
            rows.append({"valid_utc": valid.isoformat(), **parse_conditions(fm[2])})
        elif conditional and parent is not None:
            a = resolve_group_time(conditional[2], start, end)
            b = resolve_group_time(conditional[3], start, end, allow_end=True)
            if a >= b:
                raise FinalizationError("Conditional-group end must follow its start.")
            groups.append(
                {
                    "kind": conditional[1],
                    "start": a.isoformat(),
                    "end": b.isoformat(),
                    "parent": parent,
                    **parse_conditions(conditional[4], partial=True),
                }
            )
        else:
            raise FinalizationError(
                "Use complete FM groups, TEMPO/PROB30 groups, plain-language FL winds or Remarks: lines."
            )
    if not rows:
        raise FinalizationError("A complete initial FM group is required.")
    for group in groups:
        i = group["parent"]
        lower = utc(rows[i]["valid_utc"])
        upper = utc(rows[i + 1]["valid_utc"]) if i + 1 < len(rows) else end
        if not lower <= utc(group["start"]) < utc(group["end"]) <= upper:
            raise FinalizationError(
                "Keep conditional groups within their attached FM period; split groups that cross a prevailing change."
            )
        effective = {
            n: group[n] if group[n] is not None else rows[i][n]
            for n in ("wind", "visibility", "weather", "sky")
        }
        if effective["visibility"] != "P6SM" and not effective["weather"]:
            raise FinalizationError(
                "Conditional visibility of 6 SM or less requires a weather/obscuration decision."
            )
    expected = {
        (r["valid_utc"], fl)
        for r in data.get("window_rows", [])
        if not r["support_only"]
        for fl in data.get("requested_flight_levels", [])
    }
    actual = [(r["valid_utc"], r["flight_level"]) for r in aloft]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        raise FinalizationError(
            "Include every requested FL/time exactly once, with a wind or explicit unavailable reason; omit unrequested levels and end-support rows."
        )
    return {
        "rows": rows,
        "groups": groups,
        "aloft": aloft,
        "remarks": remarks,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
    }


def contributions(parsed, product):
    result = deepcopy(parsed)
    for row in result["rows"]:
        prior = [
            r for r in product["rows"] if utc(r["valid_utc"]) <= utc(row["valid_utc"])
        ]
        basis = prior[-1] if prior else None
        row["element_quality"] = {}
        for name in ("wind", "visibility", "weather", "sky"):
            source = basis[name] if basis else None
            if (
                source
                and basis["valid_utc"] == row["valid_utc"]
                and source["value"] == row[name]
                and source["reviewed"]
            ):
                row["element_quality"][name] = deepcopy(source)
            else:
                row["element_quality"][name] = {
                    "status": "manual",
                    "value": deepcopy(row[name]),
                    "reviewed": True,
                    "provenance": {
                        "method": "final_text_entry",
                        "original": deepcopy(source),
                    },
                }
    return result

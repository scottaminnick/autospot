"""Structured, editable point spot forecast; unresolved elements remain explicit."""

from copy import deepcopy

from weather.aviation import (
    ElementError,
    wind,
    normalize_wind,
    normalize_visibility,
    normalize_weather,
    normalize_sky,
    precipitation_context,
    gust_context,
)
from weather.coordinates import utc, number

ELEMENTS = ("wind", "visibility", "weather", "sky")
PLACEHOLDERS = {
    "wind": "[WIND REVIEW REQUIRED]",
    "visibility": "[VISIBILITY REQUIRED]",
    "weather": "[WEATHER REVIEW REQUIRED]",
    "sky": "[SKY REQUIRED]",
}


def element(value=None, status="missing", reason=None, reviewed=False, provenance=None):
    return dict(
        value=value,
        status=status,
        reason=reason,
        reviewed=reviewed,
        provenance=provenance,
    )


def initialize_product(data):
    start, end = utc(data["start_utc"]), utc(data["end_utc"])
    by_time = {utc(r["valid_utc"]): r for r in data.get("window_rows", data["rows"])}
    times = sorted({start, *(t for t in by_time if start < t < end)})
    rows = []
    for valid in times:
        fields = (by_time.get(valid) or {}).get("fields", {})
        token = wind(fields)
        raw_winds = [deepcopy(fields.get(k, {})) for k in ("u10", "v10")]
        clouds = {
            name: number(fields.get(name, {}), "fraction")
            for name in ("tcc", "lcc", "mcc", "hcc")
        }
        temperatures = {
            name: (
                v - 273.15
                if (v := number(fields.get(name, {}), "K")) is not None
                else None
            )
            for name in ("temperature2", "dewpoint2", "skin_temperature")
        }
        rows.append(
            {
                "valid_utc": valid.isoformat(),
                "wind": element(
                    token,
                    "derived" if token else "missing",
                    reason="wind_and_gust_review_required",
                    provenance={"method": "uv_to_true_wind", "sources": raw_winds},
                ),
                "visibility": element(
                    status="unsupported", reason="no_validated_visibility_source"
                ),
                "weather": element(reason="forecaster_weather_review_required"),
                "sky": element(
                    status="unsupported",
                    reason="cloud_fractions_do_not_define_cloud_bases_or_ceiling",
                ),
                "context": {
                    "precipitation": precipitation_context(fields, valid),
                    "gust": gust_context(fields, valid),
                    "cloud_fractions": clouds,
                    "temperature_c": temperatures,
                    "cape_j_kg": number(fields.get("cape", {}), "J/kg"),
                },
                "notes": [],
            }
        )
    return {
        "rows": rows,
        "groups": [],
        "complete": False,
        "unresolved": required_work(rows),
        "provenance_policy": "original_model_guidance_and_forecaster_entries_retained",
    }


def unresolved_element(row, name):
    field = row[name]
    return (
        field["value"] is None
        or not field["reviewed"]
        or (name == "sky" and field["value"].get("ceiling_state") == "unknown")
    )


def required_work(rows):
    return [
        {
            "valid_utc": r["valid_utc"],
            "elements": [n for n in ELEMENTS if unresolved_element(r, n)],
        }
        for r in rows
        if any(unresolved_element(r, n) for n in ELEMENTS)
    ]


def manual(name, value, raw, original):
    return element(
        value,
        "manual",
        reviewed=True,
        provenance={
            "method": "forecaster_entry",
            "entered": raw,
            "original": deepcopy(original),
            "source_element": name,
        },
    )


def apply_elements(product, entries, groups, start, end):
    """Validate every entry before replacing the product. Original context is immutable."""
    result = deepcopy(product)
    if len(entries) != len(result["rows"]):
        raise ElementError("The element list does not match this draft.")
    for row, entry in zip(result["rows"], entries):
        row["notes"] = []
        token = normalize_wind(entry.get("wind", ""))
        reviewed = bool(entry.get("wind_reviewed"))
        if token != row["wind"]["value"]:
            original = (row["wind"].get("provenance") or {}).get(
                "original", row["wind"]
            )
            row["wind"] = manual("wind", token, entry.get("wind", ""), original)
        row["wind"]["reviewed"] = reviewed and token is not None
        row["wind"]["reason"] = (
            None
            if row["wind"]["reviewed"]
            else "wind_and_gust_review_required"
            if token
            else "wind_unavailable"
        )
        if token is None:
            row["wind"]["status"] = "missing"
        for name, parser in [
            ("visibility", normalize_visibility),
            ("weather", normalize_weather),
            ("sky", normalize_sky),
        ]:
            raw = entry.get(name, "")
            if name == "visibility":
                value, note = parser(raw)
                if note:
                    row["notes"].append(note)
            else:
                value = parser(raw)
            if value is None:
                row[name] = element(
                    status="unsupported"
                    if name in {"visibility", "sky"}
                    else "missing",
                    reason="forecaster_completion_required",
                )
            elif value != row[name]["value"] or not row[name]["reviewed"]:
                original = (row[name].get("provenance") or {}).get(
                    "original", row[name]
                )
                row[name] = manual(name, value, raw, original)
        if (
            row["visibility"]["value"] not in {None, "P6SM"}
            and row["weather"]["value"] == []
        ):
            row["notes"].append(
                "Visibility is 6 SM or less with NONE selected: review the cause and appropriate weather/obscuration code."
            )
    result["groups"] = validate_groups(groups, start, end)
    result["unresolved"] = required_work(result["rows"])
    result["complete"] = not result["unresolved"]
    return result


def validate_groups(groups, start, end):
    if len(groups) > 12:
        raise ElementError("Use at most 12 conditional groups.")
    result = []
    for group in groups:
        kind = group.get("kind", "").strip()
        if not kind:
            if any(str(v).strip() for k, v in group.items() if k != "kind"):
                raise ElementError(
                    "Choose TEMPO or PROB30 for a populated conditional group."
                )
            continue
        if kind not in {"TEMPO", "PROB30"}:
            raise ElementError("Choose TEMPO or PROB30.")
        try:
            a, b = (utc(group[n]) for n in ("start", "end"))
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ElementError(
                "Conditional groups require full UTC start and end dates/times."
            ) from None
        if not utc(start) <= a < b <= utc(end) or any(
            t.minute or t.second or t.microsecond for t in (a, b)
        ):
            raise ElementError(
                "Conditional groups must use whole UTC hours within the requested period."
            )
        vis, note = normalize_visibility(group.get("visibility", ""))
        weather = normalize_weather(group.get("weather", ""))
        sky = normalize_sky(group.get("sky", ""))
        if sky is not None and sky["ceiling_state"] == "unknown":
            raise ElementError(
                "Provide a known vertical visibility for conditional groups, or leave sky blank to inherit it."
            )
        wind_value = normalize_wind(group.get("wind", ""))
        if weather == []:
            raise ElementError(
                "Omit conditional weather to inherit it; use a prevailing group to forecast no significant weather."
            )
        values = {"wind": wind_value, "visibility": vis, "weather": weather, "sky": sky}
        if all(v is None for v in values.values()):
            raise ElementError(
                "Provide at least one condition in a TEMPO/PROB30 group."
            )
        result.append(
            {
                "kind": kind,
                "start": a.isoformat(),
                "end": b.isoformat(),
                **values,
                "status": "manual",
                "method": "forecaster_conditional_group",
                "notes": [note] if note else [],
            }
        )
    return sorted(result, key=lambda g: (utc(g["start"]), utc(g["end"]), g["kind"]))


def normalized_signature(row):
    def value(name):
        v = row[name]["value"]
        if isinstance(v, dict):
            return tuple(v["tokens"])
        if isinstance(v, list):
            return tuple(v)
        return v

    return tuple(value(n) for n in ELEMENTS)


def prevailing_rows(product):
    result = []
    previous = None
    for row in product["rows"]:
        signature = normalized_signature(row)
        unresolved = any(unresolved_element(row, n) for n in ELEMENTS)
        # Never compress an unresolved sample, or suppress recovery from a gap.
        if not result or unresolved or previous is None or signature != previous:
            result.append(row)
        previous = None if unresolved else signature
    return result


def condition_text(values, partial=False, *, wind_review_marker=True):
    tokens = []
    for name in ELEMENTS:
        value = values[name] if partial else values[name]["value"]
        if value is None:
            if not partial:
                tokens.append(PLACEHOLDERS[name])
        elif name == "sky":
            tokens += value["tokens"]
        elif name == "weather":
            tokens += value
        else:
            tokens.append(value)
        if (
            name == "wind"
            and not partial
            and wind_review_marker
            and value is not None
            and not values[name]["reviewed"]
        ):
            tokens.append("[WIND/GUST REVIEW REQUIRED]")
    return " ".join(tokens)


def format_prevailing(product, end, *, wind_review_marker=True):
    rows = prevailing_rows(product)
    lines = []
    for i, row in enumerate(rows):
        valid = utc(row["valid_utc"])
        next_time = utc(rows[i + 1]["valid_utc"]) if i + 1 < len(rows) else utc(end)
        conditions = condition_text(row, wind_review_marker=wind_review_marker)
        lines.append(f"FM{valid:%d%H%M} {conditions}")
        for group in product["groups"]:
            a, b = max(valid, utc(group["start"])), min(next_time, utc(group["end"]))
            if a < b:
                lines.append(
                    f"  {group['kind']} {a:%d%H}/{b:%d%H} {condition_text(group, partial=True)}"
                )
    return lines


def editor_rows(product):
    return [
        dict(
            row,
            index=i,
            wind_input=row["wind"]["value"] or "",
            visibility_input=row["visibility"]["value"] or "",
            weather_input=" ".join(row["weather"]["value"])
            if row["weather"]["value"]
            else "NONE"
            if row["weather"]["value"] == []
            else "",
            sky_input=" ".join(row["sky"]["value"]["tokens"])
            if row["sky"]["value"]
            else "",
        )
        for i, row in enumerate(product["rows"])
    ]

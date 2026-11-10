"""UTC sampling and standard pressure-altitude winds; no actual-MSL mode."""

from bisect import bisect_left
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from weather.fields import FieldValue, State, PRESSURE_LEVELS

CONTINUOUS = {"u10", "v10", "temperature2", "dewpoint2", "surface_pressure"}
UNITS = {
    "u10": "m/s",
    "v10": "m/s",
    "temperature2": "K",
    "dewpoint2": "K",
    "surface_pressure": "Pa",
}
G0, R_AIR, T0, P0, LAPSE = 9.80665, 8314.32 / 28.9644, 288.15, 101325.0, 0.0065


def utc(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("UTC offset required")
    return result.astimezone(timezone.utc)


def flight_level_pressure(fl):
    """Pa at geopotential standard pressure altitude, FL010–450 (USSA 1976)."""
    if type(fl) is not int or not 10 <= fl <= 450:
        raise ValueError("Flight level must be an integer from 10 to 450")
    height = fl * 100 * 0.3048
    tropopause = P0 * (216.65 / T0) ** (G0 / (R_AIR * LAPSE))
    if height <= 11000:
        return P0 * (1 - LAPSE * height / T0) ** (G0 / (R_AIR * LAPSE))
    return tropopause * math.exp(-G0 * (height - 11000) / (R_AIR * 216.65))


def pressure_brackets(fl):
    pressure = flight_level_pressure(fl) / 100
    levels = sorted(PRESSURE_LEVELS)
    index = bisect_left(levels, pressure)
    if index == 0 or index == len(levels):
        raise ValueError("No enclosing model pressure levels")
    return levels[index - 1], levels[index]


def number(field, unit):
    value = field.get("value")
    return (
        value
        if (
            field.get("status") in {"direct", "derived"}
            and field.get("unit") == unit
            and type(value) in (int, float)
            and math.isfinite(value)
        )
        else None
    )


def source_identity(field):
    p = field.get("provenance") or {}
    # Providers may differ; grid, cycle and sampled point may not.
    keys = ("model", "grid", "cycle_utc", "sample_latitude", "sample_longitude")
    if any(p.get(k) is None for k in keys):
        return None
    return tuple(p[k] for k in keys)


def compatible(fields):
    identities = [source_identity(f) for f in fields]
    return bool(identities) and identities[0] is not None and len(set(identities)) == 1


def valid_at(field, valid):
    try:
        return utc((field.get("provenance") or {}).get("valid_utc")) == utc(valid)
    except (ValueError, TypeError, AttributeError):
        return False


def unavailable(name, reason, unit=None, state=State.MISSING, provenance=None):
    return FieldValue(
        name, state, unit=unit, reason=reason, provenance=provenance
    ).as_dict()


def sample_time(rows, valid):
    """Exact native slot, or two adjacent native slots <=3 h; never fill a native gap."""
    valid = utc(valid)
    ordered = [
        (utc(r["valid_utc"]), r)
        for r in sorted(rows, key=lambda r: utc(r["valid_utc"]))
    ]
    by_time = dict(ordered)
    if len(by_time) != len(ordered):
        raise ValueError("Duplicate native valid time")
    if valid in by_time:
        return deepcopy(by_time[valid])
    times = list(by_time)
    i = bisect_left(times, valid)
    brackets = ordered[i - 1 : i + 1] if 0 < i < len(times) else []
    fields = {}
    names = set().union(*(r["fields"] for _, r in ordered)) if ordered else set()
    for name in names:
        allowed = name in CONTINUOUS or name.startswith(("u_", "v_"))
        unit = UNITS.get(name, "m/s" if name.startswith(("u_", "v_")) else None)
        reason = (
            "time_interpolation_not_permitted"
            if not allowed
            else "time_brackets_unavailable"
        )
        fields[name] = unavailable(name, reason, unit)
        if not allowed or len(brackets) != 2:
            continue
        (left, a), (right, b) = brackets
        if (right - left).total_seconds() > 3 * 3600:
            fields[name]["reason"] = "time_bracket_exceeds_three_hours"
            continue
        sources = [r["fields"].get(name, {}) for r in (a, b)]
        values = [number(f, unit) for f in sources]
        if any(v is None for v in values) or not compatible(sources):
            continue
        if any(not valid_at(f, t) for f, t in zip(sources, (left, right))):
            fields[name]["reason"] = "source_time_mismatch"
            continue
        if name.startswith(("u_", "v_")):
            pressure = int(name.split("_")[1]) * 100
            surfaces = [r["fields"].get("surface_pressure", {}) for r in (a, b)]
            if any(
                number(s, "Pa") is None
                or number(s, "Pa") < pressure
                or not valid_at(s, t)
                or not compatible([s, f])
                for s, f, t in zip(surfaces, sources, (left, right))
            ):
                fields[name]["reason"] = "time_profile_surface_check_failed"
                continue
        weight = (valid - left).total_seconds() / (right - left).total_seconds()
        p = {
            k: sources[0]["provenance"][k]
            for k in (
                "model",
                "grid",
                "cycle_utc",
                "sample_latitude",
                "sample_longitude",
            )
        }
        p.update(
            valid_utc=valid.isoformat(),
            method="linear_time_components"
            if name.startswith(("u", "v"))
            else "linear_time",
            source_times_utc=[left.isoformat(), right.isoformat()],
            weight=weight,
            sources=[deepcopy(f["provenance"]) for f in sources],
        )
        fields[name] = FieldValue(
            name,
            State.DERIVED,
            values[0] + weight * (values[1] - values[0]),
            unit,
            provenance=p,
        ).as_dict()
    return {
        "valid_utc": valid.isoformat(),
        "support_only": False,
        "interpolated": True,
        "fields": fields,
    }


def flight_level_wind(fields, fl, valid):
    pressure = flight_level_pressure(fl)
    low, high = pressure_brackets(fl)
    p = {
        "flight_level": fl,
        "vertical_reference": "standard_pressure_altitude",
        "target_pressure_pa": pressure,
        "bracket_hpa": [low, high],
        "valid_utc": utc(valid).isoformat(),
        "method": "linear_uv_log_pressure",
    }
    result = {"flight_level": fl, "pressure_pa": pressure, "provenance": p}
    surface = fields.get("surface_pressure", {})
    sp = number(surface, "Pa")
    if sp is None or sp <= 0:
        reason = "surface_pressure_unknown"
    elif pressure > sp:
        reason = "target_below_model_surface"
    elif high * 100 > sp:
        reason = "bracket_below_model_surface"
    else:
        sources = [
            fields.get(f"{param}_{level}", {})
            for level in (low, high)
            for param in ("u", "v")
        ]
        values = [number(f, "m/s") for f in sources]
        if any(v is None for v in values):
            reason = "profile_brackets_unavailable"
        elif not compatible([surface, *sources]):
            reason = "profile_source_mismatch"
        elif any(not valid_at(f, valid) for f in [surface, *sources]):
            reason = "profile_time_mismatch"
        else:
            weight = math.log(pressure / (low * 100)) / math.log(high / low)
            u, v = [values[k] + weight * (values[k + 2] - values[k]) for k in (0, 1)]
            p.update(
                weight=weight,
                sources=[deepcopy(f["provenance"]) for f in [surface, *sources]],
            )
            return dict(result, status="derived", u=u, v=v, unit="m/s", reason=None)
    return dict(result, status="missing", u=None, v=None, unit="m/s", reason=reason)


def enrich(data, flight_levels=()):
    """Retain native rows; create separate exact-window rows and FL diagnostics."""
    start, end = utc(data["start_utc"]), utc(data["end_utc"])
    expected = sorted(
        utc(data["cycle_utc"]) + timedelta(hours=s) for s in data["expected_steps"]
    )
    native = {utc(r["valid_utc"]): r for r in data["rows"]}
    if len(native) != len(data["rows"]) or set(native) - set(expected):
        raise ValueError("Invalid native timeline")
    names = set().union(*(r["fields"] for r in data["rows"]))
    data["rows"] = [
        native.get(t)
        or {
            "valid_utc": t.isoformat(),
            "step": (t - utc(data["cycle_utc"])).total_seconds() / 3600,
            "fields": {
                n: unavailable(n, "expected_slot_missing", UNITS.get(n)) for n in names
            },
        }
        for t in expected
    ]
    for row in data["rows"]:
        row["support_only"] = not start <= utc(row["valid_utc"]) < end
    times = sorted({start, end, *(t for t in expected if start < t < end)})
    data["window_rows"] = []
    for valid in times:
        row = sample_time(data["rows"], valid)
        row["support_only"] = valid == end
        row["flight_levels"] = [
            flight_level_wind(row["fields"], fl, valid) for fl in flight_levels
        ]
        data["window_rows"].append(row)
    data["requested_flight_levels"] = list(flight_levels)
    field = data.get("orography") or {}
    z = number(field, "m2/s2")
    data["model_elevation"] = {
        "status": "derived" if z is not None else "missing",
        "value": z / G0 if z is not None else None,
        "unit": "geopotential_m",
        "method": "surface_geopotential / standard_gravity",
        "source": field.get("provenance"),
        "description": "Model orography; not station elevation or an AGL conversion",
    }
    return data

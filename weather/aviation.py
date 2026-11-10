"""Aviation element normalization and explicit native-time model context."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math
import re

from weather.coordinates import compatible, number, valid_at, utc


class ElementError(ValueError):
    pass


def wind_components(u, v):
    if not all(type(x) in (int, float) and math.isfinite(x) for x in (u, v)):
        return None
    speed = math.floor(math.hypot(u, v) * 3600 / 1852 + 0.5)
    if speed > 999:
        return None
    direction = (
        math.floor((math.degrees(math.atan2(-u, -v)) % 360) / 10 + 0.5) * 10 % 360
        or 360
    )
    return "00000KT" if speed == 0 else f"{direction:03d}{speed:02d}KT"


def wind(fields):
    sources = [fields.get(n, {}) for n in ("u10", "v10")]
    u, v = [number(f, "m/s") for f in sources]
    return (
        wind_components(u, v)
        if u is not None and v is not None and compatible(sources)
        else None
    )


def normalize_wind(raw):
    token = raw.strip().upper()
    if not token:
        return None
    match = re.fullmatch(r"(VRB|[0-9]{3})([0-9]{2,3})(?:G([0-9]{2,3}))?KT", token)
    if not match:
        raise ElementError("Use a wind group such as 23020G35KT, VRB03KT or 00000KT.")
    direction, speed, gust = (
        match[1],
        int(match[2]),
        int(match[3]) if match[3] else None,
    )
    if direction != "VRB" and (int(direction) > 360 or int(direction) % 10):
        raise ElementError(
            "Wind direction must be true degrees rounded to tens, 010–360, or VRB."
        )
    if speed == 0 and (direction != "000" or gust is not None):
        raise ElementError("Use 00000KT for calm wind.")
    if speed and direction == "000":
        raise ElementError("Use 360 for north wind; 000 is reserved for calm.")
    if gust is not None and gust <= speed:
        raise ElementError("Peak gust must exceed the sustained wind speed.")
    return (
        f"{direction}{speed:02d}" + (f"G{gust:02d}" if gust is not None else "") + "KT"
    )


VISIBILITY = [
    (Decimal(v), token)
    for v, token in [
        ("0", "0SM"),
        (".25", "1/4SM"),
        (".5", "1/2SM"),
        (".75", "3/4SM"),
        ("1", "1SM"),
        ("1.5", "1 1/2SM"),
        ("2", "2SM"),
        ("3", "3SM"),
        ("4", "4SM"),
        ("5", "5SM"),
        ("6", "6SM"),
    ]
]


def normalize_visibility(raw):
    value = raw.strip().upper()
    if not value:
        return None, None
    if value == "P6SM":
        return value, None
    literal = value.removesuffix("SM").strip()
    try:
        if re.fullmatch(r"(?:[0-9]{1,3}(?:\.[0-9]{1,4})?|\.[0-9]{1,4})", literal):
            miles = Decimal(literal)
        elif re.fullmatch(r"(?:[0-9]{1,2} )?[0-9]{1,2}/[1-9][0-9]?", literal):
            parts = literal.split()
            frac = Fraction(parts[-1])
            miles = Decimal(frac.numerator) / Decimal(frac.denominator)
            if len(parts) == 2:
                miles += Decimal(parts[0])
        else:
            raise ValueError()
        if not miles.is_finite() or not 0 <= miles <= 999:
            raise ValueError()
    except (ValueError, InvalidOperation, ZeroDivisionError):
        raise ElementError(
            "Enter visibility in statute miles (e.g. .75, 1.5, 6 or P6SM)."
        ) from None
    if miles > 6:
        return "P6SM", None
    selected, token = next((n, t) for n, t in reversed(VISIBILITY) if n <= miles)
    return (
        token,
        f"Visibility {literal} SM rounded down to {token}."
        if selected != miles
        else None,
    )


PRECIP = r"(?:DZ|RA|SN|SG|IC|PL|GR|GS){1,3}"
OBSCURATION = {
    "BR",
    "FG",
    "HZ",
    "FU",
    "VA",
    "DU",
    "SA",
    "PY",
    "PO",
    "SQ",
    "FC",
    "SS",
    "DS",
    "FZFG",
    "MIFG",
    "PRFG",
    "BCFG",
    "DRDU",
    "DRSA",
    "DRSN",
    "BLDU",
    "BLSA",
    "BLSN",
    "VCTS",
    "VCSH",
    "VCFG",
    "VCSS",
    "VCDS",
    "VCPO",
    "VCFC",
    "VCBLDU",
    "VCBLSA",
    "VCBLSN",
    "TS",
}


def normalize_weather(raw):
    value = " ".join(raw.upper().split())
    if not value:
        return None
    if value == "NONE":
        return []
    tokens = value.split()
    if len(tokens) > 3 or len(set(tokens)) != len(tokens):
        raise ElementError(
            "Use up to three distinct weather groups, or NONE after reviewing no significant weather."
        )
    for token in tokens:
        if token in OBSCURATION:
            continue
        match = re.fullmatch(r"[-+]?(SH|TS|FZ)?(" + PRECIP + r")", token)
        if not match or (match[1] == "FZ" and match[2] not in {"RA", "DZ"}):
            raise ElementError(
                "Use supported aviation weather codes (e.g. -RA BR, -FZRA, -RASN or NONE)."
            )
        phenomena = [match[2][i : i + 2] for i in range(0, len(match[2]), 2)]
        if len(set(phenomena)) != len(phenomena):
            raise ElementError(
                "Do not repeat a precipitation type within a weather group."
            )
        if match[1] == "SH" and any(p in match[2] for p in ("DZ", "IC", "SG")):
            raise ElementError(
                "This shower/precipitation combination needs a different aviation code."
            )
    return sorted(tokens, key=lambda t: (t in OBSCURATION, t))


def normalize_sky(raw):
    tokens = raw.upper().split()
    if not tokens:
        return None
    if tokens == ["SKC"]:
        return {
            "tokens": tokens,
            "ceiling_state": "no_ceiling",
            "ceiling_ft_agl": None,
            "lowest_base_ft_agl": None,
        }
    if len(tokens) > 12 or len(set(tokens)) != len(tokens):
        raise ElementError("Use distinct cloud layers, up to 12, or SKC.")
    layers = []
    for token in tokens:
        match = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)([0-9]{3}|///)(CB|TCU)?", token)
        if (
            not match
            or (match[2] == "///" and match[1] != "VV")
            or (match[1] == "VV" and (len(tokens) > 1 or match[3]))
        ):
            raise ElementError(
                "Use AGL cloud layers such as SCT020 BKN030, SKC, VV003 or VV///."
            )
        height = int(match[2]) * 100 if match[2] != "///" else None
        layers.append((height, match[1], token))
    layers.sort(key=lambda x: x[0] if x[0] is not None else -1)
    ceilings = [
        h for h, amount, _ in layers if amount in {"BKN", "OVC", "VV"} and h is not None
    ]
    unknown = any(h is None for h, _, _ in layers)
    return {
        "tokens": [t for _, _, t in layers],
        "ceiling_state": "unknown"
        if unknown
        else "ceiling"
        if ceilings
        else "no_ceiling",
        "ceiling_ft_agl": min(ceilings) if ceilings else None,
        "lowest_base_ft_agl": min(h for h, _, _ in layers if h is not None)
        if not unknown
        else None,
    }


IFS_TYPES = {
    0: ("No precipitation", None),
    1: ("Rain", "RA"),
    3: ("Freezing rain", "FZRA"),
    5: ("Snow", "SN"),
    6: ("Wet snow", "SN"),
    7: ("Rain/snow mixture", "RASN"),
    8: ("Ice pellets", "PL"),
    12: ("Freezing drizzle", "FZDZ"),
}


def precipitation_context(fields, valid):
    """Model diagnosis and instantaneous rain-rate category, never an issued group."""
    typ, rate = fields.get("precip_type", {}), fields.get("precip_rate", {})
    code, amount = number(typ, "code_4.201"), number(rate, "kg/m2/s")
    result = {
        "status": "missing",
        "phase": None,
        "phase_code": None,
        "suggestion": None,
        "rate_mm_h": amount * 3600 if amount is not None and amount >= 0 else None,
        "reason": "precipitation_type_or_rate_unavailable",
        "sources": [deepcopy(typ), deepcopy(rate)],
    }
    if code is None or amount is None or amount < 0:
        return result
    if code not in IFS_TYPES or code != int(code):
        return dict(
            result,
            status="unsupported",
            reason="precipitation_code_not_validated_for_IFS",
        )
    if (
        typ.get("status") != "direct"
        or rate.get("status") != "direct"
        or not compatible([typ, rate])
        or not all(valid_at(f, valid) for f in [typ, rate])
    ):
        return dict(
            result, status="invalid", reason="precipitation_source_or_time_mismatch"
        )
    phase, phase_code = IFS_TYPES[code]
    result.update(status="derived", phase=phase, phase_code=phase_code)
    if (code == 0) != (amount == 0):
        return dict(result, status="invalid", reason="precipitation_type_rate_conflict")
    if code == 0:
        return dict(
            result, reason="model_dry_at_this_native_time_not_a_no_weather_forecast"
        )
    if code == 1:
        # Rate in in/h. Floating tolerance only accommodates the unit conversion
        # of exact source thresholds, never a meteorological threshold margin.
        mm_h = amount * 3600
        prefix = (
            "-"
            if mm_h <= 2.54 or math.isclose(mm_h, 2.54, rel_tol=1e-12)
            else ""
            if mm_h <= 7.62 or math.isclose(mm_h, 7.62, rel_tol=1e-12)
            else "+"
        )
        return dict(
            result,
            suggestion=prefix + "RA",
            reason="instantaneous_rain_rate_candidate_requires_forecaster_acceptance",
        )
    return dict(
        result,
        reason="phase_and_intensity_require_forecaster_review_no_SWE_to_snow_intensity_conversion",
    )


def gust_context(fields, valid):
    gust = fields.get("gust10", {})
    peak = number(gust, "m/s")
    p = gust.get("provenance") or {}
    result = {
        "peak_kt": None,
        "interval_start_utc": p.get("interval_start_utc"),
        "interval_end_utc": p.get("interval_end_utc"),
        "reason": "gust_unavailable",
    }
    if (
        peak is None
        or peak < 0
        or gust.get("status") != "direct"
        or not valid_at(gust, valid)
    ):
        return result
    try:
        if not utc(p["interval_start_utc"]) < utc(p["interval_end_utc"]) == utc(valid):
            return dict(result, reason="gust_interval_invalid")
    except (KeyError, TypeError, ValueError, AttributeError):
        return dict(result, reason="gust_interval_invalid")
    return dict(
        result,
        peak_kt=peak * 3600 / 1852,
        reason="preceding_interval_maximum_review_future_gust_forecast_manually",
    )

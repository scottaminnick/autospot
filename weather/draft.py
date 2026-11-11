"""Minimal honest draft and raw guidance while aviation diagnoses are rebuilt."""

import base64
from datetime import datetime
import io
import math
from weather.aviation import wind, wind_components
from weather.document import aloft_lines
from weather.product import initialize_product, format_prevailing


def direct(fields, name):
    field = fields.get(name, {})
    return field.get("value") if field.get("status") == "direct" else None


def build_draft(data, label, product=None):
    product = product or initialize_product(data)
    start, end = (datetime.fromisoformat(data[k]) for k in ["start_utc", "end_utc"])
    lines = [
        "DRAFT — FORECASTER ELEMENT REVIEW RECORDED; NOT FINALIZED"
        if product["complete"]
        else "DRAFT — FORECASTER COMPLETION REQUIRED",
        f"Operation: {label}",
        f"Location: {data['latitude']:.4f}, {data['longitude']:.4f}",
        f"Valid {start:%Y-%m-%d %HZ} to {end:%Y-%m-%d %HZ}",
        f"Guidance: ECMWF IFS 0.25°, cycle {data['cycle_utc']}",
        "Sky heights: feet AGL. Winds: true direction/knots; FL winds use standard pressure altitude.",
        "Domain requires forecaster verification.",
        f"Reference geometry: {data.get('domain', {}).get('classification', 'unknown_review')}; "
        f"recorded disposition: {data.get('domain', {}).get('disposition', 'pending')}.",
        data.get("domain", {}).get("message", "Verify service authority manually."),
        "",
    ]
    lines += format_prevailing(product, data["end_utc"])
    if data.get("requested_flight_levels"):
        lines += [
            "",
            "Winds aloft — flight levels (standard pressure altitude); true direction/knots:",
        ]
        for row in data["window_rows"]:
            if row["support_only"]:
                continue
            for level in row["flight_levels"]:
                value = (
                    wind_components(level["u"], level["v"])
                    if level["status"] == "derived"
                    else f"[UNAVAILABLE: {level['reason']}]"
                )
                lines.append(
                    f"{datetime.fromisoformat(row['valid_utc']):%Y-%m-%d %H%MZ} FL{level['flight_level']:03d}: {value}"
                )
    lines += [
        "",
        "Native times and exact request boundaries are preserved; gaps require manual completion.",
        "Boundary U/V, temperature/dewpoint and pressure may be linearly interpolated over adjacent native slots ≤3 hours; source times are listed below.",
        "Gust maxima retain source intervals in the guidance table; no automatic gust group is inferred.",
        "Forecaster-entered or accepted weather, visibility and sky are recorded separately from original model guidance.",
    ]
    for row in data.get("window_rows", []):
        derived = [
            (name, f) for name, f in row["fields"].items() if f["status"] == "derived"
        ]
        if derived:
            p = derived[0][1]["provenance"]
            lines.append(
                f"Time interpolation at {row['valid_utc']}: {', '.join(p['source_times_utc'])}; fields: {', '.join(n for n, _ in derived)}."
            )
    return "\n".join(lines)


def build_spot_report(data, label):
    """Editable first guess with the request and model identification in the text."""
    start = datetime.fromisoformat(data["start_utc"])
    end = datetime.fromisoformat(data["end_utc"])
    cycle = datetime.fromisoformat(data["cycle_utc"])
    lat, lon = data["latitude"], data["longitude"]
    location = f"{abs(lat):.4f}{'S' if lat < 0 else 'N'}/{abs(lon):.4f}{'W' if lon < 0 else 'E'}"
    product = initialize_product(data)
    return "\n".join(
        [
            f"SAR: {label}  LOC: {location}",
            f"VALID {start:%Y-%m-%d %H:%MZ} TO {end:%Y-%m-%d %H:%MZ}",
            f"DATA BASED ON ECMWF IFS 0.25° {cycle:%Y-%m-%d %H:%MZ}",
            "",
            *format_prevailing(product, data["end_utc"]),
            *aloft_lines(data),
        ]
    )


def guidance_rows(data):
    rows = []
    for row in data["rows"]:
        fields = row["fields"]
        speed = None
        if direct(fields, "u10") is not None and direct(fields, "v10") is not None:
            speed = (
                math.hypot(direct(fields, "u10"), direct(fields, "v10")) * 3600 / 1852
            )
        gust = fields.get("gust10", {})
        interval = gust.get("provenance") or {}
        problems = [
            f"{k}: {v['status']} ({v.get('reason') or 'review required'})"
            for k, v in fields.items()
            if v["status"] not in {"direct", "not_applicable"}
        ]
        rows.append(
            {
                "valid_utc": row["valid_utc"],
                "support_only": row["support_only"],
                "wind": wind(fields),
                "speed": speed,
                "gust": direct(fields, "gust10") * 3600 / 1852
                if direct(fields, "gust10") is not None
                else None,
                "gust_interval": (
                    interval.get("interval_start_utc"),
                    interval.get("interval_end_utc"),
                ),
                "cloud": direct(fields, "tcc") * 100
                if direct(fields, "tcc") is not None
                else None,
                "rate": direct(fields, "precip_rate") * 3600
                if direct(fields, "precip_rate") is not None
                else None,
                "problems": problems,
                "providers": sorted(
                    {
                        (v.get("provenance") or {}).get("provider")
                        for v in fields.values()
                        if (v.get("provenance") or {}).get("provider")
                    }
                ),
            }
        )
    return rows


def chart(data):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.dates import DateFormatter

    rows = guidance_rows(data)
    times = [datetime.fromisoformat(r["valid_utc"]) for r in rows]

    def values(key):
        return [r[key] if r[key] is not None else float("nan") for r in rows]

    figure = Figure(figsize=(10, 8), layout="constrained")
    axes = figure.subplots(4, 1, sharex=True)
    axes[0].plot(times, values("speed"), marker="o", label="10 m sustained wind")
    axes[0].plot(
        times,
        values("gust"),
        marker="x",
        linestyle="none",
        label="Gust maxima — source intervals in table",
    )
    axes[0].set_ylabel("Wind (kt)")
    axes[0].legend(fontsize=8)
    axes[1].plot(times, values("cloud"), marker="o", label="Total")
    for key, label in (("lcc", "Low"), ("mcc", "Middle"), ("hcc", "High")):
        fraction = [direct(row["fields"], key) for row in data["rows"]]
        axes[1].plot(
            times,
            [value * 100 if value is not None else float("nan") for value in fraction],
            marker=".",
            linestyle="--",
            label=label,
        )
    axes[1].set_ylabel("Cloud cover (%)")
    axes[1].set_ylim(0, 100)
    axes[1].legend(fontsize=8, ncols=4)
    axes[2].plot(times, values("rate"), marker="o", linestyle="none")
    axes[2].set_ylabel("Instant precip\n(mm/h)")

    for key, label in (("temperature2", "Temperature"), ("dewpoint2", "Dewpoint")):
        kelvin = [direct(row["fields"], key) for row in data["rows"]]
        axes[3].plot(
            times,
            [value - 273.15 if value is not None else float("nan") for value in kelvin],
            marker=".",
            label=label,
        )
    axes[3].set_ylabel("2 m temp (°C)")
    axes[3].legend(fontsize=8)

    from datetime import timezone

    axes[3].xaxis.set_major_formatter(DateFormatter("%Y-%m-%d\n%H%MZ", tz=timezone.utc))
    axes[3].set_xlabel("UTC native valid time; gaps indicate unavailable data")
    for ax in axes:
        ax.grid(alpha=0.3)
        ax.axvline(
            datetime.fromisoformat(data["start_utc"]), color="gray", linestyle=":"
        )
        ax.axvline(datetime.fromisoformat(data["end_utc"]), color="gray", linestyle=":")
    figure.suptitle(
        f"ECMWF IFS 0.25° — original model guidance\n"
        f"{data['latitude']:.4f}, {data['longitude']:.4f} | Cycle {data['cycle_utc']}\n"
        f"Requested {data['start_utc']} to {data['end_utc']}\n"
        f"Native quality: {data.get('counts', {}).get('direct', 0)} direct / {data.get('counts', {}).get('missing', 0)} missing / "
        f"{data.get('counts', {}).get('unsupported', 0)} unsupported / {data.get('counts', {}).get('invalid', 0)} invalid; visibility and ceiling unavailable from this feed",
        fontsize=9,
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=100)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

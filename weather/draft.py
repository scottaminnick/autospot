"""Minimal honest draft and raw guidance while aviation diagnoses are rebuilt."""

import base64
from datetime import datetime
import io
import math


def direct(fields, name):
    field = fields.get(name, {})
    return field.get("value") if field.get("status") == "direct" else None


def wind(fields):
    u, v = direct(fields, "u10"), direct(fields, "v10")
    if u is None or v is None:
        return None
    speed = round(math.hypot(u, v) * 3600 / 1852)
    direction = (round((math.degrees(math.atan2(-u, -v)) % 360) / 10) * 10) % 360 or 360
    return "00000KT" if speed == 0 else f"{direction:03d}{speed:02d}KT"


def build_draft(data, label):
    start, end = (datetime.fromisoformat(data[k]) for k in ["start_utc", "end_utc"])
    lines = [
        "DRAFT — FORECASTER COMPLETION REQUIRED",
        f"Operation: {label}",
        f"Location: {data['latitude']:.4f}, {data['longitude']:.4f}",
        f"Valid {start:%Y-%m-%d %HZ} to {end:%Y-%m-%d %HZ}",
        f"Guidance: ECMWF IFS 0.25°, cycle {data['cycle_utc']}",
        "Complete visibility, weather and sky from reviewed guidance.",
        "Domain requires forecaster verification. Wind interpolation/FL output is not applied.",
        "",
    ]
    by_time = {datetime.fromisoformat(r["valid_utc"]): r for r in data["rows"]}
    times = sorted({start, *[t for t in by_time if start < t < end]})
    for valid in times:
        value = wind(by_time[valid]["fields"]) if valid in by_time else None
        lines.append(
            f"FM{valid:%d%H%M} {value or '[WIND REVIEW REQUIRED]'} "
            "[VISIBILITY REQUIRED] [WEATHER REVIEW REQUIRED] [SKY REQUIRED]"
        )
    lines += [
        "",
        "Native model times are shown; gaps require manual completion.",
        "Gust maxima retain source intervals in the guidance table; no automatic gust group is inferred.",
    ]
    return "\n".join(lines)


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

    figure = Figure(figsize=(10, 7), layout="constrained")
    axes = figure.subplots(3, 1, sharex=True)
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
    axes[1].plot(times, values("cloud"), marker="o")
    axes[1].set_ylabel("Total cloud (%)")
    axes[1].set_ylim(0, 100)
    axes[2].plot(times, values("rate"), marker="o")
    axes[2].set_ylabel("Instant precip\n(mm/h)")
    axes[2].xaxis.set_major_formatter(DateFormatter("%d/%H%MZ"))
    axes[2].set_xlabel("UTC native valid time; gaps indicate unavailable data")
    for ax in axes:
        ax.grid(alpha=0.3)
        ax.axvline(
            datetime.fromisoformat(data["start_utc"]), color="gray", linestyle=":"
        )
        ax.axvline(datetime.fromisoformat(data["end_utc"]), color="gray", linestyle=":")
    figure.suptitle(
        f"ECMWF IFS 0.25° — original model guidance\n"
        f"{data['latitude']:.4f}, {data['longitude']:.4f} | Cycle {data['cycle_utc']}\n"
        f"Requested {data['start_utc']} to {data['end_utc']}",
        fontsize=9,
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=100)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

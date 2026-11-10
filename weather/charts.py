"""Exact-time reviewed forecast display, separate from original model guidance."""

import base64
import io
from datetime import timezone
import math

from weather.coordinates import utc

CEILING_TOP = 12000


def ceiling_series(forecast):
    result = []
    for row in forecast["rows"]:
        sky = row.get("sky")
        result.append(
            {
                "valid_utc": row["valid_utc"],
                "state": sky["ceiling_state"] if sky else "unknown",
                "height": sky.get("ceiling_ft_agl") if sky else None,
            }
        )
    return result


def reviewed_chart(data, forecast, identifier, version, finalized=False, basis=None):
    from matplotlib.figure import Figure
    from matplotlib.dates import DateFormatter

    rows = ceiling_series(forecast)
    times = [utc(r["valid_utc"]) for r in rows]
    heights = [
        min(r["height"], CEILING_TOP)
        if r["state"] == "ceiling" and r["height"] is not None
        else math.nan
        for r in rows
    ]
    states = [{"unknown": 0, "no_ceiling": 1, "ceiling": 2}[r["state"]] for r in rows]
    figure = Figure(figsize=(10, 5.5), layout="constrained")
    ax, state = figure.subplots(
        2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    end = utc(data["end_utc"])
    if times:
        ax.step(
            [*times, end],
            [*heights, heights[-1]],
            where="post",
            label="Prevailing ceiling",
        )
        state.step([*times, end], [*states, states[-1]], where="post", color="#426880")
        ax.scatter(times, heights, s=20)
    high = False
    for time, row in zip(times, rows):
        if row["height"] is not None and row["height"] > CEILING_TOP:
            ax.plot(
                time,
                CEILING_TOP,
                marker="^",
                color="#b34d1d",
                label="Above plotted range" if not high else None,
            )
            ax.annotate(
                f"{row['height']:,} ft",
                (time, CEILING_TOP),
                xytext=(4, -14),
                textcoords="offset points",
                fontsize=7,
            )
            high = True
    conditional = False
    for group in forecast.get("groups", []):
        sky = group.get("sky")
        if sky and sky["ceiling_state"] == "ceiling":
            height = min(sky["ceiling_ft_agl"], CEILING_TOP)
            ax.plot(
                [utc(group["start"]), utc(group["end"])],
                [height, height],
                linestyle="--",
                color="#a74649",
                label="Conditional ceiling (see text)" if not conditional else None,
            )
            conditional = True
    ax.set_ylim(0, CEILING_TOP + 700)
    ax.set_ylabel("Ceiling (ft AGL)")
    ax.legend(fontsize=8, loc="best")
    state.set_yticks([0, 1, 2], ["Unknown", "No ceiling", "Ceiling"])
    state.set_ylim(-0.35, 2.35)
    state.set_ylabel("Prevailing\nstate")
    state.xaxis.set_major_formatter(DateFormatter("%Y-%m-%d\n%H%MZ", tz=timezone.utc))
    state.set_xlabel("Full UTC valid time; FEW/SCT do not define a ceiling")
    for axis in [ax, state]:
        axis.grid(alpha=0.3)
        axis.set_xlim(utc(data["start_utc"]), end)
    figure.suptitle(
        f"{basis or ('Approved text forecast' if finalized else 'Structured forecaster draft')} — {identifier} v{version}\n"
        f"{data['latitude']:.4f}, {data['longitude']:.4f} | {data.get('model', 'ECMWF_IFS')} {data['grid']} cycle {data['cycle_utc']}\n"
        f"Valid {data['start_utc']} to {data['end_utc']} | Manual/derived forecast elements; original model chart is separate",
        fontsize=8,
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=100)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

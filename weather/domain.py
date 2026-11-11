"""Versioned aviation reference geometry. Classification is not service authority."""

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).parent / "data/domain"
# Deployment-owned snapshots, independently pinned; never a live network dependency.
HASHES = {
    "awc_sigmet_area.json": "71290473819441f0e84732603f309201860707fa095e1393a857a60e60f4c9f6",
}
HASHES["aawu-firs.geojson"] = (
    "3942cf25ef8d2bde37a81f5a9552bb357350a733ace9bce88034c0193cad0167"
)


def unwrap(ring):
    if len(ring) < 4 or ring[0] != ring[-1]:
        raise ValueError("Unclosed ring")
    result = [ring[0]]
    for x, y in ring[1:]:
        if not all(math.isfinite(v) for v in (x, y)) or not -90 <= y <= 90:
            raise ValueError("Invalid coordinates")
        x += 360 * round((result[-1][0] - x) / 360)
        result.append([x, y])
    # The pinned source uses non-winding, continuous-longitude rings. Refuse
    # unsupported global windings instead of silently drawing the wrong polygon.
    if (
        result[0] != result[-1]
        or max(x for x, _ in result) - min(x for x, _ in result) >= 180
    ):
        raise ValueError("Unsupported ring topology")
    return result


def ring_relation(ring, latitude, longitude):
    ring = unwrap(ring)
    if abs(latitude) == 90:
        return "boundary" if any(y == latitude for _, y in ring) else "outside"
    center = (min(x for x, _ in ring) + max(x for x, _ in ring)) / 2
    x = longitude + 360 * round((center - longitude) / 360)
    inside = False
    for (a, b), (c, d) in zip(ring, ring[1:]):
        distance = math.hypot(c - a, d - b)
        cross = (x - a) * (d - b) - (latitude - b) * (c - a)
        if (
            abs(cross) <= 1e-9 * max(1, distance)
            and min(a, c) - 1e-9 <= x <= max(a, c) + 1e-9
            and min(b, d) - 1e-9 <= latitude <= max(b, d) + 1e-9
        ):
            return "boundary"
        if (b > latitude) != (d > latitude) and x < a + (c - a) * (latitude - b) / (
            d - b
        ):
            inside = not inside
    return "inside" if inside else "outside"


def regions(features, latitude, longitude):
    matches = []
    for feature in features:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            polygons = [polygons]
        elif geometry["type"] != "MultiPolygon":
            raise ValueError("Unsupported geometry")
        for polygon in polygons:
            outside = ring_relation(polygon[0], latitude, longitude)
            holes = [ring_relation(h, latitude, longitude) for h in polygon[1:]]
            if outside == "outside" or "inside" in holes:
                continue
            relation = (
                "boundary" if outside == "boundary" or "boundary" in holes else "inside"
            )
            matches.append(
                {"name": feature["properties"].get("name", "AWC"), "relation": relation}
            )
    return matches


@lru_cache(maxsize=1)
def load_geometry():
    datasets = {}
    for name, digest in HASHES.items():
        payload = (ROOT / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Geometry snapshot hash mismatch")
        features = json.loads(payload)["features"]
        regions(features, 0, 0)  # Validate every ring, even for an outside query.
        datasets[name] = features
    return datasets


def classify(latitude, longitude, disposition="pending"):
    result = {
        "classification": "unknown_review",
        "automatic_service_assignment": False,
        "disposition": disposition,
        "awc": [],
        "aawu": [],
        "snapshot": "2026-11-10; retrieval date, not an operational effective date",
        "source_hashes": HASHES,
        "sources": [
            "https://aviationweather.gov/gis/awc_sigmet_area.json",
            "https://aviationweather.gov/gis/firpoly.topojson",
        ],
    }
    try:
        if (
            not all(math.isfinite(v) for v in (latitude, longitude))
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError("Invalid point")
        geometry = load_geometry()
        awc = regions(geometry["awc_sigmet_area.json"], latitude, longitude)
        aawu = regions(geometry["aawu-firs.geojson"], latitude, longitude)
        status = (
            "boundary_review"
            if any(m["relation"] == "boundary" for m in awc + aawu)
            else "conflict_review"
            if awc and aawu
            else "AWC"
            if awc
            else "AAWU"
            if aawu
            else "other_office_review"
        )
        result.update(classification=status, awc=awc, aawu=aawu)
    except (OSError, ValueError, KeyError, TypeError):
        status = "unknown_review"
    result["message"] = {
        "AWC": "Inside the candidate AWC SIGMET geometry. Verify spot-service authority and record coordination.",
        "AAWU": "Inside an Anchorage FIR. Follow the local AAWU redirect/coordination procedure before use.",
        "other_office_review": "Outside both candidate reference regions. Verify the responsible office or coordinated AWC service rule.",
        "boundary_review": "On a reference boundary. Resolve office responsibility through coordination.",
        "conflict_review": "Reference regions overlap. Resolve office responsibility through coordination.",
        "unknown_review": "Domain reference unavailable or invalid. Verify office responsibility manually.",
    }[status]
    return result


def location_map(latitude, longitude):
    """Small reference map, continuous longitude around point; no base-map service."""
    import base64
    import io
    from matplotlib.figure import Figure

    figure = Figure(figsize=(8, 3.5), layout="constrained")
    ax = figure.subplots()
    try:
        geometry = load_geometry()
        for name, color, label in [
            ("awc_sigmet_area.json", "#4b88a2", "AWC candidate SIGMET area"),
            ("aawu-firs.geojson", "#d4933e", "Anchorage FIRs"),
        ]:
            first = True
            for feature in geometry[name]:
                for polygon in feature["geometry"]["coordinates"]:
                    ring = unwrap(polygon[0])
                    center = (min(x for x, _ in ring) + max(x for x, _ in ring)) / 2
                    shift = 360 * round((longitude - center) / 360)
                    for offset in (-360, 0, 360):
                        ax.fill(
                            [x + shift + offset for x, _ in ring],
                            [y for _, y in ring],
                            alpha=0.25,
                            color=color,
                            edgecolor=color,
                            label=label if first else None,
                        )
                        first = False
    except (OSError, ValueError, KeyError, TypeError):
        ax.text(
            0.5,
            0.9,
            "Reference geometry unavailable",
            transform=ax.transAxes,
            ha="center",
        )
    ax.plot(longitude, latitude, "r*", markersize=12, label="Requested point")
    ax.set_xlim(longitude - 65, longitude + 65)
    ax.set_ylim(max(-90, latitude - 30), min(90, latitude + 30))
    ax.set_xlabel("Continuous longitude (°; wrapped at dateline)")
    ax.set_ylabel("Latitude (°)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, loc="lower left")
    ax.set_title(
        f"Candidate aviation reference geometry • snapshot 2026-11-10\nPoint {latitude:.4f}, {longitude:.4f} • verify spot-service authority",
        fontsize=9,
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=100)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

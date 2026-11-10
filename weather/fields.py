"""Named ECMWF fields and strict index/GRIB decoding contracts."""

from dataclasses import asdict, dataclass
from datetime import timedelta
from enum import StrEnum
import hashlib
import json
import math
import threading


class State(StrEnum):
    DIRECT = "direct"
    DERIVED = "derived"
    MANUAL = "manual"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


class DataFault(Exception):
    def __init__(self, code, state=State.INVALID, details=None):
        super().__init__(code)
        self.code, self.state = code, state
        self.details = details or {}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    param: str
    param_id: int
    level_type: str
    level: int
    source_unit: str
    unit: str
    processing: str = "instant"
    pressure: bool = False
    minimum: float | None = None
    maximum: float | None = None


SPECS = [
    FieldSpec("u10", "10u", 165, "heightAboveGround", 10, "m s**-1", "m/s"),
    FieldSpec("v10", "10v", 166, "heightAboveGround", 10, "m s**-1", "m/s"),
    FieldSpec(
        "gust10",
        "10fg",
        49,
        "heightAboveGround",
        10,
        "m s**-1",
        "m/s",
        "max",
        minimum=0,
    ),
    FieldSpec(
        "tcc",
        "tcc",
        164,
        "entireAtmosphere",
        0,
        "(0 - 1)",
        "fraction",
        minimum=0,
        maximum=1,
    ),
    *[
        FieldSpec(p, p, i, "surface", 0, "(0 - 1)", "fraction", minimum=0, maximum=1)
        for p, i in [("lcc", 186), ("mcc", 187), ("hcc", 188)]
    ],
    FieldSpec(
        "precip_rate",
        "tprate",
        260048,
        "surface",
        0,
        "kg m**-2 s**-1",
        "kg/m2/s",
        minimum=0,
    ),
    FieldSpec(
        "precip_type",
        "ptype",
        260015,
        "surface",
        0,
        "(Code table 4.201)",
        "code_4.201",
        minimum=0,
    ),
    FieldSpec("precip_total", "tp", 228, "surface", 0, "m", "m", "accum", minimum=0),
    FieldSpec(
        "precip_convective", "cp", 143, "surface", 0, "m", "m", "accum", minimum=0
    ),
    FieldSpec(
        "cape", "mucape", 228235, "mostUnstableParcel", 0, "J kg**-1", "J/kg", minimum=0
    ),
    FieldSpec("temperature2", "2t", 167, "heightAboveGround", 2, "K", "K", minimum=0),
    FieldSpec("dewpoint2", "2d", 168, "heightAboveGround", 2, "K", "K", minimum=0),
    FieldSpec("skin_temperature", "skt", 235, "surface", 0, "K", "K", minimum=0),
    FieldSpec("surface_pressure", "sp", 134, "surface", 0, "Pa", "Pa", minimum=0),
]
OROGRAPHY = FieldSpec(
    "surface_geopotential", "z", 129, "surface", 0, "m**2 s**-2", "m2/s2"
)
CORE = {"u10", "v10", "tcc", "surface_pressure"}
PRESSURE_LEVELS = {1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50, 10}
_DECODE_LOCK = threading.Lock()


def pressure_spec(param, level):
    if level not in PRESSURE_LEVELS or param not in {"u", "v", "t", "gh"}:
        raise ValueError("Unsupported pressure field")
    ids = {"u": 131, "v": 132, "t": 130, "gh": 156}
    units = {"u": "m s**-1", "v": "m s**-1", "t": "K", "gh": "gpm"}
    return FieldSpec(
        f"{param}_{level}",
        param,
        ids[param],
        "isobaricInhPa",
        level,
        units[param],
        {"u": "m/s", "v": "m/s", "t": "K", "gh": "gpm"}[param],
        pressure=True,
    )


@dataclass
class FieldValue:
    name: str
    status: State
    value: float | None = None
    unit: str | None = None
    reason: str | None = None
    provenance: dict | None = None
    attempts: list | None = None

    def as_dict(self):
        return asdict(self)


def parse_index(body, run, step, size):
    if not body or len(body) > 2 * 1024 * 1024:
        raise DataFault("index_size")
    try:
        rows = [
            json.loads(line)
            for line in body.decode("utf-8").splitlines()
            if line.strip()
        ]
        if not 1 <= len(rows) <= 3000:
            raise ValueError()
        previous_end = -1
        for row in rows:
            offset, length = row["_offset"], row["_length"]
            if type(offset) is not int or type(length) is not int:
                raise ValueError()
            if (
                offset < 0
                or length < 20
                or length > 8 * 1024 * 1024
                or offset < previous_end
            ):
                raise ValueError()
            if offset + length > size:
                raise ValueError()
            previous_end = offset + length
            if (
                str(row["date"]) != run.strftime("%Y%m%d")
                or int(row["time"]) != run.hour * 100
                or row["stream"] != "oper"
                or row["type"] != "fc"
                or row["class"] != "od"
                or row.get("domain") != "g"
                or str(row.get("expver")) != "0001"
                or int(row["step"]) < 0
                or not isinstance(row["param"], str)
                or row["levtype"] not in {"sfc", "pl", "sol"}
            ):
                raise ValueError()
        # Some +0 files contain +3 temperature placeholders. Select record times
        # below, rather than treating every record as valid at the filename step.
        return rows
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DataFault("malformed_index") from None


def locate(rows, spec, step):
    matches = [
        r
        for r in rows
        if r["param"] == spec.param
        and int(r["step"]) == step
        and r["levtype"] == ("pl" if spec.pressure else "sfc")
        and (not spec.pressure or int(r.get("levelist", -1)) == spec.level)
    ]
    if len(matches) > 1:
        raise DataFault("ambiguous_index_field")
    return matches[0] if matches else None


def decode_message(body, spec, run, step, latitude, longitude):
    import eccodes as ec

    if (
        len(body) < 20
        or body[:4] != b"GRIB"
        or body[7] != 2
        or body[-4:] != b"7777"
        or int.from_bytes(body[8:16], "big") != len(body)
    ):
        raise DataFault("grib_framing")
    # ecCodes global/native state is kept serial within a request process.
    with _DECODE_LOCK:
        gid = None
        try:
            gid = ec.codes_new_from_message(body)
            keys = [
                "paramId",
                "units",
                "typeOfLevel",
                "level",
                "dataDate",
                "dataTime",
                "validityDate",
                "validityTime",
                "stepType",
                "startStep",
                "endStep",
                "stepUnits",
                "gridType",
                "Ni",
                "Nj",
                "iDirectionIncrementInDegrees",
                "jDirectionIncrementInDegrees",
            ]
            keys += [
                "centre",
                "marsClass",
                "marsStream",
                "dataType",
                "experimentVersionNumber",
                "typeOfGeneratingProcess",
                "productionStatusOfProcessedData",
                "generatingProcessIdentifier",
                "modelName",
            ]
            grid_bounds = {
                "latitudeOfFirstGridPointInDegrees": 90.0,
                "latitudeOfLastGridPointInDegrees": -90.0,
                "longitudeOfFirstGridPointInDegrees": 180.0,
                "longitudeOfLastGridPointInDegrees": 179.75,
                "iScansNegatively": 0,
                "jScansPositively": 0,
                "jPointsAreConsecutive": 0,
                "alternativeRowScanning": 0,
            }
            keys += list(grid_bounds)
            meta = {k: ec.codes_get(gid, k) for k in keys}
            expected = {
                "paramId": spec.param_id,
                "units": spec.source_unit,
                "typeOfLevel": spec.level_type,
                "level": spec.level,
                "dataDate": int(run.strftime("%Y%m%d")),
                "dataTime": run.hour * 100,
                "stepType": spec.processing,
                "endStep": step,
                "stepUnits": 1,
                "gridType": "regular_ll",
                "Ni": 1440,
                "Nj": 721,
                "iDirectionIncrementInDegrees": 0.25,
                "jDirectionIncrementInDegrees": 0.25,
            }
            expected.update(
                centre="ecmf",
                marsClass="od",
                marsStream="oper",
                dataType="fc",
                experimentVersionNumber="0001",
                typeOfGeneratingProcess=2,
                productionStatusOfProcessedData=0,
                modelName="IFS",
            )
            expected.update(grid_bounds)
            for key, value in expected.items():
                if meta[key] != value:
                    raise DataFault(f"wrong_{key}")
            start, end = meta["startStep"], meta["endStep"]
            if (
                not 0 <= start <= end
                or (spec.processing == "instant" and start != end)
                or (spec.processing == "max" and start >= end)
                or (spec.processing == "accum" and start != 0)
            ):
                raise DataFault("wrong_processing_interval")
            valid = run + timedelta(hours=step)
            if (
                meta["validityDate"] != int(valid.strftime("%Y%m%d"))
                or meta["validityTime"] != valid.hour * 100 + valid.minute
            ):
                raise DataFault("wrong_valid_time")
            point = dict(
                ec.codes_grib_find_nearest(gid, latitude, longitude, npoints=1)[0]
            )
            value = float(point["value"])
            if ec.codes_get(gid, "bitmapPresent") and value == ec.codes_get(
                gid, "missingValue"
            ):
                raise DataFault("grib_missing_value", State.MISSING)
            if not math.isfinite(value):
                raise DataFault("nonfinite_value")
            if (
                not all(
                    math.isfinite(float(point[k])) for k in ["lat", "lon", "distance"]
                )
                or not -90 <= point["lat"] <= 90
                or not -180 <= point["lon"] <= 360
                or not 0 <= point["distance"] <= 30
            ):
                raise DataFault("invalid_sample_location")
            if (spec.minimum is not None and value < spec.minimum) or (
                spec.maximum is not None and value > spec.maximum
            ):
                raise DataFault("value_out_of_range")
            if spec.param == "ptype":
                if value == 255:
                    raise DataFault("grib_missing_value", State.MISSING)
                if value != int(value):
                    raise DataFault("noninteger_category")
            return FieldValue(
                spec.name,
                State.DIRECT,
                value,
                spec.unit,
                provenance={
                    "model": "ECMWF_IFS",
                    "grid": "0p25",
                    "cycle_utc": run.isoformat(),
                    "valid_utc": valid.isoformat(),
                    "interval_start_utc": (run + timedelta(hours=start)).isoformat(),
                    "interval_end_utc": valid.isoformat(),
                    "metadata": meta,
                    "requested_latitude": latitude,
                    "requested_longitude": longitude,
                    "sample_latitude": point["lat"],
                    "sample_longitude": point["lon"],
                    "distance_km": point["distance"],
                    "message_sha256": hashlib.sha256(body).hexdigest(),
                    "method": "nearest_grid_point",
                    "decoder_version": ec.codes_get_api_version(),
                },
            )
        except DataFault:
            raise
        except (ec.CodesInternalError, ValueError, TypeError, KeyError, OverflowError):
            raise DataFault("decode_failed") from None
        finally:
            if gid is not None:
                ec.codes_release(gid)

# ECMWF capability and evidence matrix

Checked 2026-11-10; IFS `0p25/oper/fc`, original 2026-11-09 00Z cycle.

## Live decoded fields

Values below are model samples at the nearest grid point **24.75 N, 77.75 W**,
approximately 4.56 km from the requested 24.71 N, 77.76 W. They are not observations.
Units and processing intervals are decoded from GRIB, not guessed from filename cadence.

| Index parameter | GRIB ID | Units | Processing, forecast hours | Sample value |
|---|---:|---|---|---:|
| `sp` | 134 | `Pa` | instant, 12–12 | 101658.605 |
| `lcc` | 186 | `(0 - 1)` | instant, 12–12 | 0.28125 |
| `mcc` | 187 | `(0 - 1)` | instant, 12–12 | 0 |
| `hcc` | 188 | `(0 - 1)` | instant, 12–12 | 0.546875 |
| `10fg` | 49 | `m s**-1` | max, 11–12 | 5.71651518 |
| `skt` | 235 | `K` | instant, 12–12 | 301.867233 |
| `ptype` | 260015 | `(Code table 4.201)` | instant, 12–12 | 0 |
| `tp` | 228 | `m` | accum, 0–12 | 0 |
| `tprate` | 260048 | `kg m**-2 s**-1` | instant, 12–12 | 0 |
| `tcc` | 164 | `(0 - 1)` | instant, 12–12 | 0.671875 |
| `10u` | 165 | `m s**-1` | instant, 12–12 | -2.45025635 |
| `mucape` | 228235 | `J kg**-1` | instant, 12–12 | 240 |
| `10v` | 166 | `m s**-1` | instant, 12–12 | -2.77612305 |
| `cp` | 143 | `m` | accum, 0–12 | 0 |
| `2t` | 167 | `K` | instant, 12–12 | 300.354874 |
| `2d` | 168 | `K` | instant, 12–12 | 293.822464 |

## Pressure-level support

| Parameter | ID | Units | Decoded levels in this capture |
|---|---:|---|---|
| U/V | 131 / 132 | m s⁻¹ | 925, 850 hPa |
| Temperature | 130 | K | 925, 850 hPa |
| Geopotential height | 156 | gpm | 925, 850 hPa |

The captured indexes enumerate 1000, 925, 850, 700, 600, 500, 400, 300, 250,
200, 150, 100, 50 and 10 hPa. Enumeration is not a decode validation of every
parameter at every level. Surface `z` at step zero is separately sampled; it is
model geopotential, not a precise site elevation. Preserve `gpm` as geopotential
height; do not silently describe it as geometric MSL height.

## Interpretation decisions

- Wind components are available for every three-hour step +12…+36 in the original
  request. Preserve all nine absolute valid times; +36 is the ending boundary.
- Gust at +12 has `stepType=max`, `startStep=11`, `endStep=12`: a preceding
  **one-hour** maximum. Do not assume its support interval equals the 3-hour file spacing.
- Cloud fractions are independent diagnostic layers. Total 0.671875 differs from
  low + middle + high (0.828125). Summing layers or assigning an arbitrary base is invalid.
- `tp` and `cp` are 0–12 hour accumulations in metres; `tprate` is instantaneous
  kg m⁻² s⁻¹. Convert water-equivalent rate to mm/h by ×3600. Any accumulated
  convective amount does not prove a thunderstorm at the final valid time.
- `ptype` is a categorical Code Table 4.201 value. This sample is zero (no
  precipitation), not a winter-weather test. Freeze a nonzero/winter case in PR 4.
- `vis`, `cbh` and a direct ceiling field are absent from the captured surface
  inventory. Mark these **unsupported by this feed**, with manual completion.
  This is not a claim that ECMWF cannot produce them in other authorized products.
- CAPE is context only. No automatic TS, fog, probability, or layer-base diagnosis
  is validated by Phase 0. No replacement diagnosis is approved here.

## Precipitation-type mapping boundary

The ECMWF-hosted GRIB2 Code Table 4.201 distinguishes rain (1), freezing rain (3),
snow (5), wet snow (6), rain/snow mixture (7), ice pellets (8), drizzle (11),
freezing drizzle (12), and missing (255). Preserve all codes, including unfamiliar
ones, without interpolating them. The table also includes thunderstorm (2), but
a table entry does not establish that this IFS feed diagnoses thunderstorms.
PR 4 must validate model-specific code usage and appropriate weather/intensity
mapping with nonzero fixtures. Until then, type is displayed as guidance.

## Provider/cycle evidence and limits

See `inventory-observations.json` for every attempt, exact URL, status, hash and
timestamp. The recovered capture contains inventories for the original 00Z
interval and +12 from all four cycles. AWS +12 inventory also succeeded on this
check. A separate mirror comparison uses AWS’s own index and compares decoded
10 m U plus the full message hash against the ECMWF-hosted copy.

Transient proxy failures in the research environment are retained as attempt
evidence and are not attributed to an ECMWF outage. A successful later retry
does not establish sustained provider reliability or fix the deployed app.
Only ECMWF-hosted and AWS paths were sampled; Azure/GCP remain documented,
untested candidates. The rebuilt pipeline should prefer the verified working
ECMWF-hosted path initially, with bounded AWS fallback, then tune from measured
availability. No fallback to GFS is implicit.

Published cadence: 00/12Z supply 0–144 by 3 h and 150–360 by 6 h; 06/18Z
supply 0–144 by 3 h. Live probes are recorded separately and do not establish
all times/fields as continuously available. Initial interpolation is restricted
to three-hour brackets; longer native spacing must be shown as such or rejected
for elements needing interpolation.

A step-zero file contains two temperature-extreme index records labeled step 3
alongside step-zero records. The adapter must select on record metadata and
verify decoded valid time; the filename does not establish every message’s time.

## Sources and attribution

- ECMWF Open Data overview: https://www.ecmwf.int/en/forecasts/datasets/open-data
- ECMWF gust definition: https://codes.ecmwf.int/grib/param-db/49
- ECMWF precipitation type: https://codes.ecmwf.int/grib/param-db/260015
- Code table: https://codes.ecmwf.int/grib/format/grib2/ctables/4/201/
- Exact data URLs and record byte ranges are in the frozen fixture metadata.

ECMWF IFS data © ECMWF, redistributed under CC BY 4.0:
https://creativecommons.org/licenses/by/4.0/ . Source terms:
https://apps.ecmwf.int/datasets/licences/general/ . Raw `.grib2` messages and
indexes are unmodified slices/responses; point JSON is an extracted derivative.
ECMWF does not endorse this application or the derived spot forecasts.

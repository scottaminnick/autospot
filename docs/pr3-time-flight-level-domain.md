# PR 3 — exact UTC boundaries, flight-level winds and aviation reference geometry

Based on merged PR 2 (`aff941e`). The surface-only request remains the default;
optional comma-separated flight levels now request actual pressure-level U/V.

## Time and missing samples

Native rows keep their decoded, verified absolute UTC valid times. Separate
`window_rows` include the exact requested start, interior native times and end
support. The interval remains `[start, end)`: end support cannot create an FM or
an aloft forecast row. Full dates appear in the table, aloft text and plot axis.
Missing expected first/interior/last slots remain gaps in their original positions.
No extrapolation or shifting of the first surviving sample is permitted.

Only instantaneous U/V, 2 m temperature/dewpoint and surface pressure can be
interpolated in time. Interpolation requires adjacent native slots no more than
three hours apart, finite valid values, identical model/cycle/grid/sample point,
and matching source valid times. Native missing slots are never filled. Wind
components are interpolated before direction/speed calculation. Source times,
weights, method and original provenance remain inspectable; the draft and review
page identify interpolated boundaries. Providers may differ within the same run.

Gust maxima, cloud fractions, precipitation, categories, CAPE and probabilities
are not interpolated. The native table retains their source values and gust
support intervals. Cloud/precipitation chart points are not joined as interpolated
values. Initial visibility, weather and sky still require forecaster completion.
The allowed three-hour limit also means a six-hour source gap remains unavailable.

## Flight levels, pressure coordinates and model surface

`FL030` means 3,000 feet of standard **pressure altitude**, not 925 hPa, actual
altitude MSL or height AGL. There is no actual-MSL mode in this release.

Pressure uses the US Standard Atmosphere 1976 hydrostatic equations with
geopotential pressure altitude (FL × 100 × 0.3048 m), sea-level pressure 101325 Pa,
temperature 288.15 K, gravity 9.80665 m/s², specific gas constant
8314.32/28.9644 J/(kg K), and tropospheric lapse rate 0.0065 K/m. Above 11,000 m,
the implementation uses the isothermal 216.65 K layer; it does not extend the
lapse-rate formula through FL450. The supported FL010–FL450 range is below the
next atmospheric layer at 20 km.

The requested pressure is bracketed in ECMWF's available pressure levels. U/V
are interpolated linearly in log pressure, then converted to true direction and
knots. Results record target pressure, enclosing levels, weight, coordinate method
and all source provenance. FL030 is approximately **908.12 hPa**, with **850/925**
hPa brackets. The Phase 0 acceptance table's 1000/925 example was corrected.
FL450 is approximately 147.48 hPa, bracketed by 100/150 hPa.

Target pressure and both model brackets must be at or above the model surface.
Missing/nonpositive surface pressure, an underground target, an underground
bracket, missing profile values, wrong units, mismatched source points/cycles,
and wrong valid times produce explicit unavailable reasons. For off-hour FL
output, **both source times** must also pass the profile-level surface check;
interpolating surface pressure cannot make an underground source valid.

Model orography is derived only from verified surface geopotential divided by
standard gravity. Negative elevations stay negative; missing elevation stays
unknown. Its unit is geopotential metres and it is identified as model orography,
not station elevation or a basis for converting sky heights or FLs to AGL/MSL.

Flight levels are optional, distinct integer FL010–450 values, maximum 12.
Only their necessary pressure brackets are downloaded. Run selection checks
requested profile inventory throughout the period alongside required surface
fields. Missing optional profiles remain visible and never cause a silent model
switch. Four concurrent downloads, the existing deadlines/cache and 700-field
budget remain in force. Requests with too many levels for their duration are
rejected before source requests, with a shorter-period/fewer-levels message.

## Geometry and coordination

The runtime includes hash-pinned copies of the Phase 0 AWC SIGMET-area geometry
and all three named Anchorage FIRs, with source metadata. These deployment assets
are separate from test fixtures. A missing or corrupt snapshot produces an
unknown-review result; no boundary network download is needed for each forecast.

The source's continuous longitudes are preserved. Ring queries shift the point
into the same longitude branch instead of wrapping vertices independently. Polygon
holes, edges, vertices, ±180° equivalence, overlapping regions and the Arctic
pole are handled explicitly. At a shared pole, the result is boundary review,
independent of the supplied longitude. Unsupported winding topology is rejected
rather than guessed. Segments follow the frozen longitude/latitude display
geometry; this is not a replacement spherical FIR reconstruction.

The map identifies the requested point against the candidate aviation geometry.
It is a longitude/latitude reference map with no external base-map dependency.
Classification reports AWC, AAWU, outside, boundary, conflict or unknown review.
Source URLs, retrieval date and hashes are retained. The snapshot date is not an
assertion of the operational effective date or current boundary freshness.

**Operational gate remains open:** Scott has not established that the SIGMET
polygon is the spot-service boundary. The original Bahamas point is outside it,
and the local Aleutian example is inside AWC geometry rather than Anchorage FIRs.
Automatic blocking/assignment remains disabled. The request records a forecaster
selection (pending, AWC coordinated, AAWU redirect or other-office coordination),
and the result/draft show it alongside the independent geometry classification.
A selection is not proof that coordination occurred. AAWU locations show the local
redirect/coordination instruction. No external message is sent. Pending disposition
remains an incomplete draft; enforced finalization/versioning belongs to PR 5.

## Verification

- Offline coverage includes UTC normalization/year rollover; missing first,
  middle and last slots; duplicate times; component winds across north; no
  category/interval interpolation; no extrapolation; mismatched sources; source
  terrain masks; log-pressure winds; FL450's isothermal layer; unknown/negative
  elevation; input/work bounds; all 12 frozen geometry locations; dateline,
  holes, boundary/conflict, polar and missing/corrupt-snapshot cases.
- Independent rounded numeric references were visually checked against NOAA's
  *US Standard Atmosphere 1976*, Table IV **geopotential** English altitudes,
  printed pages 120, 124, 126, 128 and 130. Source URL/hash and seven reference
  pressures are retained in `tests/fixtures/pr3/standard-atmosphere.json`.
  The tolerance follows the source's 0.01 hPa tabulation, not display rounding.
- Two newly retained real pressure GRIB messages (U/925 and V/100 hPa) re-decode
  in CI with matching values/metadata/hashes and reject incorrect pressure levels.
  These supplement the existing seven surface messages and synthetic algorithms.
- [Live replay evidence](evidence/pr3-live-check.json): Bahamas, November 9
  13–15Z, off-hour start, FL030/450, two native support times. All 48 downloaded
  samples validated, no missing/invalid values; all seven retained surface
  reference hashes matched. Cold 22.105 s, cached 0.079 s. These are local
  measurements, not Railway guarantees or verification of forecast skill.

The edited UTF-8 download remains a **draft**. Weather diagnoses/formatting remain
PR 4; finalization, provenance packages and archive integration remain PR 5.
No runtime dependency, Railway setting or production deployment is changed.

## Source basis

- [US Standard Atmosphere 1976 (NOAA)](https://www.ngdc.noaa.gov/stp/space-weather/online-publications/miscellaneous/us-standard-atmosphere-1976/us-standard-atmosphere_st76-1562_noaa.pdf), equations and Table IV.
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data), current 0.25° pressure levels, parameters and cadence, checked November 10, 2026.
- [Phase 0 domain source and operational gate](phase0/domain-source.md), including the supplied local spot procedure.
- [Local HTTPS web-flow evidence](evidence/pr3-web-check.json): real spawned
  acquisition for a current future off-hour request, FL030/450, exact validity,
  domain disposition, generated chart/map PNGs and byte-identical edited Unicode
  download passed. The PNGs were visually inspected. Full Chromium browser
  automation could not launch because this environment denied its local socket;
  HTTP, template and PNG checks were used. This is not a Railway staging result.

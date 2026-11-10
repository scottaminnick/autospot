# Autospot product contract — Phase 0

Version 0.1 • 2026-11-10 • Baseline `30cae1e205ac67844b2dbf2a1cb1715ef148212f`

## Purpose and authority

Generate an editable first guess for a **point aviation spot forecast** within
AWC's operational domain. ECMWF IFS is the default. A forecaster combines the
guidance with satellite and other evidence, completes missing elements, and
finalizes the text for the existing coordination and NAOC archive workflow.

The supplied *AWC Spot Weather Forecast Coordination/Collaboration 10-001-003*
governs the local product: TAF-format spot text, optional flight-level winds,
requested validity, and forecaster delivery/archive. Its examples include a
72-hour period. This is not an airport TAF issuance system. Do not enforce a
24/30-hour airport limit or add a fabricated station identifier/WMO header.
The specification choices below implement Scott's accepted starting positions.
Source data availability is documented separately from operational acceptance.

## Request contract

| Input | Initial contract |
|---|---|
| Latitude/longitude | Finite decimal degrees, latitude −90…90, longitude −180…180; negative west. Reject NaN/infinity and malformed values before retrieval. Preserve original coordinates; normalize only internally. |
| Location/operation label | Optional plain text, at most 120 characters; escape in HTML and sanitize download filenames. A label is not an airport identifier. |
| Model | Enumerated `ECMWF` default or explicit `GFS`. Never change models silently. |
| Valid start | ISO date plus UTC hour, minute 00 for initial UI; timezone-aware UTC internally. Invalid date is an error, never “now.” |
| Duration | Integer 1–72 hours; quick choices 12/24/36/48/72. End = start + duration. |
| Optional winds | Off unless requested. Presets FL030/060/090/120/180/240/300/340/390, selectable; at most 12 distinct integer FLs in FL010…FL450. Each level needs valid profile brackets and surface-pressure checks. These are product limits, not guaranteed data availability. |
| Optional temperature aloft | Off initially; preserve supporting model temperatures and units. No FD-style encoding implied. |
| Domain | Show mapped point, source/version, and AWC/AAWU/other/boundary/conflict/unknown status. See domain record for the release gate. |

Normal generation uses the newest fully usable cycle for the whole requested
period, discovered from a bounded set of available runs. Check coverage of all
needed times, brackets and fields, including start offset and duration. Never
clamp a past offset to zero. A historical request can be used in a clearly
labeled replay/test path when frozen data exist; otherwise explain unavailable
coverage. No request may silently shift its validity to fit the data.

## Data and time contract

- One identified model/cycle/grid for a draft. Provider failover may use the same
  ECMWF model, cycle, step and field from another verified source. Match each
  provider's own index to its own GRIB bytes. Record provider per field. No
  silent previous-cycle patching of isolated gaps or blending with GFS.
- Store request location, sampled grid location, retrieval time, model/cycle,
  parameter ID, vertical coordinate, decoded units, valid time or interval,
  provider, object/range identity, and transformation method for every field.
- Keep `direct`, `derived`, `manual`, `unsupported`, `missing`, `invalid`, and
  `not_applicable` distinguishable. Values in unresolved states are null, never
  meteorological numbers. A valid zero is distinct from a missing value.
- Preserve an absolute UTC timeline. Missing first/middle/last samples leave
  gaps. Do not relabel surviving samples or extrapolate beyond available data.
- Native three-hour output remains visible as three-hour guidance. At a
  non-native boundary, linear time interpolation is allowed only for U/V,
  temperature/dewpoint and pressure with two valid bracketing samples from the
  same run (initial bracket limit three hours). Show both times and the method.
  Interpolate components before calculating wind speed/direction.
- Do not interpolate categorical precipitation type, weather codes, probabilities,
  gust maxima, accumulated precipitation, or cloud-layer descriptions. For these,
  show native values and their support intervals; initial spot conditions require
  forecaster completion/review. Never treat nearest-time selection as an exact
  valid-time measurement.
- Preserve start/end and statistical processing from GRIB. The captured +12 gust
  has an 11–12 h maximum interval: three-hour publication cadence does **not**
  establish a three-hour gust interval. Accumulations need same-cycle differencing
  over verified endpoints; missing/reset accumulations cannot produce a rate.
- The product interval is `[start, end)`. A sample at end may support interpolation
  or interval calculations, but cannot produce an FM group at expiration.

## Flight levels and terrain

All requested FL values mean **standard pressure altitude**, including FL030.
Convert FL × 100 ft to standard-atmosphere pressure, then interpolate U/V
linearly in log pressure between valid enclosing pressure levels. Compute wind
direction/speed after interpolation. Store target pressure, brackets and weights.
Use a piecewise standard atmosphere above the tropopause; do not extend the
tropospheric lapse-rate formula through FL450. Constants/method and independent
numeric reference cases belong in PR 3.

No actual-MSL mode in the initial release. If added later, it requires separate
model-height interpolation, clear labels and tests. Do not relabel 925 hPa as
3,000 ft or 850 hPa as 6,000 ft. Mask target or bracket levels below model surface
pressure. Unknown surface pressure means the below-ground check is unknown,
not sea level. Preserve negative terrain elevations. Surface geopotential is
model orography, not a precise station elevation; its reference/units must be
verified before use. Sky heights in the spot text are AGL, explicitly identified
in the export metadata, and must not be inferred from pressure-level heights.

## Forecast elements and permitted first guess

| Element | Initial behavior |
|---|---|
| Sustained wind | Supported 10 m U/V → knots and meteorological true direction; preserve raw values. Normalize after rounding through north; calm needs valid low wind, never missing U/V. |
| Gusts | Show direct maximum with its actual interval; qualifying aviation gust coding/change detection is implemented and tested in PR 4. Do not automatically move an interval maximum to a future period. |
| Visibility | Manual completion until a direct accessible field or independently reviewed diagnosis is validated. No P6SM default. |
| Sky/ceiling | Display total/low/mid/high cloud fractions as supporting guidance. They do not give layer bases, ceiling height, or independent additive layers. Manual layer/ceiling completion initially. |
| Precipitation | Display verified rate, accumulation and type as guidance. Type/intensity → text mapping requires PR 4 validation. Unsupported/unknown codes stay unresolved; do not map all precipitation to rain. |
| Thunder/fog/obscuration | Forecaster supplied initially. CAPE/convective precipitation are context, not a thunderstorm forecast or probability. Reduced visibility alone does not identify fog. |
| TEMPO/PROB | Forecaster supplied; validate group times within validity and preserve local conventions. No numeric probability inferred from deterministic snapshots. |
| Aloft | Optional FL winds from validated profiles; unavailable/underground levels explicitly identified for review. |

Final text must contain an initial prevailing group with wind, visibility and
sky description. Weather/obscuration must be explicitly reviewed, including an
explicit internal “none expected” decision when no weather token is appropriate.
Each FM group supplies complete prevailing elements. Changes in cloud amount,
ceiling, gust onset/cessation and visibility must survive compression. A no-ceiling
state is different from no cloud or unknown ceiling. Fewer clouds and poor
visibility can coexist; do not manufacture a physical causal relationship.

## Review, finalization, export and retention

1. Retrieval returns a usable draft with per-element quality, or a named failure.
   An all-empty response is a failed generation, never an approvable report.
2. An incomplete draft may be edited. Required unresolved elements must be
   completed manually or addressed by an explicitly permitted not-applicable
   state. Required visibility and sky cannot be waived as not applicable.
3. Forecaster review records the draft ID/version, reviewed period/location,
   domain disposition, manual edits and finalization time. Authentication alone
   is not an approval. Editing finalized text creates a new draft version.
4. Finalization validates structure, times, required fields and absence of
   placeholders against this local format. It cannot verify forecast truth.
5. Export UTF-8 text and a package of final text plus provenance/quality JSON;
   optional chart is labeled **original model guidance**, including after edits.
   Include model/cycle, location, issue time, UTC validity, sky AGL/wind FL
   reference and manual contributions. No external dissemination occurs.
6. Initial server-side drafts expire after 24 hours and are explicitly described
   as temporary. Finalization does not promise server archival. Downloaded
   packages go to the established NAOC folder through the existing workflow.
   PR 1/5 must select storage that respects worker isolation, expiry and quotas;
   document cleanup before enabling retention. Do not log forecast body/labels
   or credentials. The 24-hour draft lifetime is an implementation default that
   can be adjusted without changing the meteorological contract.

## Illustrative products — not forecasts for actual operations

Incomplete draft (synthetic wind, deliberately unresolved weather fields):

```text
DRAFT — FORECASTER COMPLETION REQUIRED
Location: 38.00 N, 49.00 W — TRAINING EXAMPLE
Valid 2026-11-20 00Z to 2026-11-21 00Z
FM200000 23020KT [VISIBILITY REQUIRED] [WEATHER REVIEW] [SKY REQUIRED]
```

Completed formatting example (synthetic, adapted to the local spot format):

```text
AWC Aviation Spot Forecast — TRAINING EXAMPLE
Location: 38.00 N, 49.00 W
Issued: 2026-11-19 2330Z
Valid 2026-11-20 00Z to 2026-11-21 00Z
FM200000 23020KT P6SM BKN040
FM201200 25020KT 3SM RA BR SCT020 BKN030
  TEMPO 2017/2022 1SM -TSRA OVC015

Winds (flight levels; true direction/knots):
FL030 — 21020KT; from 20/12Z 24035KT
FL060 — 22030KT; from 20/12Z 25040KT
```

Dates and meteorological values in these examples are invented to show structure;
they are not reconstructed from the captured Bahamas case. Human weather/sky
edits and source identification belong in the accompanying provenance record.

## Acceptance ownership

Scott accepted the starting positions in the rebuild plan. Code/test acceptance
is separate from operational acceptance of representative forecasts. The
source-to-operational-domain issue in [domain-source.md](domain-source.md) must
be resolved before PR 3 enables automatic location rejection. PR 1 can proceed
with numeric input validation and the agreed missing-data semantics immediately.

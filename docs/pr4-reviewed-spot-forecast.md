# PR 4 — reviewable aviation spot forecast elements

Based on merged PR 3 (`f5e56f0`). This adds structured forecaster completion and
TAF-format composition to the existing ECMWF point workflow. Original model
values remain separate from forecast entries. Downloads remain editable drafts;
this PR does not finalize, transmit or archive an operational forecast.

## Forecaster workflow

1. Generate the requested ECMWF draft, including optional flight-level winds.
2. Review the model context beside each exact requested/native forecast time.
3. Enter visibility, weather and AGL sky layers, and review wind/gust decisions.
   Common conditions can explicitly fill blank time-slot fields. Individual rows
   override those common values. Blank input stays unresolved; `NONE` records an
   explicit no-significant-weather decision.
4. Optionally enter manual TEMPO or PROB30 groups with complete UTC dates/times.
5. Rebuild text from the reviewed elements, then make any free-text adjustments
   in the existing editor and download the UTF-8 draft.

The rebuild button deliberately uses the element form. Free-text changes do not
change structured elements or original guidance, and must be made after rebuilding.
The page states this distinction. The original chart stays identified as original
model guidance. Final text/version association, finalization checks, provenance
packages and the NAOC archive workflow remain PR 5.

Every element retains quality, original guidance and latest forecaster entry.
An unchanged reviewed model wind keeps its derived status. Replaced elements
are manual, retaining the original source element. Clearing a value returns it
to an unresolved state. This is a current review record, not an immutable edit
history or an authenticated identity for a named forecaster.

## Forecast text and normalization

The supplied *AWC Spot Weather Forecast Coordination/Collaboration 10-001-003*
was read again for this implementation. Its point workflow, TAF-format text,
requested validity and optional FL winds remain the product authority. It includes
a 72-hour example. No airport station/WMO identifier or airport 24/30-hour duration
restriction is added.

- Each prevailing FM contains wind, visibility, weather decision and sky. A
  reviewed `NONE` omits the weather token; blank weather keeps a review prompt.
  All unresolved rows remain visible, including recovery from a missing slot.
- Complete rows compress only when every normalized aviation element matches.
  Wind direction/speed, gust onset/change/cessation, visibility, phenomena and
  cloud layers all participate. Clear-to-ceiling and cloud-amount changes cannot
  disappear because mean wind stayed unchanged. All original time slots remain
  in the structured record even when text is compressed.
- Wind uses true direction, knots, north as 360, calm as 00000KT and three-digit
  speeds when necessary. Numeric U/V conversion uses half-up rounding, including
  the half-knot boundary. Manual gusts must exceed sustained speed; the forecaster
  assesses expected gust behavior. A model preceding maximum is **not** enough
  to establish future peaks/lulls or justify an automatic G group.
- Visibility uses the NWS TAF reporting values: 0, 1/4, 1/2, 3/4, 1, 1 1/2, 2, 3,
  4, 5, 6 and P6SM. Numeric input rounds down, visibly noted: .75 → 3/4SM,
  1.5 → 1 1/2SM, 2.5 → 2SM, exactly 6 → 6SM, >6 → P6SM. Missing/nonfinite
  values never become P6SM. Low visibility with `NONE` produces a review note,
  without diagnosing fog from visibility.
- Manual weather supports mixed precipitation and independent obscurations,
  such as `-RASN`, `-FZRA`, `PL BR`, or `RA FG`. Unknown codes are rejected in
  the element form. Free text remains editable; this is not a complete final-text
  aviation grammar validator.
- Manual FEW/SCT layers have no ceiling. BKN/OVC and vertical visibility define
  a ceiling independently of the lowest cloud base. All heights are feet AGL.
  SKC, known ceiling, no ceiling and unknown vertical visibility remain distinct.
  VV/// can remain an unresolved prevailing draft entry; it is not a completed
  known ceiling. No cloud fraction is turned into an arbitrary layer height.
- TEMPO/PROB30 are explicit forecaster choices, never inferred from CAPE or
  deterministic snapshots. Periods use full dates, whole UTC hours and must lie
  within the requested window. Groups that cross FM changes are split at those
  boundaries, preserving their original manual period in the structured record.
  Omitted group elements inherit prevailing conditions. No group starts at or
  beyond expiration. No probability or occurrence-duration basis is invented.

## Model weather context and boundaries

Precipitation-type interpretation follows the IFS subset of WMO 4.201:
0 dry, 1 rain, 3 freezing rain, 5 snow, 6 wet snow, 7 rain/snow mix, 8 ice pellets,
12 freezing drizzle. Codes not validated for IFS, inconsistent type/rate pairs,
missing values, mismatched points/cycles/valid times or unsupported units remain
unresolved. Categories are not time interpolated.

For rain only, a native, matched positive instantaneous rate supplies an optional
candidate: light at ≤2.54 mm/h, moderate above 2.54 through 7.62 mm/h, heavy above
7.62 mm/h. These boundaries convert 0.10/0.30 in/h without rounding the model rate
first. The candidate is labeled for review; an explicit button replaces the
weather input with it. It is not automatically inserted into a forecast, and a
single instantaneous sample does not establish prevailing persistence.

Winter phases are displayed by their real model diagnosis, with phase/intensity
requiring manual review. Liquid-equivalent rate is not converted into snow,
mixture, ice-pellet or freezing-drizzle intensity. Freezing rain is not relabeled
ordinary rain. No new vertical-profile phase diagnosis is claimed. IFS limitations
and local terrain/thermal structure still require forecaster assessment.

Total/low/mid/high cloud fractions are shown independently. Their sum is not
used for cloud amount or ceiling. Temperature/dewpoint and CAPE are context.
There is no automatic fog, thunder, shower, ceiling or numerical-probability
forecast. Model-dry precipitation is not a no-significant-weather decision.
Gust maxima retain their actual preceding support intervals and are not shifted
into future periods. At an off-hour boundary, non-interpolable fields stay
unavailable; the native table remains available for review.

## Temporary review storage and request controls

A server-side workspace is necessary to connect original guidance with manual
entries without trusting model payloads submitted by a browser. It uses a private
local SQLite file on the existing host, not an external database service.
`AUTOSPOT_REVIEW_DB` can select the path; by default `reviews.sqlite3` is beside
the existing security database. No new runtime dependencies or infrastructure
are introduced.

Records belong to the authenticated session, use random identifiers, and have
an absolute 24-hour lifetime from creation. Signing out, session expiry or a
credential change ends access sooner. Access/creation/save/startup remove expired
rows. Revisions use atomic compare-and-swap: stale tabs get 409; another session
gets 404; revoked sessions cannot reopen a workspace. All saves require CSRF,
rate limits, bounded values and duplicate-field checks. An invalid later entry
cannot partially save earlier entries.

Bounds: 4 MiB JSON per workspace, 32 MiB aggregate payload, 64 active workspaces,
and 16,384 SQLite pages (normally 64 MiB main file). Full storage returns a named
503 rather than silently evicting another active review. Edits do not extend the
expiry. The private, single-host file survives web-worker recycling but is not a
cross-replica store or a durable archive; deployment may discard it. Existing
single-replica security limitations remain. Forecast bodies, operation names,
credentials and source payloads are not added to application logs.

## Validation and evidence

- 109 offline tests pass: original regressions plus visibility boundaries,
  combined weather, ceiling/base states, wind rounding, gust transitions,
  complete FM composition/compression, missing samples, 72-hour/year rollover,
  conditional splitting, manual probability, owner/CSRF/stale-write protection,
  multiworker saves, expiry/quotas and atomic invalid-input rejection.
- Two new real raw messages (ptype and total instantaneous precipitation rate)
  contain positive-rate cases for **all seven nonzero IFS categories**: rain,
  freezing rain, snow, wet snow, mixture, ice pellets and freezing drizzle.
  Source URLs, byte ranges, record metadata, timestamps and hashes are retained
  in `tests/fixtures/pr4/nonzero-precipitation.json`. Offline tests re-decode the
  actual GRIB bytes at each captured grid point. These are model values, not
  verification of weather truth or operational acceptance of intensity.
- All 12 source-geometry reference checks, lint, dependency consistency and the
  locked-runtime advisory scan pass.
- [Local HTTPS evidence](evidence/pr4-web-check.json): two Gunicorn workers,
  real spawned ECMWF acquisition, off-hour six-hour request with FL030/450,
  manual element/TEMPO rebuild, six subsequent reloads with worker recycling,
  stale-write 409, other-session 404 and exact edited Unicode download passed.
  Generation took 42.027 s locally, including retrieval/render/redirect. Test
  forecaster entries were explicitly synthetic, not operational weather.
- Chromium browser automation could not launch because this environment denied
  its local socket. HTTP/form/template tests and decoded PNG checks were used;
  a complete interactive browser layout check remains outstanding.

No Railway configuration or production deployment was changed. Domain assignment
still requires the unresolved spot-service boundary decision from Phase 0.
Weather/element review is distinct from finalization and operational acceptance.

## Source basis (checked November 10, 2026)

- Supplied AWC local spot procedure 10-001-003 and [Phase 0 product contract](phase0/product-contract.md).
- [NWSI 10-813, October 30, 2024](https://www.weather.gov/media/directives/010_pdfs/pd01008013curr.pdf), Appendix B for element encoding and visibility; local spot duration/format remains governing.
- [NWS rain-intensity context](https://www.weather.gov/asos/PresentWeatherSensor.html), 0.10/0.30 in/h categories; application to instantaneous model guidance is explicitly a review candidate.
- [ECMWF precipitation-type table](https://codes.ecmwf.int/grib/format/grib2/ctables/4/201/) and [current IFS precipitation-type interpretation](https://confluence.ecmwf.int/spaces/FUG/pages/673550526/Section+2A.1.5.5+Types+of+Precipitation).
- [IFS freezing-drizzle introduction and supported subset](https://confluence.ecmwf.int/pages/viewpage.action?pageId=327675207).

ECMWF data © ECMWF, redistributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The raw messages are unmodified byte slices; point/context JSON is a derivative.
ECMWF does not endorse this application or the derived forecast text.

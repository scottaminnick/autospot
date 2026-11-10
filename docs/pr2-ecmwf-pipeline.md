# PR 2 — verified ECMWF data acquisition

Based on merged PR 1 (`5b18f7f34b39ef63ab4fd632e8fe030ac91c3bdd`).

## Result and scope

The request path now uses named, verified point values from ECMWF IFS 0.25°.
It preserves the selected run, UTC validity, source intervals, grid point and
provider provenance. Unsupported, missing, invalid and not-applicable inputs
remain null values with explicit quality states. The former xarray-first-variable
fetcher, nested retries and silent meteorological defaults are removed from the
application path.

The original Bahamas case retrieved all 144 planned surface samples with no
missing or invalid values. Its seven retained raw-message hashes matched Phase 0.
Observed retrieval was 34.551 s without a warm cache and 0.083 s for a matching
cached repeat. These are local measurements, not Railway guarantees. The
first-guess text still requires forecaster completion for visibility, weather
and sky; a successful data retrieval is not a completed aviation forecast.

## Run and field contracts

- Examine at most four six-hourly cycles, newest first. Use absolute requested
  start **and end**, with native slots spanning the interval and necessary
  bracketing slots. 00/12Z support three-hour steps through +144 and six-hour
  steps thereafter; 06/18Z stop at +144. No clamping of negative offsets.
- For each viable candidate, inspect start, end and every intermediate slot.
  Required core inventory is 10 m U/V, total cloud and surface pressure.
  An unpublished first object skips that candidate promptly. All core slots
  must be indexed for a candidate to count as inventory-complete.
- Prefer the newest inventory-complete run. If no complete candidate exists,
  a partially indexed candidate may provide an explicitly incomplete draft.
  Never fill its gaps with another cycle or model. The diagnostic and data
  quality records distinguish inventory availability from successful decoding.
- Supported surface fields are U/V, gust, total/low/mid/high cloud fraction,
  precipitation rate/type/total/convective accumulation, CAPE, 2 m temperature,
  dewpoint, skin temperature and surface pressure. Surface geopotential is
  separately retrieved at step zero. Pressure U/V/T/geopotential-height contracts
  are available; actual FL sampling/output is PR 3.
- A maximum 700 field jobs is checked before source requests. The current
  surface-only 72-hour plan stays below that cap. Unknown or malformed requests
  remain subject to PR 1 input limits before the pipeline is started.

Every accepted GRIB message must match parameter ID, decoded level type/value,
units, cycle, valid time, processing type/interval and hour units. It must also
match the expected regular 0.25° global grid, ECMWF centre, IFS model, operational
forecast stream/class/type and production status. The generating-process ID is
recorded rather than hard-coded across model upgrades. Missing bitmap/category
values and nonfinite data are never numeric weather defaults.

Instantaneous fields must have identical start/end times. Gust maxima preserve
their preceding interval, including the observed one-hour maximum at +12.
Accumulations must start at the selected cycle; no rate is inferred from them in
this PR. Cloud fractions are not added into layers or assigned synthetic heights.
Precipitation type remains categorical guidance rather than automatic weather.

## Providers, ranges and retries

The tested source order is ECMWF-hosted, AWS's official regional endpoint, then
Google's ECMWF mirror. Their base URLs follow ECMWF's own `ecmwf-opendata` source
registry: <https://github.com/ecmwf/ecmwf-opendata/blob/main/ecmwf/opendata/urls.py>.
Azure is not enabled because its current access flow needs additional validation.

Each provider supplies its **own index and GRIB object identity**. The index is
strict JSON-line data: malformed records, invalid sizes/offsets, overlap, wrong
run metadata and ambiguous requested fields are rejected. The +0 index's +3
temperature placeholders are not silently relabeled to step zero.

Object HEAD metadata establishes size and identity. Every field GET requests an
exact byte range with If-Match when an ETag is available. HTTP 206, exact
Content-Range, exact byte count, ETag and complete single-message GRIB framing
are required. Oversized responses, wrong offsets, extra/truncated bytes and
changed identities trigger an explicit failure or another provider's own lookup.
A changed object invalidates its cached context for the request.

One transport layer controls retry behavior: at most two attempts per operation,
no adapter/nested retries, jittered backoff, bounded Retry-After handling and
provider cooldowns after transient failures. 404s are not retried. Redirects and
encoded range responses are rejected. Source URLs are fixed in code, never
accepted from a request or cache entry without matching the expected URL.

Connect/read timeouts are each bounded to ten seconds or remaining budget.
The original two-second connect setting was too short for the observed TLS path;
ten seconds recovered the full reference case. All retries/streams share a
75-second acquisition deadline and four concurrent tasks per request. Data
decoding is serialized within the request process to protect native global state.

The web request runs acquisition in an isolated spawned process. An 80-second
parent deadline terminates outstanding work, including native decode/slow-stream
cases, with a short bounded cleanup allowance. A child kernel alarm also limits
orphaned work on Linux. Two web workers can therefore create at most eight
concurrent acquisition tasks in one replica; no new queue/database service is
introduced. Other chart/render overhead is outside the acquisition deadline.

## Shared positive cache

`AUTOSPOT_FORECAST_CACHE` selects the local SQLite cache path, defaulting to
`/tmp/autospot-state/forecast-cache.sqlite3`. The file is private and shared by
workers on the same replica. No operation label, password or report text is stored.

- Object/index contexts: five-minute TTL, retaining their exact index hash,
  provider URLs, ETag, modification time and object length.
- Validated direct point values: thirty-minute TTL, keyed by policy version,
  model, grid, cycle, step, field/vertical specification, exact requested point,
  provider, index hash, object identity and byte range.
- Recheck cached field identity, units, parameter/level/time, point and finite
  range before use. Corrupt entries are evicted/refetched. Failed or unresolved
  values are never negatively cached.
- LRU/expiry eviction bounds logical values to 32 MiB; SQLite page limits bound
  the main database to approximately 64 MiB. Raw GRIB grids are not cached.
  Cache I/O failure degrades to uncached acquisition, not fabricated data.

The cache is temporary model guidance, not an operational archive or cross-replica
coordination service. A deployment may discard it. Provider identities are reused
only within their TTL; an expired context is re-fetched before a point lookup.

## Draft and display behavior

The legacy automatic formatter cannot consume nullable fields safely, so it is
replaced by a minimal draft: verified native-time sustained winds plus explicit
visibility, weather and sky completion prompts. There are no automatic TEMPO,
PROB, fog, thunderstorm or cloud-base diagnoses. A non-native requested start has
an initial wind-review prompt; native bracketing guidance retains its actual time.
No FM starts at expiration. This containment implements the pipeline's required
missing-value semantics; the full reviewed formatting/diagnoses remain PR 4.

The review page shows original raw wind, gust intervals, total cloud fraction,
instantaneous precipitation, source providers and unresolved states. Charts use
absolute UTC with visible gaps, requested-period boundaries and independent
location/model/cycle identification. They do not plot cloud fraction as a ceiling.
Full field/provenance JSON is inspectable on the page. The text download remains
an edited draft, without claiming transmission, final validation or archiving.

GFS is deliberately unavailable until a GFS adapter meets these contracts. The
server rejects GFS before acquisition and does not switch models. Optional winds
aloft are withheld until PR 3 applies the correct pressure-altitude interpolation
and below-ground checks. Operational domain acceptance remains the Phase 0 gate.

## Verification and replay

Offline tests exercise raw retained GRIB messages and deterministic fault
simulations: wrong field/level/unit/time/grid, malformed indexes and placeholders,
missing first/intermediate/end coverage, separate mirror offsets, wrong/truncated
ranges, changed objects, unavailable providers, retry/cooldown limits, hard process
termination, positive-cache reuse/isolation/expiry/corruption/eviction, and nullable
draft/plot output. Existing web/security and Phase 0 tests continue in CI.

Live evidence is in [pr2-live-check.json](evidence/pr2-live-check.json):

- Full original request: 144 direct samples, 27 explicit unsupported states,
  zero missing/invalid samples; all seven retained raw messages matched.
- Forced ECMWF/AWS unavailability with an **actual** Google range retrieval:
  identical 10 m U message hash and point value. The first two failures were
  injected; Google's retrieval/validation was real.
- Two-worker HTTPS web smoke with a current future request, actual spawned
  acquisition, chart, exact requested start, manual-field prompts, UTF-8 export
  and logout/replay controls passed. No Railway deployment was performed.

Explicit manual live check (not part of ordinary CI):

```sh
python -m scripts.check_ecmwf_live --latitude 24.71 --longitude=-77.76 --start 2026-11-09T12:00:00Z --duration 24 --as-of 2026-11-09T00:00:00Z --warm
```

The `--as-of` option controls candidate discovery for a replay; it does not prove
what was published at that historical time. The rolling archive may expire.
Do not keep polling expired dates. Normal web requests use current discovery time.

Audit ownership: F04, F11, F12 and the acquisition/missing-data part of F01.
PR 3 supplies numeric time interpolation, FL winds, terrain and operational AOR
handling; PR 4 supplies reviewed weather coding; PR 5 supplies finalization,
versioning, provenance packages and archive workflow support.

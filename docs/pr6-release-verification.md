# AutoSpot pilot and release verification

PR 6 adds verification and release documentation for the merged AutoSpot
application. The intended use is an ECMWF-first draft for an AWC forecaster.
Review, coordination, issuance and archiving follow the existing AWC process.
Operational forecaster acceptance is open.

## Verification completed

The application baseline was `a62f58787379336165868a1b2546026c9b7f6490`,
the PR 5 merge. On November 11, 2026 UTC, all 131 frozen/offline tests and
12 source-geometry checks passed. The locked environment passed `pip check`
and the runtime lockfile audit reported no known vulnerabilities.

| Acceptance cases | Verification |
|---|---|
| Bahamas 24-hour UTC window and ending support | [Pipeline regressions](../tests/test_ecmwf_pipeline.py) and a live replay of the November 9 00Z IFS run. Nine native slots were present, steps +12 through +36, with +36 marked as boundary support. All seven retained messages matched by SHA-256. |
| 429/503 and same-cycle mirror recovery, malformed ranges and wrong field identity, +0 record placeholders, partial or total data loss | [Pipeline regressions](../tests/test_ecmwf_pipeline.py) and [web failure handling](../tests/test_web_foundation.py). |
| Off-hour starts, UTC rollover, 72-hour validity, missing wind slots, north-crossing and calm winds, FL030/FL450 and terrain checks | [Coordinate and domain tests](../tests/test_coordinates_domain.py), [pipeline tests](../tests/test_ecmwf_pipeline.py) and [product tests](../tests/test_aviation_product.py). |
| Gust intervals and changes, clear to overcast, ceiling versus cloud-base and unknown states, visibility increments, overlapping layers, CAPE and manual weather, fog with rain, winter precipitation | [Product tests](../tests/test_aviation_product.py) and [finalization tests](../tests/test_finalization.py), with actual nonzero IFS precipitation messages for seven type codes. |
| Invalid requests, escaped and Unicode text, session ownership, concurrent edits, final text and separate original chart, ZIP checksums | [Web tests](../tests/test_web_foundation.py), [review tests](../tests/test_review_workflow.py), [finalization tests](../tests/test_finalization.py) and the [PR 5 HTTPS exercise](evidence/pr5-web-check.json). |
| Bahamas, Aleutians, Anchorage, Honolulu, dateline and shared boundary | [Twelve reference locations](../tests/fixtures/phase0/domain/test-locations.json) checked against the application classifier. Geometry is advisory. Service responsibility follows the local procedure. |

The [GDAL comparison](evidence/pr6-reference-check.json) covers 20 point values
from eight frozen GRIB2 messages. GDAL's GRIB driver and AutoSpot's ecCodes
path independently sampled the same native grid cells. The checks matched
cycle, interval and vertical level. Values agreed within one source packing
increment in native units, and all seven precipitation codes matched exactly.
These comparisons verify decoding and point sampling. Forecast accuracy
requires the side-by-side forecaster review below.

To repeat the decoder check, install GDAL CLI tools (`gdal-bin` on Debian)
and run from the repository root:

```sh
python -m scripts.check_ecmwf_reference
```

See the [live check results](evidence/pr6-live-checks.json) for request
timing and resource measurements. The local one-worker HTTPS request reached
the review page in 53.0 seconds with a fresh cache. The identical repeat took
11.9 seconds and missed the proposed five-second target. The model-only
historical replay repeated in 0.17 seconds with 144 cached fields. In a
separate live probe, all three mirrors returned HTTP 404 for the newer
cycle's first required step after 12.1 seconds combined. Cycle discovery
checks newer candidates before using cached fields from an older run.
Measure staging repeats both with the newest cycle available and while
waiting for it, then decide whether the observed latency is acceptable.

To repeat the historical acquisition while the run remains on the provider:

```sh
python -m scripts.check_ecmwf_live --latitude 24.71 --longitude -77.76 \
  --start 2026-11-09T12:00Z --as-of 2026-11-09T05:59Z --warm
```

The replay constrains candidate cycles. Historical publication timing cannot
be inferred from this retrieval.

## Railway staging

The isolated [Auto-Spot Pilot project](https://railway.com/project/87e2d789-b953-47f3-af73-aa0c25e1aa17)
has a `staging` environment and a `pilot` service. The service configuration
uses one us-west2 replica, one Gunicorn worker, a 180-second worker timeout,
worker recycling at five requests plus up to two jitter, a 60-second
deployment readiness timeout at `/healthz`, and serverless sleep. The start
command runs `python -m pip check` before Gunicorn. The pilot uses credentials
scoped to its staging service and an exact GitHub revision.

Production was already following `main` with automatic deployments. Railway
showed the PR 5 merge at deployment `2dd71131-175a-41c2-8a76-0b071ffea764`.
Build logs showed Railpack using Python 3.12.14 and installing
`requirements.txt`. Runtime logs confirmed Gunicorn 25.3.0 and two workers.
Production's start command overrides the Procfile, uses a 180-second timeout
and omits worker recycling. Production has one replica, serverless sleep and
no configured readiness path. These observations came from the production
configuration and logs.

1. Configure fresh `APP_PASSWORD` and `SECRET_KEY` only on the pilot service.
   Use the Railway Variables view or an authorized secret-setting operation.
2. Connect the pilot service to the exact PR 5 merge SHA above and deploy it.
   Generate a pilot HTTPS domain. Verify readiness, login and a draft request.
   Record the successful deployment ID and configuration for rollback.
3. Pin the pilot service to the PR 6 candidate SHA and verify the deployment
   reports that revision. Capture Python version, installed versions for the
   packages in `requirements.txt`, `pip check`, and build and boot logs. Audit
   that package set. Confirm readiness, worker recycling and request/timeout
   logs. Keep credentials, cookies, request bodies and operational data out
   of the record.
4. Run the two HTTPS checks below. On a fresh cache, check the first 24-hour
   request against the proposed 60-second target and the identical repeat
   against 5 seconds. Record the selected cycle and separate any serverless
   wake time from request time. Watch Railway CPU, peak memory, outbound
   traffic and worker restarts during an individual request and two overlapping
   requests from separate sessions. One sync worker can queue the second
   request. Each active model request uses a child process with up to four
   download threads, a 75-second transport budget and an 80-second web wait.
   Confirm that the chosen configuration meets the pilot workload.
5. Rehearse rollback on staging. Redeploy the recorded PR 5 deployment,
   verify the active revision, health, login and draft generation, and
   redeploy the saved candidate. Record both deployment IDs, recovery time,
   and whether a fresh login is required. If using the Railway dashboard's
   **Rollback** action for production, remember that it also restores the
   previous variables. See
   [Railway deployment actions](https://docs.railway.com/deployments/deployment-actions).

```sh
python -m scripts.check_pilot_web https://PILOT-HOST --summary surface.json
python -m scripts.check_pilot_web https://PILOT-HOST \
  --duration 6 --flight-levels 30,450 --summary aloft.json
```

The script prompts for the pilot password and times from submission through
loading the review page. It leaves both requests as incomplete drafts. A first
request is cold only when the cache started empty, and a repeat can select a
new cycle if one becomes available. Use the logged request IDs and cache
evidence to interpret the timings. Local cache and draft stores are ephemeral.
Export approved text and ZIPs to the established archive before replacement
or rollback.

## Hosted pilot results

Railway verified readiness and installed the same 35 pinned packages on PR 5
and PR 6. The startup consistency check passed for both deployments. The
locked package audit reported no known vulnerabilities. The pilot served
authenticated HTTPS drafts and recycled its Gunicorn worker during the checks.

| Hosted check | Result |
|---|---|
| 24-hour surface draft | 28.169 s to review on a fresh cache, 8.447 s on the identical repeat. Both selected the November 10 18Z run. The repeat exceeded the proposed 5 s target. |
| Two simultaneous six-hour drafts | 29.389 s and 29.884 s to review from distinct sessions. The single worker processed the submits in sequence. |
| Archive smoke with FL030 and FL450 | Explicit approval with synthetic review entries produced matching UTF-8 text and ZIP. All manifest file hashes matched. |
| Rollback rehearsal | Railway redeployed the saved PR 5 build and then the saved PR 6 build. Readiness passed after 31.5 s and 25.2 s respectively. The old test session needed a fresh login. |
| Railway resource sampling | Peak samples were 0.193 GB of memory and 0.784 CPU cores across the pilot measurements. Railway sampled every 30 seconds. |

The [hosted evidence](evidence/pr6-railway-pilot.json) records the deployment
revisions and request IDs. The pilot demonstrates the workflow and timing
for these requests on the revision recorded in the evidence. In Railway, use
the staging service's Variables view to retrieve its login password. The review
page opens on raw native-time ECMWF values and the meteogram. The editable
forecast box and **Approve & Save Report** restore the original sequence.
The service check and local UTF-8 download apply to the text in the box. Review the repeat latency alongside your operational cases
before release.

## Forecaster pilot and release decision

Run the pilot alongside the existing workflow using representative requests.
Use the same location and UTC validity, and record the selected model cycle and
forecast lead before comparing results.

| Pilot request | What Scott reviews |
|---|---|
| Typical 24-hour in-domain request | Surface wind/gust timing and units, visibility, weather and ceiling decisions, text edits and the two exported charts. Compare with the model guidance normally used at the desk. |
| Off-hour start with FL030 and FL450 | Initial FM time, native UTC brackets, terrain/pressure and any unavailable levels. Compare with the corresponding IFS pressure-level winds. |
| Wet or low-ceiling case with a significant change | Compare the draft and completed FM or TEMPO with operational guidance, observations and satellite or radar when relevant. Verify that required manual elements remain visible. |
| Bahamas or office-boundary request | Confirm which office is responsible under the local spot procedure and verify that an unconfirmed draft cannot finalize. |

For each case, retain the location, UTC window, guidance cycle, independent
guidance or imagery used, discrepancy and its disposition, elapsed time, and
reviewer decision. Complete one approved test package and verify its text,
original guidance, forecast chart, manifest and archive procedure.

Keep PR 6 in draft while Scott reviews representative output and the
observed performance.
A merge to `main` can deploy immediately. Before release, confirm the
production deployment and known-good rollback ID in Railway, coordinate a
time for the change, and confirm that no active draft relies on temporary
storage. After deployment, verify the active revision, health, login, a live
draft and archive smoke, and watch logs and resource use. On readiness,
authorization, data-integrity or release-blocking forecast failures, use the
recorded rollback deployment. Repeat the smoke checks and use the existing
forecast workflow until service is restored.

## Release notes

- ECMWF IFS 0.25° guidance uses validated, time- and level-specific sampling,
  same-run provider fallback, and explicit missing-field states.
- UTC validity and optional FL010–FL450 winds retain source brackets and
  terrain checks. Confirm service responsibility under the local procedure.
- Review visibility, weather, sky and wind changes, then approve a saved
  version. The matching text and ZIP preserve provenance and checksums.
- GFS is unavailable. BECMG and LLWS may be saved as draft text. The approved
  text grammar supports FM and reviewed TEMPO/PROB30. Downloads are local and
  the existing coordination and archive process remains in use.

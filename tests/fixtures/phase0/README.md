# Phase 0 frozen evidence

This directory contains **research/test evidence**, not a forecast for operational
use and not data that the live app imports. No passwords, production settings,
user messages, or real operation labels are included.

## Contents and provenance

- `ecmwf-20261109/indexes/`: exact live JSON-line inventories, including the
  original 00Z +12…+36 request, other cycle/cadence probes and the AWS mirror.
- `ecmwf-20261109/messages/`: seven original, complete GRIB messages extracted
  with HTTP Range, including 10 m U/V, gust, rate, type, total cloud, and step-zero
  surface geopotential. Only these seven raw messages are preserved locally.
- `point-samples.json`: 41 decoded nearest-grid-point samples, parameter/units,
  pressure levels, time/statistical metadata, source URLs/ranges/hashes. The 34
  other raw messages were decoded but are not committed; their point metadata
  and message hashes remain. This is sufficient for normalized-data tests,
  not independent redecoding of those 34 messages after the live archive expires.
- `inventory-observations.json`: all index attempts, including temporary
  research-network proxy failures. These failures are not evidence that the
  provider was down. Successful later attempts are recorded separately.
- `initial-capture-failures.json`: original transport failures retained for
  provenance. It is an attempt log, not the final data fixture.
- `mirror-comparison.json`: AWS 10 m U, retrieved from its own index; message
  SHA-256 matches the primary source. AWS byte offsets are never presumed equal.
- `domain/`: unchanged AWC source geometries plus a derived three-FIR AAWU
  subset and geometry test-location expectations. Not an operational signoff.
- `manifest.json`: SHA-256 and byte count for every evidence file except this
  manifest itself. Update deliberately when accepting a changed fixture.

The original request is 24.71 N, 77.76 W, 2026-11-09 12Z–2026-11-10 12Z.
The nearest model node is 24.75 N, 77.75 W. All samples are model predictions;
none is observed weather or a validated final forecast. The first sample has
wind ≈41.4322° true at 7.19764 kt. If rounded by the intended wind convention,
that suggests `04007KT`; it does not resolve visibility, sky or weather.

## Offline validation

Run from the repository root in an environment with ECMWF ecCodes installed:

```sh
python -m unittest discover -s tests -p 'test_phase0*.py' -v
```

Capture used Python 3.12 and ecCodes 2.48.0 Python bindings / 2.49.0 C library
(actual API version recorded in JSON). Tests make no network calls and do not
import the production app. Installing/locking runtime dependencies and CI belongs
to PR 1. Raw decoding tests fail on a missing decoder rather than reporting a pass.

To attempt a fresh dated capture before the rolling archive expires:

```sh
python scripts/capture_phase0_ecmwf.py --date 20261109 --output /tmp/autospot-new-capture
```

The capture is sequential, uses bounded per-request timeouts, and refuses to
replace a directory. It performs no authentication or application changes.
The frozen record includes recovered attempts after a temporary proxy failure;
rerunning a live capture is not expected to reproduce provider status/timestamps.
The optional mirror comparison and boundary captures are documented by exact
source URLs and hashes; they are not performed by this ECMWF capture script.

## Attribution

ECMWF IFS forecast data © ECMWF, CC BY 4.0
<https://creativecommons.org/licenses/by/4.0/>. Raw messages/indexes are unchanged;
point metadata are extracted derivatives. ECMWF does not endorse the app or
forecaster products. AWC geometries are credited to NOAA/NWS Aviation Weather
Center; exact URLs, retrieval times and response metadata are in `domain/sources.json`.

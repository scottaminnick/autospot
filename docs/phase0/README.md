# Phase 0 — product and data contract

Completed research package: 2026-11-10. Baseline main commit:
`30cae1e205ac67844b2dbf2a1cb1715ef148212f`.

## Review order

1. [Product contract](product-contract.md): accepted ECMWF-first direction,
   1–72-hour requests, UTC handling, optional FL winds, incomplete/final states,
   manual fields, example outputs and temporary draft/export behavior.
2. [ECMWF capability matrix](ecmwf-capabilities.md): actual decoded field IDs,
   units, processing intervals, provider/cycle evidence and unsupported fields.
3. [Domain source decision](domain-source.md): AWC aviation geometry and AAWU
   FIRs, including the new Bahamas boundary finding and operational review gate.
4. [Acceptance cases](acceptance-cases.md): expected behaviors for the six
   implementation PRs, with live versus synthetic evidence distinguished.
5. [Frozen evidence](../../tests/fixtures/phase0/README.md): capture provenance,
   licensing, hashes and offline validation instructions.

## Findings that change implementation

- The live IFS feed supplies low/mid/high cloud fraction, precipitation type,
  surface temperature/dewpoint and pressure. The existing app omits several of
  these useful inputs. Direct visibility and layer bases remain unsupported in
  the checked feed, so the first rebuilt draft needs manual completion.
- Original 00Z +12…+36 wind data were recovered for the Bahamas request. There
  are 41 frozen point samples, seven raw GRIB messages, and an independent
  provider copy of 10 m U with identical message hash. No claim of improved
  application behavior follows from a successful data capture.
- Gust time support is 11–12 h in the captured +12 file. Two step-3 temperature
  placeholders appear in the step-zero inventory. Both demonstrate why decoded
  field metadata must take precedence over a filename/cadence assumption.
- The original Bahamas test point is outside AWC's published SIGMET polygon.
  This needs a spot-service scope decision before automatic domain blocking.
  Source geometry is available; whether that is the exact spot-service boundary
  remains an operational acceptance item. PR 1/2 can proceed independently.

## Validation performed

```sh
python -m unittest discover -s tests -p 'test_phase0*.py' -v
python scripts/check_phase0_domain.py
```

The offline suite validates fixture integrity, redecodes the seven retained
messages, verifies the original timeline and model sample values, checks
processing intervals, and preserves source topology distinctions. It does not
exercise or certify the current forecast generator. No new live-data test is
required on ordinary test runs.

## Exit assessment and next PR

| Phase 0 deliverable | Result |
|---|---|
| Product specification and sample output | Written; inherited starting positions accepted by Scott; additional bounded implementation defaults explicitly recorded. |
| Actual-feed capability sheet | Written with decoded sample metadata and limitations; no automatic ceiling/visibility diagnosis promised. |
| Aviation boundary source/test points | AWC-hosted sources frozen; 12 reference locations checked. Operational spot-boundary equivalence needs review before PR 3 gating. |
| Reproducible fixtures/expected results | 41 extracted samples; seven raw messages; inventories, hashes, source geometry and acceptance cases retained. |

Proceed to **PR 1: validation, escaped templates, authentication/session
hardening, dependency/runtime pinning, structured errors and regression CI**.
Use this package's field states and request contract as the implementation
reference. PR 2 then replaces acquisition with explicit field validation,
bounded retries/cache, complete-interval checks and same-model provider failover.

This PR adds documents, research capture/check scripts and offline test evidence.
`app.py`, runtime dependencies, Procfile and Railway configuration are unchanged.
It is suitable for review without deploying a changed forecast generator.

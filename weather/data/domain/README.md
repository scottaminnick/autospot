# Aviation reference geometry snapshot

Exact Phase 0 source bytes, captured 2026-11-10; metadata is in `sources.json`.
`awc_sigmet_area.json` is the AWC-hosted candidate SIGMET-area display geometry.
`aawu-firs.geojson` contains all three named Anchorage FIRs extracted from the
AWC-hosted `firpoly.topojson` (original retained in Phase 0 fixtures). Runtime
hashes are pinned in `weather/domain.py`, including the extracted AAWU file.

These are reference layers, not approved spot-service boundaries. Preserve the
source's continuous longitudes. Updating a snapshot requires source comparison,
new hashes and regression review; do not silently treat missing geometry as AWC.
The capture date is not an operational effective date. No automatic refresh or
location blocking is enabled.

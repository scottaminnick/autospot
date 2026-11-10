# Aviation domain source and routing evidence

Checked 2026-11-10. These are aviation sources, not marine forecast zones.

## Selected sources

1. **AWC's own SIGMET-area geometry:**
   <https://aviationweather.gov/gis/awc_sigmet_area.json>.
   The GFA JavaScript map configuration references this URL for its
   `awcSigmetMap` / AWC SIGMET Area overlay. Freeze the returned bytes and hash;
   do not scrape a map image or substitute state/ARTCC boundaries.
2. **AWC-hosted FIR geometry:**
   <https://aviationweather.gov/gis/firpoly.topojson>.
   Extract the three named Anchorage geometries (Continental, Oceanic, Arctic)
   for candidate AAWU routing. Names matter: all three have ICAO `PAZA` in this
   source. Filtering by ICAO alone must not discard any of them.
3. **NWSI 10-811, September 2, 2025**, section 3 and Appendix C:
   <https://www.weather.gov/media/directives/010_pdfs/pd01008011curr.pdf>.
   This establishes the aviation MWO framework and identifies the three Alaska
   FIRs. The Pacific prose and plotted geometry need reconciliation before a
   simplistic latitude/longitude rule is used; preserve the actual geometry.
4. The supplied local spot procedure requires domain verification and redirect
   coordination for AAWU requests. It does not provide a machine-readable spot
   service boundary or authorize the application to infer one from marine zones.

The raw source files, response metadata, SHA-256 hashes, extracted AAWU features,
and test locations are in `tests/fixtures/phase0/domain/`. Retrieval timestamps
identify the snapshot; they are **not** an effective operational date. No
effective-date/version assertion was supplied in the feature properties.

## Important observations

- The AWC polygon contains longitudes west of −180. It is a display geometry
  with continuous longitude wrapping, not a ready-to-use canonical RFC 7946
  GeoJSON boundary. Preserve the original; normalize/split at the dateline in
  PR 3 with explicit tests. Naively wrapping every vertex independently creates
  polygons across the wrong side of the globe.
- The original failing point **24.71 N, 77.76 W is outside this AWC SIGMET
  polygon**. This says what the selected geometry contains; it does not settle
  whether a coordinated AWC spot request can be accepted there.
- The local procedure's sample point **51.7385 N, 161.965 W** is inside the
  captured AWC polygon and outside the three captured AAWU FIR polygons. Do not
  classify it as AAWU simply because it is near the Aleutians.
- Pacific points at 40 N on either side of the dateline are in the captured AWC
  polygon. Honolulu is outside it. A “west of 140 W = outside AWC” rule would
  be incorrect for this geometry.
- Shared edges and overlapping or conflicting classifications require review.
  An unavailable boundary never means the location is automatically in scope.

## Implementation decision and remaining operational gate

Use the versioned AWC aviation geometry as the **candidate reference layer**
and the named Anchorage FIRs for AAWU routing. Preserve the domain check as an
independent result; an outside-domain location is not a data-download failure.

**Before enabling automatic location blocking in PR 3, Scott must confirm that
this SIGMET boundary is the intended spot-service boundary, or identify the
broader coordinated service rule.** This is a newly discovered scope distinction,
not a reason to delay PR 1/2. Until confirmed, the rebuilt review path must say
“Domain requires verification,” show the geometry classification, and require a
recorded forecaster disposition. It must not claim a machine-verified AWC spot
assignment. Known AAWU requests follow the local coordination procedure.

The fixture's expected labels are **geometry regression expectations**, not
approval of operational coverage. Tests include the Bahamas point, Kansas City,
both procedure examples, Anchorage, Honolulu, dateline east/west, a polygon edge,
and an outside point. Arctic/polar topology and all shared boundaries need the
geospatial implementation's dedicated tests before production use.

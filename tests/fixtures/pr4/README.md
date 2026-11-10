# Real IFS nonzero precipitation-type fixtures

Captured from the 2026-11-10 00Z IFS 0.25° operational forecast at +12 h.
Two unmodified raw GRIB messages contain precipitation type and instantaneous
water-equivalent rate. The manifest preserves source URL, record/byte range,
retrieval time, hashes, decoded metadata and point samples.

The global fields contain positive-rate samples for every nonzero IFS category:
rain (1), freezing rain (3), snow (5), wet snow (6), mixed rain/snow (7), ice
pellets (8), freezing drizzle (12). Selected points are representative model
values, not observations, weather verification or forecasts issued by AWC.

The offline suite re-decodes both messages at each captured point and checks
phase handling. Rain alone has a rate-based candidate requiring forecaster
acceptance. Other phases/intensities require manual review; SWE is not a snow
intensity measure. The live archive may expire; these frozen tests need no network.

ECMWF IFS data © ECMWF, CC BY 4.0:
https://creativecommons.org/licenses/by/4.0/ . Source attribution and interpretation
references: `docs/pr4-reviewed-spot-forecast.md`. No ECMWF endorsement is implied.

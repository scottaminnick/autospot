## Railway legacy hotfix

This branch starts from the Railway rollback revision `8cc94e8`. The original
request form, editor, meteogram placement and download button are retained.
The pilot routes requests to GFS only. The ECMWF pipeline is parked in source.
GFS supplies visibility and a cloud ceiling field along with cloud cover. The
guidance table beside the editable report shows those point values by valid time.

The GFS path retains its legacy forecast heuristics. This hotfix corrects run
coverage checks, bounds GRIB range reads and preserves missing visibility,
precipitation and ceiling inputs. Only the reported ceiling can provide a
cloud-height first guess; other cloud layer bases are left to review. Review model-derived entries before
saving. The edited report downloads directly as UTF-8 text.

# Rebuild status

The ECMWF-first rebuild specification and validated data fixtures are in
[Phase 0](docs/phase0/README.md). They document the intended replacement behavior;
the legacy application described below has not yet been repaired.

# AutoSpot — NWSI 10-813 Spot Forecast Generator

A Flask web app that generates NWSI 10-813 format spot forecasts (TAF-style)
for any lat/lon — built for Search and Rescue, ship incidents, and other
point-specific aviation weather support. Originally prototyped in Google
Colab; converted here into a standalone, Railway-deployable web service.

## How it works

1. User submits a lat/lon, start time, and forecast duration
   via the web form.
2. The app pulls only the needed GRIB2 fields for GFS
   directly from public AWS buckets using HTTP byte-range requests (no full
   file downloads, no API key required):
   - **GFS** (global, 25km) from NOAA's `noaa-gfs-bdp-pds` bucket.
3. `cfgrib`/`xarray` decode the GRIB2 bytes; surface fields drive the
   NWSI 10-813 trigger logic (wind shift, visibility change, ceiling
   category change, etc.) that builds FM/TEMPO/PROB30 groups.
4. A matplotlib meteogram (ceilings, visibility, wind, precip) renders
   server-side as a base64 PNG embedded directly in the response — no
   files are written to persistent storage.
5. The forecaster reviews/edits the draft text in-browser, then "Approve &
   Save" triggers a client-side download (no server-side file storage).

## Required environment variables (Railway -> Variables tab)

| Variable | Purpose |
|---|---|
| `APP_PASSWORD` | Shared password gating the whole app |
| `SECRET_KEY` | Signs the login session cookie. **Must stay identical** across deploys/workers - generate once and don't regenerate it casually, or everyone gets logged out. Generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"` |

The app intentionally hard-fails with a 500 on `/login` if either variable
is missing, rather than silently falling back to an insecure default.

## Deploying

1. Push this repo to GitHub.
2. In Railway: New Project -> Deploy from GitHub repo -> select this repo.
3. Set `APP_PASSWORD` and `SECRET_KEY` in the service's Variables tab.
4. Railway auto-detects Python via the `Procfile` + `requirements.txt` -
   no Dockerfile needed. (Verified: `cfgrib`'s ecCodes dependency comes
   bundled via the `ecmwflibs` package, so no `apt-get install
   libeccodes-dev` step is required at the OS level.)
5. First deploy may take a few minutes (matplotlib/xarray/cfgrib are
   sizeable). Subsequent deploys are faster due to build caching.

## Notes on the gunicorn timeout

A single forecast request can legitimately take 15-120+ seconds (multiple
GRIB2 byte-range fetches across many forecast hours, done concurrently via
a thread pool). gunicorn's default worker timeout is 30 seconds, which
would kill these requests mid-flight - the `Procfile` explicitly sets
`--timeout 180` to give enough headroom. If you ever see a forecast
request fail with no app-side error logged, check this first before
assuming a Railway platform issue.

## Credit

Core meteorological/NWSI 10-813 logic and the original Flask app concept
came from an earlier Google Colab prototype; this repo restructures it for
standalone deployment and adds password-gated access.

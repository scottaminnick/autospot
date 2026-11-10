# AutoSpot — aviation spot forecast drafts

AutoSpot retrieves verified ECMWF IFS point guidance and prepares an editable
aviation spot draft. The forecaster completes visibility, weather and sky using
reviewed guidance and follows the existing AWC coordination/archive workflow.

[Phase 0](docs/phase0/README.md) defines the product and freezes reference data.
[PR 1](docs/pr1-foundation.md) adds web/session security and locked dependencies.
[PR 2](docs/pr2-ecmwf-pipeline.md) replaces retrieval with validated fields,
bounded downloading, shared positive caching and same-run provider fallback.

The current path supports ECMWF IFS 0.25°. GFS is unavailable until its adapter
passes the same contracts. Missing visibility/cloud bases do not become normal
weather values: the draft contains explicit completion prompts. Automatic weather
coding, time/FL interpolation, operational domain gating and finalization/version
tracking remain later rebuild work. Aloft output is withheld pending correct FL
handling in PR 3; pressure-field decode contracts are available for that work.

## Install and test

Python **3.12.14** is pinned in `.python-version`. Runtime and development
requirements are fully pinned and hashed. From an isolated virtual environment:

```sh
python -m pip install --require-hashes -r requirements-dev.txt
python -m pip check
python -m unittest discover -s tests -v
python scripts/check_phase0_domain.py
pip-audit -r requirements.txt --disable-pip --no-deps --progress-spinner off
```

The tests use frozen data and mocked upstream requests. The audit consults the
public advisory database; passing means no *known* listed vulnerability was found.

## Configuration

| Setting | Purpose |
|---|---|
| `APP_PASSWORD` | Shared login password; changing it invalidates existing sessions. |
| `SECRET_KEY` | Stable random signing key, at least 32 characters; identical across workers. |
| `AUTOSPOT_SECURITY_DB` | Optional security-state file, default `/tmp/autospot-state/security.sqlite3`. Workers in one replica must share this path. |
| `AUTOSPOT_FORECAST_CACHE` | Optional bounded forecast-cache file, default `/tmp/autospot-state/forecast-cache.sqlite3`. |
| `AUTOSPOT_TRUST_PROXY_HOPS` | Default `0`. Set only after verifying the actual trusted proxy chain; controls forwarded client IP/protocol handling. |

HTTPS is required by default: session cookies are Secure, HttpOnly and SameSite
Lax. For loopback-only HTTP development, an explicit factory override is available:

```sh
flask --app 'app:create_app({"SESSION_COOKIE_SECURE": False})' run --host 127.0.0.1 --port 5005
```

Set password/key in the environment before starting. Never commit them. The
readiness endpoint `/healthz` returns 503 for missing/weak configuration; it does
not fetch weather data. Authentication expires after eight hours; forms expire
after two hours and can be refreshed by loading a new form.

## Railway and deployment

`Procfile` provides the repository start command. A Railway service start-command
override takes precedence; verify it when deploying. PR 1 supports multiple web
workers **within one replica** using shared local SQLite security state. Multiple
replicas require a genuinely shared control store first. Ephemeral state resets
on replacement/deployment, causing reauthentication and resetting rate budgets.

Use `/healthz` as the readiness path when configuring staging. The PR 1 rollout
notes cover cookie/session migration, proxy settings, budgets and remaining work.
Production rollout remains a separate step after staging review.

## Forecaster workflow

1. Sign in and request a location, UTC start and duration (1–72 hours).
2. Review source times, field quality and original model guidance; complete and edit the draft.
3. Download the edited report as UTF-8 text and use the established coordination
   and NAOC archive workflow. The download does not send or archive a report.

Draft/version finalization, missing-field completion, authoritative spot-domain
routing, and provenance packages are covered by later PRs.

## Credit

Core meteorological/NWSI 10-813 logic and the original Flask app concept
came from an earlier Google Colab prototype; this repo restructures it for
standalone deployment and adds password-gated access.

## PR 3: UTC, optional FL winds and domain review

Requests preserve exact UTC boundaries. Permitted numeric interpolation is
labeled with its source times; missing native slots remain gaps. Optional
comma-separated flight levels use standard pressure altitude, log-pressure U/V
interpolation and model-surface checks, with explicit unavailable reasons.
The location map reports candidate AWC/Anchorage geometry and records forecaster
coordination disposition. Automatic spot-service assignment remains disabled
pending the operational boundary decision. See [PR 3 details and validation](docs/pr3-time-flight-level-domain.md).

# AutoSpot — aviation spot forecast drafts

AutoSpot generates an editable point forecast for forecaster review. ECMWF IFS
is the preferred/default model; GFS is an explicit alternate choice. The local
AWC spot procedure defines the intended TAF-format output and optional FL winds.

The rebuild is being delivered in focused PRs. [Phase 0](docs/phase0/README.md)
defines the product and preserves reference data. [PR 1](docs/pr1-foundation.md)
adds request validation, escaped templates, session/security controls, reproducible
runtime dependencies, error handling and CI.

**Meteorological repairs are still in progress.** The existing acquisition and
forecast logic retain the audit's missing-field defaults, approximate flight-level
mapping, timing and weather-coding issues. Review all elements against source
guidance. This foundation PR does not establish forecast accuracy or complete the
ECMWF pipeline rebuild. ECMWF cloud-layer fractions and precipitation type are
available in the checked feed but are not yet integrated into the legacy path.

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

1. Sign in and request a location, UTC start, duration (1–72 hours), and model.
2. Review and edit the draft and original model guidance.
3. Download the edited report as UTF-8 text and use the established coordination
   and NAOC archive workflow. The download does not send or archive a report.

Draft/version finalization, missing-field completion, authoritative spot-domain
routing, and provenance packages are covered by later PRs.

## Credit

Core meteorological/NWSI 10-813 logic and the original Flask app concept
came from an earlier Google Colab prototype; this repo restructures it for
standalone deployment and adds password-gated access.

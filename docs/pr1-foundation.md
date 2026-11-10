# PR 1 — request and web security foundation

Based on merged Phase 0 (`8cc94e821889c254bd46d757936e9f78c1851cf3`).

## Behavior changes

- Forecast input is validated before run discovery, elevation lookup or field
  downloads: finite bounded coordinates, enumerated model, actual UTC calendar
  date/hour, integer 1–72-hour duration, one-line label ≤120 characters, and
  unambiguous single values. Missing model defaults to ECMWF; an invalid model
  is rejected. Invalid dates never become “now.”
- Normal requests start at the current UTC hour or later and end within 144 hours
  of submission. This is a conservative request horizon; it does not establish
  that an actual run covers the interval. Full model/cycle/field coverage remains
  PR 2. Historical replay is not exposed in the request form.
- A maximum 700 field tasks is checked before downloading. Under the current
  legacy field plan, a 72-hour GFS request has 676 tasks. This is a work ceiling,
  not a new retry/deadline/concurrency algorithm; those controls are PR 2.
- Separate Jinja templates autoescape all user/report text. There is no `safe`
  filter around report/chart HTML. Charts pass only base64 image data into a
  fixed image element. CSS is a static asset; inline executable code is removed.
- The edited report downloads as an attachment with the exact UTF-8 bytes.
  The former HTML preview/JavaScript `atob` path is removed. The button says
  “Download edited report,” without claiming final validation or transmission.
- Empty data or no usable surface rows returns an HTTP 502 error without an
  editable/approvable report. Other exceptions return a generic reference and
  correct status rather than exposing exception strings or returning false 200s.
- Responses include a generated request ID, structured status/duration logs,
  no-store, nosniff, CSP, frame restrictions, referrer policy and HTTPS HSTS.
  Request bodies, labels, IP addresses, passwords, and exception text are not
  recorded by application logging. The upstream field logger no longer prints
  arbitrary errors, and it no longer suppresses warnings for the entire process.

## Authentication, sessions and shared controls

`APP_PASSWORD` and a stable `SECRET_KEY` of at least 32 characters are required.
Password comparisons use UTF-8 bytes, so non-ASCII input cannot trigger the old
`compare_digest` string-type failure. Successful login clears the anonymous
session and rotates the CSRF token. The session cookie is Secure, HttpOnly and
SameSite Lax. The new cookie name forces reauthentication after rollout.

The signed cookie carries an opaque random session ID and CSRF state, not the
password or forecast. A shared local SQLite file stores session expiry and a
keyed credential revision. It supports workers sharing one host/replica:

- Absolute session lifetime: eight hours, without automatic sliding extension.
- Logout is POST + CSRF and revokes the server record, rejecting replayed cookies.
- Changing the password or signing key invalidates prior sessions.
- CSRF is required on login, logout, generation and export. Tokens expire after
  two hours; loading a form renews an expired token. Duplicate tokens are rejected.
- Expired session/rate records are pruned during rate-budget transactions.
- SQLite transactions atomically reserve rate budgets across workers; a file
  with private permissions is used, rather than per-process memory counters.

| Budget | Window | Maximum |
|---|---|---:|
| Login attempts, all clients | 15 minutes | 100 |
| Login attempts, client address | 15 minutes | 20 |
| Forecast generation, all sessions | 5 minutes | 20 |
| Forecast generation, one authenticated session | 5 minutes | 3 |
| Report exports, one authenticated session | 1 minute | 30 |

These are fixed windows and count successful and failed attempts. Rate-limited
responses return 429 with Retry-After. A shared password does not establish a
named forecaster identity; the per-session budget is a browser login budget.
Client address keys are HMAC-derived before storage. By default forwarded headers
are not trusted; behind a proxy, all clients may conservatively share the proxy's
address budget until the trusted hop count is verified.

The state file is temporary security state, not draft/final storage. Worker
recycling retains it; replacing the replica resets it and requires sign-in.
There is no persistent volume or external database requirement for this PR.
**Do not run multiple replicas with separate local files:** use a genuinely
shared session/rate store before scaling to that arrangement. A missing state
store fails closed. Server-side forecast drafts/version retention remain PR 5.

## Request size and resource boundaries

Body limit: 256 KiB; multipart field memory limit: 128 KiB; form parts: 16.
Downloaded report text must be nonblank, contain no null bytes, and fit within
64,000 UTF-8 bytes. These limits apply on the server as well as UI hints.
They do not validate forecast semantics or substitute for the later finalizer.

## Runtime and CI

Python 3.12.14 is pinned. `requirements.txt` locks and hashes 35 runtime packages;
`requirements-dev.txt` includes the same runtime versions plus development tools.
`requirements-dev.in` constrains against the runtime lock to prevent drift.
Regenerate with uv (the capture used uv 0.13.6):

```sh
uv pip compile requirements.in --python-version 3.12 --generate-hashes --no-emit-index-url --no-emit-index-annotation -o requirements.txt
uv pip compile requirements-dev.in --python-version 3.12 --generate-hashes --no-emit-index-url --no-emit-index-annotation -o requirements-dev.txt
```

CI installs hashes, checks dependency consistency, runs targeted Python lint and
all offline tests, checks the frozen geometry locations, and audits the runtime
lock against known Python advisories. GitHub Actions are pinned by commit SHA
and have read-only repository permissions; pull-request code gets no deployment
credentials. Advisory coverage does not certify bundled native libraries or
the absence of undisclosed vulnerabilities.

## Verification

- Fresh isolated Python 3.12.14 environment installed from the hashed development
  lock; runtime/development versions match and dependency consistency passes.
- 28 offline tests pass, including the existing Phase 0 raw-GRIB redecodes and
  new request/security cases. All weather requests in the web suite are mocked.
  A frozen-data assembly smoke case also renders a real PNG with the new stack.
- Regression cases cover NaN/infinite/out-of-range coordinates, bad dates/models,
  unbounded/duplicate input, no-network validation, empty upstream output,
  escaped script text, exact Unicode export, CSRF/expiry/renewal, replayed-cookie
  revocation, password rotation, cross-worker sessions, forwarded-header handling,
  and concurrent rate reservations.
- Two-worker HTTPS Gunicorn smoke check validates real Secure-cookie login,
  repeated authenticated requests across worker recycling, static CSS, UTF-8
  attachment, and logout-cookie replay rejection.
- Runtime advisory scan on 2026-11-10: 35 packages checked, no known findings or
  skipped packages. CI repeats the scan rather than relying on this dated result.

## Staging and rollout

1. Install the locked environment using the pinned Python version. Keep the
   service to one replica. Ensure all workers share a writable private security
   file path and identical password/signing key. Existing sessions will expire
   during migration to the new cookie/store.
2. Use HTTPS. Verify the actual proxy chain before setting
   `AUTOSPOT_TRUST_PROXY_HOPS` (0 by default, supported range 0–2). Do not blindly
   trust arbitrary X-Forwarded-For values. Proxy settings do not change the cookie
   requirement; loopback HTTP needs an explicit factory override for development.
3. Configure readiness to `/healthz`; expect JSON 200 when configuration/state
   are usable, 503 otherwise. Readiness never checks weather providers.
4. Verify the service's actual start command. Railway's known start-command
   override may still use two workers and omit the Procfile's recycling options;
   the controls support either worker count on one replica. No platform setting
   is changed by this PR.
5. In staging, check login/logout, one valid request, invalid input, one upstream
   failure, and the edited download. Inspect request IDs/statuses without logging
   credentials or report bodies. Review the concrete result before production.

Audit ownership: F09, F10, F17 and the foundation part of F20; the UTF-8 export
path also repairs F19 early, and false-success containment addresses part of F01.
Remaining F01 and forecast science/data defects stay assigned to PRs 2–5. No
domain blocking, automatic ceiling/visibility diagnosis, FL interpolation, or
new provider fallback is claimed by this PR. The Phase 0 operational domain
decision remains open for PR 3.

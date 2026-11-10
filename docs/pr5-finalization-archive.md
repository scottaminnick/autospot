# PR 5 — saved-version finalization and consistent archive downloads

Based on merged PR 4 (`c713667`). The request, original model guidance,
structured element review, saved forecast body and approved forecast now live
in one session-owned review workspace. Finalization is an explicit forecaster
action tied to a saved body/version; authentication alone does not approve it.

## Review and approval

The element form rebuilds a saved draft body. The free-text editor can then
complete or adjust that body without changing the original model fields or
silently rewriting the structured element form. Saved text has a version, UTC
preparation time, origin and SHA-256 digest. Request identification is displayed
separately and added to the standalone text: operation, coordinates, full UTC
validity, model/grid/cycle, prepared/issued time, draft ID/version and units.

The operator saves text changes, reviews the saved version and explicitly
approves its location, validity, service responsibility, winds/FL winds,
visibility, weather (including none expected) and sky. A completed AWC service
coordination disposition and a one-line coordination note are required. Pending,
AAWU-redirect and other-office-coordination dispositions remain drafts.
The geometry is still a reference layer; the application does not establish the
spot-service boundary or authorize an exception to the local AAWU procedure.

Finalization rejects unsaved text, unresolved placeholders, unknown required
sky/ceiling, missing prevailing elements, nonreportable visibility, invalid or
out-of-order group times, and unsupported text constructs. It validates the
chosen local syntax, not forecast truth or proof that coordination occurred.
The local spot product keeps its requested 1–72-hour validity; no station/WMO
header or airport 24/30-hour limit is introduced.

A save or element rebuild after approval creates a new draft version and clears
approval. Old version-specific download URLs then fail. Stale tabs cannot save
or approve over a newer revision; the existing SQLite compare-and-swap protects
both actions across workers. Finalization stores the approved body digest,
complete text bytes/digest, source revision, approved version, finalization/issue
time, original-guidance digest, parsed forecast and review attestation.

Existing PR 4 workspaces can still be opened. Their first text save establishes
a versioned document before approval is allowed. The original guidance is kept.
Only the current saved document/approval is retained in the temporary workspace;
this is not an immutable version archive. Already downloaded packages identify
their own approved version and remain independent artifacts.

## Text validation and plain-language FL winds

The supported final body contains:

- Complete `FMDDHHMM` prevailing groups: wind, visibility, optional reviewed
  weather/obscuration, and sky. Omitted weather is an internal reviewed-none
  decision captured by the approval. Visibility ≤6 SM requires a cause code;
  no fog is invented to satisfy that requirement.
- Manual TEMPO/PROB30 groups with partial elements inheriting the attached
  prevailing group. Their full resolved UTC intervals must fit that FM period
  and the request. Crossing an FM change requires separate groups; the element
  formatter already performs that split. Conditional hour 24 resolves to the
  next UTC day; month/year rollover remains explicit.
- An optional requested-FL section using full UTC dates/times and plain language:
  `2026-11-10 14:00Z FL030: from 210 degrees true at 20 kt`, `calm (0 kt)`, or
  `unavailable (reason)`. Each requested FL/native-or-start time appears exactly
  once; ending support rows and unrequested levels are rejected. Unavailable
  optional winds remain missing with a reviewed reason, never numeric defaults.
- Up to eight optional `Remarks:` lines retaining Unicode text.

The initial FM must equal the exact request start. Every FM is strictly earlier
than expiration and increases without duplicates. Each FM supplies complete
prevailing elements; conditional values are checked together with inherited
conditions. Weather/sky normalization reuses PR 4's rules. VV/// may remain in a
draft but cannot finalize as a known required ceiling. The validator is a bounded
local grammar, not a universal TAF parser: unsupported constructs such as LLWS,
BECMG or alternate aloft encodings can be saved as drafts but are not finalized
by this implementation.

Parsed final values are the approved forecast record. Values matching a reviewed
structured element at the same time retain its quality/source; other final-text
entries are marked manual with their prior element as context. A new FM at a
different time is not relabeled as a direct/derived sample from an earlier time.
FL entries identify their original time/level and retain derived or missing
status when appropriate. The raw source arrays and transformation provenance
stay available in the original-guidance part of the record.

## Charts and quality states

The original model chart remains separate and explicitly labeled after text
editing. It carries location, model/cycle, full requested UTC period, units and
native direct/missing/unsupported/invalid counts. It states that ceiling is not
available from this feed. Cloud fraction is not plotted as ceiling height.

A second chart shows the forecaster forecast: saved valid text when available,
approved parsed text after approval, or the independently labeled structured
form when saved text is unresolved. A physical ceiling panel and a categorical
state panel distinguish known ceiling, no ceiling and unknown. FEW/SCT do not
create a ceiling. A known ceiling above 12,000 ft is shown with an above-range
marker and exact numeric height in the accompanying table. Conditional known
ceilings use dashed intervals; all conditional states remain in the text.
Each exported image identifies its own location, model/cycle, full UTC period,
units, source/basis and, for the forecaster plot, draft/version. Missing values
remain gaps. PNGs for representative unknown/no-ceiling/high-ceiling and live
request cases were visually inspected.

## Downloads and archive package

Approved downloads are read from the saved approval snapshot, not arbitrary
posted text. They require the owning authenticated session and the currently
approved version. Text/source digests and saved request/document identification
are checked before export. Issuance time is the recorded approval time; the
preparation time remains separately identified.

The ZIP contains fixed safe entry names:

| File | Content |
|---|---|
| `forecast.txt` | Exact approved UTF-8 bytes, matching the direct text download |
| `provenance.json` | Request, units, timestamps, native quality, original model/derived values, structured review, saved body, parsed approved forecast and attestation |
| `original_model_guidance.png` | Independently identified original model chart |
| `reviewed_forecast.png` | Independently identified approved forecast/ceiling chart |
| `README.txt` | Local NAOC retention instructions and the distinction between download and archive/transmission |
| `manifest.json` | SHA-256 and byte count for every other member; excludes itself |

The package can be placed in the established NAOC folder using the local
procedure. No folder destination was invented, no archive write was made and
no forecast was externally transmitted. ZIP entry names are fixed rather than
built from user text. Auth session IDs, credentials and secret keys are excluded
from the provenance record. Hashes identify content; they are not a digital
signature or a named-person identity assertion.

Draft downloads are explicitly identified as drafts. The legacy `/transmit`
route remains compatible as a draft-only attachment with a `DRAFT_` filename;
it cannot access the approved export/package paths. Free-text changes do not
change original guidance. No new database service, runtime dependency or
retention extension is introduced: PR 4's session ownership, 24-hour expiry,
record/payload/disk quotas and single-host/replica limits remain in effect.

## Validation and limits

131 offline tests pass, including exact UTF-8/ZIP parity, every manifest digest,
request/source corruption checks, stale/other-owner/expired approvals, edit and
rebuild invalidation, legacy reviews, direct text completion, time/month/year
rollover, conditional inheritance, required elements, placeholder rejection,
requested-FL coverage and ceiling-state separation. Existing raw GRIB decoding,
meteorological, request/security and quota tests remain enabled.

All 12 geometry reference checks, lint, dependency consistency and the locked
runtime advisory scan pass. [Local two-worker HTTPS evidence](evidence/pr5-web-check.json)
records real spawned ECMWF acquisition for a six-hour off-hour request with
FL030/450; manual element/TEMPO rebuilding; saved Unicode text; finalization;
approved-text/ZIP byte parity; every package hash; worker recycling; stale-tab
and other-session failures; and unchanged original model PNG after edits.
Generation took 50.320 s locally. Test forecaster entries and coordination
attestation were deliberately synthetic, not operational forecasts or actual
service coordination. These measurements are not Railway guarantees.

Full interactive browser layout verification remains outstanding because
Chromium could not launch in this environment in the preceding work. HTTP,
form/template, PNG decoding and visual chart inspection were used here.
No Railway configuration or production deployment was performed. Representative
operational acceptance, browser/staging checks, performance/resource review,
rollout and rollback remain PR 6. The operational service-boundary decision
remains open.

Source basis: supplied AWC procedure 10-001-003; [Phase 0 product contract](phase0/product-contract.md);
[PR 4 element rules and primary references](pr4-reviewed-spot-forecast.md).

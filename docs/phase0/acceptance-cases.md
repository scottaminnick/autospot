# Implementation acceptance cases

These are expected behaviors for PRs 1–6. They are not claims that the baseline
application passes. Phase 0's offline tests check frozen evidence only.

| Case | Expected result | Owner PR |
|---|---|---|
| Original Bahamas request, 2026-11-09 12Z to 2026-11-10 12Z | 00Z run steps +12…+36, nine native wind samples; +36 is boundary support, not a new FM. Manual visibility/sky and domain review remain explicit. | 2–5 |
| Provider AWS 503/429, same ECMWF run available elsewhere | Bounded retry/failover using matching provider index; same model/cycle; field provenance retained. | 2 |
| Wrong param/level/unit/time; truncated or incorrect byte range | Reject field as invalid with cause, never select first decoded variable. | 2 |
| All fields missing | Failed generation; no approve-as-final report. | 1/2/5 |
| First/middle/last wind sample missing | Absolute UTC positions unchanged, gaps visible, no extrapolation. | 2/3/5 |
| Request at 07Z, bracket 06Z/09Z | Component interpolation explicitly labeled; initial categorical fields require review; 06Z/09Z never relabeled 07Z. | 3 |
| Request crossing month/year and 72-hour validity | Full UTC timestamps remain authoritative; FM day groups resolve unambiguously within interval. | 3/4 |
| +0 index contains +3 temperature-extreme placeholders | Field lookup uses record time and decoded time, not object filename alone. | 2 |
| Gust maximum 11–12 h in +12 file | Interval shown as one hour; no assumption that all fields cover preceding three hours. | 2/4 |
| Synthetic winds 350°/10° at equal speed, midpoint | Interpolate U/V through north, never 180°. Calm retains valid-data state. | 3 |
| Synthetic FL030 target between 1000/925 hPa | Standard-atmosphere target pressure and log-pressure U/V interpolation; never hard-code 925 hPa as FL030. | 3 |
| Synthetic FL450 | Piecewise standard atmosphere across tropopause; correct pressure bracket. | 3 |
| Target/bracket below ground; unknown surface pressure | Explicit unavailable/unknown status; no physical wind fabricated below ground. | 3 |
| Synthetic SKC to OVC025; steady speed with qualifying gust onset/cessation | Change groups retained; clouds and gusts participate in change detection. | 4 |
| FEW010 / SCT020 versus BKN020 | Former states have no ceiling; BKN ceiling 2,000 ft AGL; no-ceiling and unknown remain distinct. | 4/5 |
| Captured low+mid+high exceeds total cover | Do not sum layers or invent bases; support manual sky editing. | 4 |
| Nonzero precipitation type, freezing rain/snow/ice pellets/mix | Validated type-specific mapping or manual completion, never all rain. Capture real nonzero fixtures before accepting diagnoses. | 4 |
| CAPE >300 without independent convection evidence | No automatic TSRA or PROB30. | 4 |
| Fog plus rain | Independent justified phenomena can coexist; visibility alone is not fog diagnosis. | 4 |
| Synthetic visibility .75/1.5/2.5/exact 6 SM | Correct local aviation increments; exactly 6 is not P6; finite/missing validated first. | 4 |
| Coordinate NaN/∞/95N; bad date/model; negative or >72 h | Reject before network or expensive work. | 1 |
| HTML/script text, Unicode label/text, non-ASCII password | Text stays escaped; UTF-8 bytes preserved; invalid login causes no 500. | 1/5 |
| Two users or tabs edit old draft version | Wrong owner/version cannot finalize a different or stale draft. | 1/5 |
| Final text edited after guidance chart generation | Chart labeled original model guidance; final text separately versioned. | 5 |
| AWS/ECMWF outage with partial useful fields | Partial draft explains missing elements; completion permitted only through recorded forecaster edits. | 2/5 |
| Domain: Bahamas, Aleutian example, Anchorage, Honolulu, ±180°, shared edge | Reproduce source geometry results; operational routing gated by the domain decision; no bounding-box shortcut. | 3 |

Weather truth requires forecaster review and independent reference comparisons
in PR 6. A parser test passing cannot establish forecast skill. Numerical
tolerances must follow source packing precision, not arbitrary rounded displays.

---
phase: 06-dashboard-experience
plan: 05
subsystem: web
tags: [template-extraction, packaging, dashboard, svg, xss, ci, fastapi]

requires:
  - phase: 06-dashboard-experience
    provides: "06-02's outcome_distribution + daily series + SQL percentiles; 06-04's /metrics.budget (budget_snapshot on the route)"
  - phase: 05-run-event-persistence-live-feed
    provides: "the live-feed block (EventSource /events, FEED_TYPES, snapshot listener) and its grep tests"
provides:
  - "src/relay/templates/dashboard.html — the whole page as a packaged file, read once at import (D-04)"
  - "main._TEMPLATE_PATH — package-relative load path; no HTML literal survives in main.py"
  - "section#summary (cards + outcome bars), section#charts (cost, latency, gauge), section#feed, table#runs — named seams for 06-06's dialog#drill and 06-07's section#try-it"
  - "One whole-page rendering rule: createElement/createElementNS + textContent, grep-asserted on the served document"
  - "CI docker smoke extended to /dashboard and /metrics against the built image"
affects: [06-06 drill-down panel, 06-07 try-it and polish]

tech-stack:
  added: []
  patterns:
    - "Package data resolved through Path(__file__).parent and pinned by a test that resolves through relay.__file__, never through the repo root"
    - "Marker-delimited JS blocks with block-scoped grep tests; the markers themselves asserted first so no assertion below can be vacuous"
    - "A bar is CSS, not markup: widths set through the style object, never an interpolated HTML string"

key-files:
  created:
    - src/relay/templates/dashboard.html
  modified:
    - src/relay/main.py
    - .github/workflows/ci.yml
    - tests/test_dashboard.py

key-decisions:
  - "The template is read once at import, not per request and not in lifespan: /dashboard is the public landing surface (a syscall per visitor otherwise), nothing here binds to a loop, and an import-time failure is the loud early signal that the file did not make it into the image"
  - "The demo-key substitution stays per request — escape() and the '(not configured)' fallback untouched — so a rotated key is served without a redeploy"
  - "No pyproject.toml and no Dockerfile change: verified empirically by building a wheel and serving /dashboard from the wheel-installed package, not assumed from research"
  - "The markup-sink rule widened from the feed block to the whole document, and the /metrics polling half was rewritten onto DOM APIs rather than left alone — a block-scoped rule cannot cover 06-06 and 06-07, which have not been written yet"
  - "The gauge fraction is spent_today_usd / daily_ceiling_usd read straight off /metrics.budget; the in-flight reservation it inherits is stated in the UI copy rather than removed"
  - "Latency segments are drawn only between adjacent days that both carry a percentile — a line across an idle week would be an invention, and a zero would read as 'every run was instant'"

patterns-established:
  - "_block(html, name) marker extraction in tests/test_dashboard.py, asserting both markers before splitting"
  - "_code_only(block) — absence assertions run over code with // lines stripped, because the comments name the forbidden construct on purpose"

requirements-implemented: [DASH-01, DASH-02, DASH-04]

metrics:
  duration: ~50 min
  completed: 2026-08-12
  tasks: 3
  commits: 3
  tests-added: 8
  suite: 389 passed (from 381)
---

# Phase 6 Plan 05: Dashboard template extraction and page shell — Summary

The dashboard moved out of `main.py` into `src/relay/templates/dashboard.html` (read once at import, key substituted per request), gained aggregate cards, an outcome-distribution bar chart, hand-built inline-SVG daily cost/latency charts and a budget gauge fed by the gate's own arithmetic — with the markup-sink rule widened from the feed block to the whole document and the CI docker smoke extended to catch a template lost from the wheel.

## What was built

**Task 1 — extraction (`710fed7`).** `DASHBOARD_HTML`'s 124-line literal was moved byte-for-byte into `src/relay/templates/dashboard.html` and replaced with:

```python
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")
```

`dashboard()` is unchanged — `escape()`, the `(not configured)` fallback and the per-request `.replace()` all survive. `grep -c "<!doctype html>" src/relay/main.py` → 0; the CLAUDE.md-named anti-pattern is closed, not deepened.

**Task 2 — the shell (`c7468ec`).** Body restructured into `header` / `section#summary` / `section#charts` / `section#feed` / `table#runs`, with HTML comments marking where `section#try-it` (06-07) and `dialog#drill` (06-06) go. The `refresh()` half was rewritten onto `el()`/`clear()` + `createElement` + `textContent` behind `// --- metrics poll (/metrics) — begin/end ---`. `OUTCOME_BUCKETS` is an explicit ordered list of the seven server-side buckets; bars set `fill.style.width`, never a string of markup.

**Task 3 — charts and gauge (`285ef81`).** A `// --- charts (SVG) — begin/end ---` block with an `svg()` helper over `createElementNS`, a daily cost bar chart, a p50/p95 chart, and a nested `// --- budget gauge — begin/end ---` block drawing a 180° arc whose fraction is `m.budget.spent_today_usd / m.budget.daily_ceiling_usd`. All three render inside the existing 5s poll — `grep -c 'fetch("/metrics")'` → 1.

## The packaging risk — verified, not assumed

Research's claim was that no `pyproject.toml` or `Dockerfile` change is needed. Confirmed here three ways rather than trusted:

1. **Wheel built** (`pip wheel . --no-deps`): `zipfile.namelist()` contains `relay/templates/dashboard.html`. No pyproject change.
2. **Wheel installed and served**: the wheel was installed with `--target` into a throwaway directory and the app imported from it (`relay.__file__` under `/site/`). `GET /dashboard` → 200, 6091 bytes, with the demo key substituted; `GET /metrics` → 200 with `budget`, `cost_usd`, `daily`, `last_runs`. This is the closest local proxy for the container that was available — **Docker is not running on this machine**, so the image itself was not built here; CI is where that check lands.
3. **`git diff --stat pyproject.toml Dockerfile`** → empty.

The test pins the *load path*, not the wheel: `_packaged_template()` resolves `Path(relay.__file__).parent / "templates" / "dashboard.html"` and the served body is asserted equal to that file's text with the placeholder replaced. Under an editable install `relay.__file__` is the source tree, so **this test cannot see a hatchling exclusion** — it can only see a load path that has wandered off the package. That gap is exactly what the docker smoke covers.

## CI docker smoke — what it would and would not catch

The job now waits for `/health`, then requires both:

- `curl -sf .../dashboard | grep -q "Relay"` — non-empty and expected content.
- `curl -sf .../metrics | grep -q '"outcome_distribution"'` — likewise.

Either failure dumps `docker logs relay` and exits 1.

**Would catch:** the template excluded from the wheel by a future `.gitignore` rule (import-time `FileNotFoundError` → the container never comes up, or a 500); a `_TEMPLATE_PATH` pointing outside the package; the runtime image's SQLite lacking window functions, which would 500 `/metrics` (`ROW_NUMBER() OVER` in `DAILY_BUCKETS_SQL`); a 200 that served an empty body.

**Would NOT catch:** anything about the *rendered* page — no JS executes, so a broken chart, a NaN scale or an XSS sink is invisible to it; a template present but stale; `/dashboard` regressions that keep the word "Relay" (the grep is deliberately cheap and content-shallow); anything on non-GET or authenticated routes. It is a packaging and boot check, not a UI check.

## Mutation testing — every named mutation run, confirmed red, restored

| # | Mutation | Target test | Result |
|---|----------|-------------|--------|
| A | `_TEMPLATE_PATH` → a path off the package that does not exist | `..._served_from_the_packaged_template` | RED (import-time `FileNotFoundError` — the "loud early signal" behaving as designed) |
| A2 | `_TEMPLATE_PATH` → a repo-root copy that **does** exist and differs slightly | same | RED (`1 failed`) — the stronger form: app boots, page serves, test still catches the wandered path |
| B | Key baked at import (`.read_text().replace(...)`) | `..._substitutes_the_key_per_request` | RED (`1 failed`) |
| C | One card rendered through a markup sink inside the metrics-poll block | `..._never_renders_through_a_markup_sink` | RED (`1 failed`) — **and Phase 5's `..._as_text_never_html` passed**, which is the concrete evidence that the block-scoped rule was insufficient |
| D | `step_limit` dropped from `OUTCOME_BUCKETS` | `..._renders_the_summary_from_metrics` | RED (`1 failed`) |
| E | Empty-state label changed to `"None yet"` | `..._renders_without_a_demo_key` | RED — **and `tests/test_auth.py::..._does_not_render_none` red too** (`2 failed`) |
| F | CDN chart library `<script src>` + `new Chart(tag)` replacing `createElementNS` | `..._are_built_as_inline_svg_without_a_library` | RED (`1 failed`) |
| G | Gauge spend summed from `last_runs` via `reduce(...)` | `..._the_gauge_reads_the_servers_budget_object` | RED (`1 failed`) |
| H | Cost chart's empty branch deleted | `..._charts_have_an_empty_state` | RED (`1 failed`) |

One test defect was found and fixed during this: `assert "last_runs" not in gauge` passed only because the *comment* explaining why the sum is forbidden names `last_runs`. Fixed by adding `_code_only()` (drops `//` lines) so the absence assertion is about code; mutation G was then re-run against the corrected test.

## Assertion strength — stated plainly

**Genuine integration assertions** (execute real code, read a real response):
- `test_dashboard_is_served_from_the_packaged_template` — response body vs. the installed package's file.
- `test_dashboard_substitutes_the_key_per_request` — two requests across a settings change.
- `test_dashboard_renders_without_a_demo_key` — 200 + `"None"` absence on a real response.

**Weak by construction** (grep over served HTML; **there is no DOM in this suite** — no jsdom, no headless browser, so none of the page's JS executes): `..._never_renders_through_a_markup_sink`, `..._renders_the_summary_from_metrics`, `..._are_built_as_inline_svg_without_a_library`, `..._the_gauge_reads_the_servers_budget_object`, `..._charts_have_an_empty_state`. Each says so in its own docstring, and 06-VALIDATION.md already marks these rows known-weak with the real check at 06-07's human checkpoint. The gauge test in particular cannot prove the page and the gate agree — that proof is server-side in 06-04's `test_budget_gauge_matches_...`.

**Local one-off verification, deliberately not committed** (it would make Node a test dependency, which the phase's threat register rules out): the `<script>` was extracted and (a) `node --check`ed — syntax OK — and (b) executed against a ~30-line DOM stub with a realistic `/metrics` payload and an all-zero one. Both render paths completed with 7 cards, 7 bars, gauge labels reading `$0.04 / $5 · $4.96 left today`, and the stub raised on any `NaN` attribute or any `undefined`/`None` text — nothing raised. This is stronger than grep and weaker than a browser; it does not replace the human checkpoint.

## Deviations from Plan

**1. [Rule 1 — Test defect] `last_runs` absence assertion was satisfied by a comment**
- **Found during:** Task 3, first run of the gauge test.
- **Issue:** the gauge block's explanatory comment names `last_runs`, so `assert "last_runs" not in gauge` was asserting about prose, and would have stayed green under a partial mutation that kept the comment.
- **Fix:** added `_code_only(block)` and ran the absence assertions over comment-stripped code; mutation G re-run and confirmed red.
- **Files modified:** `tests/test_dashboard.py`
- **Commit:** `285ef81`

**2. [Rule 3 — Blocking] The live-feed `<ul id="feed">` collided with the new `section#feed`**
- **Found during:** Task 2.
- **Issue:** the restructure wraps the feed in `section#feed`, which would have shadowed the list's id.
- **Fix:** list renamed to `id="feed-list"`, `getElementById("feed-list")` in the feed block, CSS selectors updated. Verified no test greps for the old id (`grep -rn 'id="feed'` across `tests/` outside this plan's file → nothing). Every identifier Phase 5's tests assert (`new EventSource("/events")`, the `snapshot` listener, `FEED_TYPES`, `f.run_uid`, `f.ticket_id`, `EventSource.CLOSED`, `setInterval(refresh, 5000)`) is untouched.
- **Files modified:** `src/relay/templates/dashboard.html`
- **Commit:** `c7468ec`

No architectural deviations, no auth gates, no package installs.

## Seams left for the next waves

- `<!-- section#try-it is added by its own plan (06-07); nothing here builds it. -->` sits after the header, where the RESEARCH sketch places it.
- `<!-- dialog#drill (the run drill-down) is added by its own plan (06-06). -->` sits after `section#recent`.
- `renderRuns()` stamps `row.dataset.uid = r.run_uid` (from 06-04's `last_runs`) with a comment saying nothing reads it yet — 06-06 attaches the opener.
- `el()`/`clear()`/`svg()` live in their own `// --- render helpers ---` block, above both consumers, so a new block does not need its own.
- The whole-page sink test already covers blocks that do not exist yet: any markup sink 06-06 or 06-07 adds reds immediately.

## Verification

- `.venv/bin/python -m pytest -q` → **389 passed** (floor 381; +8).
- `.venv/bin/ruff check src tests` → clean.
- `grep -cE "innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(" src/relay/templates/dashboard.html` → 0.
- `grep -c "None" src/relay/templates/dashboard.html` → 0 (case-sensitive).
- `grep -c 'setInterval(refresh, 5000)' …` → 1; both live-feed markers present.
- `grep -c "createElementNS" …` → 3; `grep -cE '<script src=|https://[^"'"'"']*\.js|cdn\.' …` → 0.
- `grep -c 'fetch("/metrics")' …` → 1; `grep -c spent_today_usd …` → 1.
- `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/evals.yml` → clean (frozen files untouched).
- `git diff --stat pyproject.toml Dockerfile` → empty.

## Known Stubs

None. `section#charts` is fully rendered; the two empty HTML comments are seams for later plans, documented above, and neither renders a placeholder to a visitor.

## Threat Flags

None. No new network surface, auth path, file access pattern or schema change. The one new file read (`_TEMPLATE_PATH`) is package-relative and happens at import, and T-06-19/20/21/22 are all mitigated as the register specifies.

## Self-Check: PASSED

- `src/relay/templates/dashboard.html` — FOUND
- `tests/test_dashboard.py` — FOUND
- `.github/workflows/ci.yml` — FOUND
- Commits `710fed7`, `c7468ec`, `285ef81` — FOUND in `git log`

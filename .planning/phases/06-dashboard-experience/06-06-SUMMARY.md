---
phase: 06-dashboard-experience
plan: 06
subsystem: web
tags: [dashboard, drill-down, dialog, xss, grounding, guardrails, sse]

requires:
  - phase: 06-dashboard-experience
    provides: "06-04's GET /runs/{run_uid} envelope and its four states; 06-03's project_run_detail per-step field list and its server-computed `cited` flag; 06-02's run_uid on /metrics.last_runs"
  - phase: 05-run-event-persistence-live-feed
    provides: "the live-feed block, its per-run grouping on f.run_uid, and the shipped greps that own it"
provides:
  - "dialog#drill — the run drill-down as a native <dialog> on the dashboard page (D-05)"
  - "openDrill(uid) — one fetch of GET /runs/{uid}, rendered into the panel; the only entry point"
  - "runCell(r) — a runs-table row whose id is a control, and plain text when run_uid is null"
  - "renderSteps/renderStepBody/renderChunks/renderRawDetail — the eight-type step renderer with cited-vs-not highlighting"
  - "an 'open trace' control on every live-feed run node, so an in-flight run is drillable before its summary row exists"
affects: [06-07 try-it and polish]

tech-stack:
  added: []
  patterns:
    - "A missing number renders as an em dash from an explicit null/undefined test, never from falsiness — 0 is a real timing"
    - "The panel renders the server's grounding answer and never recomputes the comparison client-side"
    - "A control exists only where its handle does: a null run_uid yields plain text, not a button that opens nothing"

key-files:
  created: []
  modified:
    - src/relay/templates/dashboard.html
    - tests/test_dashboard.py

key-decisions:
  - "The four documented states are rendered as four sentences, not as an empty list: swept prints the SERVER's note (which names the retention window) so a settings change cannot be contradicted by a length hardcoded on the page"
  - "in_flight renders its note AND the steps recorded so far — the route returns them with run:null, and hiding them would make a run in flight look emptier than it is"
  - "Cited-vs-not is rendered as two classes and two labels, both fed by the server's `cited` flag; the page never reaches into the reply's citation list (asserted: `.citations` and `normalise` are absent from the block)"
  - "Demo-only input/result/text/missing_citations go in a collapsed <details> keyed on `!== undefined`, so a redacted run renders with no empty holes"
  - "The drill fetch takes a path parameter and nothing else — no query string, no headers bag — verified by grep AND by executing openDrill against a stub and reading back the URL it requested"

patterns-established:
  - "_code_only(_block(html, name)) for every drill assertion, so a name surviving in a comment cannot make a grep green (06-05's defect, not repeated)"
  - "Dispatch asserted in its written form (`s.type === \"x\"`) rather than by bare type name, which any comment or label map would satisfy"

requirements-implemented: [DASH-03]

metrics:
  duration: ~15 min
  completed: 2026-08-12
  tasks: 3
  commits: 6
  tests-added: 9
  suite: 398 passed (from 389)
---

# Phase 6 Plan 06: The run drill-down panel — Summary

Clicking a run — in the history table or on the live feed while it is still working — opens a native `<dialog>` fed by `GET /runs/{run_uid}`, rendering the ordered trace with its timings, the tools called with their argument keys, the retrieval chunks the reply actually cited distinguished from the ones it merely saw, and the guardrail denials as their enumerated facts; every value written with `textContent`, and nothing on the page asking the server for more than it offered.

## What was built

**Task 1 — the panel, the fetch and the run-level states (`c81cb1c` RED, `cbbfd9c` GREEN).**
`<dialog id="drill">` replaced 06-05's seam comment, with a heading region and a close control. The `// --- drill-down (/runs/{uid}) — begin/end ---` block holds `openDrill(uid)`: it clears, opens the modal, fetches `/runs/` + `encodeURIComponent(uid)`, and renders the envelope (ticket, outcome, model, cost, duration, steps, tokens, created_at) before branching on `status`. `runCell(r)` in the metrics-poll block turns the row's id cell into a control — and into plain text when `run_uid` is null, so a pre-Phase-5 row is un-clickable rather than broken.

The five states, each a sentence rather than an empty box:

| State | HTTP | What the panel says |
|---|---|---|
| `complete` | 200 | the trace |
| `in_flight` | 200, `run: null` | "still working… totals appear when it ends", plus the steps recorded so far |
| `swept` | 200 | the **server's** `note` (it names the window), with a fallback string |
| `unrecorded` | 200 | "ran before step recording existed, or its record was never written" |
| unknown uid | 404 | "no such run — this service never issued that id" |

A non-404 non-OK response and a thrown `fetch` each have their own copy; neither leaves a blank dialog.

**Task 2 — the step renderer (`6bfa8bf` RED, `d7e807d` GREEN).**
One `<ol>`, one row per step: `#seq`, a display label, and the offset from `elapsed_ms`. Then a branch per published type — all eight. `tool_use` shows the tool and its argument **keys** as chips plus `unknown_arg_count`; `tool_result` shows success/failure, `denied_by`, its `duration_ms`, and each tool's own result shape; `guardrail` shows the guard, the action and the expected/supplied ticket-id pair that is the prompt-injection payoff; `notice` its kind, cause and mode; `usage`/`resolution` steps and cost; `error` its `reason`/`status`/`error_type` (never a message — the server does not publish one); `text` its character count. `renderChunks` renders each search_docs hit as `doc · id · score` with `.chunk.cited` / `.chunk.uncited` and two distinct labels, styled differently in the stylesheet so the class is not decorative. `renderRawDetail` puts the demo-only fields in a collapsed `<details>`.

**Task 3 — the feed entry point (`65084c5` RED, `e13ea2c` GREEN).**
`runNode(f)` gained an "open trace" button calling `openDrill(f.run_uid)`. Nothing else in that block moved: its markers, `new EventSource("/events")`, the snapshot listener, `FEED_TYPES`, the grouping lookup and the `EventSource.CLOSED` branch are all asserted by a shipped Phase-5 test, and this was an addition.

## Mutation testing — every named mutation applied, run, confirmed red, restored

Restoration verified with `diff -q` against a pristine copy after each batch.

| # | Mutation | Target test | Result |
|---|---|---|---|
| 1 | Delete the `"swept"` branch | `..._renders_the_run_states` | **RED** |
| 2 | Render a step head through a markup sink | `..._values_as_text_never_html` | **RED** — and the whole-page sink test red too |
| 3 | Delete the `if (!r.run_uid)` branch from `runCell` | `..._runs_table_opens_the_drill_down` | **RED** |
| 4 | Delete the `guardrail` branch from the renderer | `..._renders_every_step_type` | **RED** |
| 5 | Render every chunk identically (one class, one label) | `..._distinguishes_cited_from_retrieved` | **RED** |
| 6 | `dash`/`ms` collapsed to `String(v \|\| 0)` | `..._renders_timings` | **RED** |
| 7 | Drop `if (!raw.length) return;` | `..._renders_demo_fidelity_when_present` | **RED** |
| 8 | Delete the feed's `openDrill` control | `..._live_feed_can_open_a_drill_down` | **RED** |
| 9 | Drop `"text"` from `FEED_TYPES` | same, **and** `test_run_events.py::..._subscribes_to_the_live_feed` | **RED** (both) |
| 10 | Append `?full=1` to openDrill's fetch | `..._never_asks_for_full_fidelity` | **RED** |

No test passed under its own mutation, so none required fixing.

## Assertion strength — stated plainly

**Weak by construction — all nine committed tests.** There is no DOM in this suite: no jsdom, no headless browser, so nothing committed here executes `openDrill`, opens a dialog or renders a step. Every assertion is a grep over the served HTML, scoped to a marker-delimited block via `_block` and run over `_code_only` for absence assertions (06-05 found a comment satisfying an absence grep; that lesson is applied throughout, and the dispatch assertions use the written form `s.type === "x"` rather than a bare type name, which a label map or a comment would satisfy). Each docstring says so. 06-VALIDATION.md already defers the DOM-level proof to 06-07's human checkpoint, and nothing here closes it.

**`test_the_page_never_asks_for_full_fidelity` passed the moment it was written** — the drill-down never had such a parameter. Its entire value is failing the day someone adds one; the actual control is server-side, in 06-04's `test_full_fidelity_is_server_decided`. Its docstring names this rather than dressing it up.

**Local one-off execution, deliberately not committed** (committing it would make Node a test dependency, which the phase's threat register rules out). The `<script>` was extracted and run under a ~50-line DOM stub that **throws** if any node's `textContent` is set to `undefined`, `null`, `NaN` or `None`, or if any attribute stringifies to `NaN`:

- All five envelope paths rendered: `complete` (15 steps covering every type, including a legacy row with `elapsed_ms: null` and `char_count: null`, a `tool_result` with `duration_ms: null`, a `seq` gap, and a clamped `"unknown"` tool), `in_flight` with `run: null`, `swept`, `unrecorded` with `run.duration_ms: null`, and a demo run with `ticket` plus raw fields. Nothing threw; every missing number came out as `—`.
- Chunk counting confirmed **1 cited / 1 uncited** node from a two-hit result — the two states are genuinely built from the flag, not just named in source.
- The demo run produced **3** collapsed raw regions; the public run produced **0** (no empty holes).
- `runCell` produced a `button` for a row with a uid and a `span` for one without.
- The not-ok paths: 404 → "no such run…", 429 → "the trace is unavailable right now (HTTP 429)", a thrown fetch → the connection copy, and `openDrill(null)` issued no request at all. The URLs actually requested were `/runs/<uid>` with **no query string** — which is the no-ask property observed rather than grepped.

This is stronger than grep and weaker than a browser. It does not replace the human checkpoint.

## Deviations from Plan

**None.** No auto-fixes were needed, no blocking issues arose, no architectural decisions came up, and no packages were installed. Two small shaping choices worth recording, neither a departure from the plan's intent:

- The runs-table id cell became `runCell(r)`, a named function, rather than an inline branch — the null-uid branch is the thing a mutation deletes, and a named function makes that grep-assertable as `if (!r.run_uid)` instead of as an anonymous ternary.
- Task 1 shipped a minimal `renderSteps` (seq + type + offset) so the page was whole at that commit; Task 2 replaced it with the full renderer. That is the TDD increment, not a stub left behind.

## Verification

- `.venv/bin/python -m pytest -q` → **398 passed** (floor 389; +9).
- `.venv/bin/ruff check src tests` → All checks passed.
- `.venv/bin/python -m pytest tests/test_dashboard.py tests/test_run_events.py -q` → 98 passed.
- `grep -c "openDrill" src/relay/templates/dashboard.html` → **3** (definition, table wiring, feed wiring).
- `grep -cE "innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(" …` → **0**.
- `grep -c "None" …` → **0** (case-sensitive; `tests/test_auth.py`'s whole-document check green).
- `grep -cE "full=|fidelity=|X-Demo|raw=1" …` → **0**.
- 06-07's `<!-- section#try-it … -->` seam → present and untouched.
- `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/evals.yml` → clean.
- `.planning/STATE.md` / `.planning/ROADMAP.md` → untouched; the orchestrator owns them.

## Known Stubs

None. Every branch renders real content from the route's own fields. The only intentionally empty region is the raw `<details>`, which is not built at all when the server sent nothing for it.

## Threat Flags

None. No new network surface, auth path, file access or schema change — the panel consumes an existing public route. T-06-23 (XSS into the widest model-influenced surface) is mitigated as the register specifies: `textContent` via `el()` for every value, `createElement` for every node, grep-asserted on the drill block and again on the whole document, with mutation 2 confirming both. T-06-24 is mitigated by absence, mutation 10, and the observed request URL.

## Handoff to 06-07

- `openDrill(uid)` is a plain function on the page: Try-it can call it directly with the `X-Relay-Run-Uid` header off its own `POST /process` response, and it will be a demo-origin run, so the panel badges it and shows the raw fields.
- The dialog's ids are `drill`, `drill-title`, `drill-envelope`, `drill-steps`, `drill-close`; the shared helpers `dash(v)` and `ms(v)` live in the drill block and are safe to reuse.
- New CSS classes: `.drill-open`, `.drill-facts`, `.badge`, `.drill-ticket`, `.steps/.step/.step-head`, `.chips/.chip`, `.chunks/.chunk.cited/.chunk.uncited`, `.raw`.
- `<!-- section#try-it … -->` is where 06-05 left it, above `section#summary`.
- The whole-page sink test and the "None" check now cover this block too — a Try-it form that renders the visitor's own text through a sink reds immediately.

## Self-Check: PASSED

- `src/relay/templates/dashboard.html` — FOUND
- `tests/test_dashboard.py` — FOUND
- `.planning/phases/06-dashboard-experience/06-06-SUMMARY.md` — FOUND
- Commits `c81cb1c`, `cbbfd9c`, `6bfa8bf`, `d7e807d`, `65084c5`, `e13ea2c` — all FOUND in `git log`

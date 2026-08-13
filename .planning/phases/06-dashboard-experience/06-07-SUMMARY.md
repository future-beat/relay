---
phase: 06-dashboard-experience
plan: 07
subsystem: web
tags: [dashboard, try-it, sse, fetch, refusals, rate-limit, budget, xss, demo]

requires:
  - phase: 06-dashboard-experience
    provides: "06-05's page shell, el()/clear()/svg() and the whole-page markup-sink rule; 06-06's openDrill(uid) and the runNodes feed map; 06-04's X-Relay-Run-Uid response header and tickets.origin-driven fidelity"
  - phase: 01-security-perimeter
    provides: "the published demo key, demo_create_limit / demo_process_limit, and the daily spend ceiling whose 429/503 bodies this page renders verbatim"
provides:
  - "section#try-it — three prefilled, editable examples submitted with the published demo key (DASH-05, D-06)"
  - "streamRun(ticketId) — fetch + a frame-buffered SSE parse of POST /process, because EventSource can neither POST nor set X-API-Key"
  - "badgeOwnRun(uid) — the submitter's run badged in the ambient (redacted) feed, keyed on the server's own X-Relay-Run-Uid (T-06-30)"
  - "offerTheTrace(uid) — 'see the full trace', deep-linking into 06-06's full-fidelity drill-down"
  - "renderRefusal(status, detail) — one renderer for 429 / 503 / auth refusals, in the server's own copy (D-08)"
affects: []

tech-stack:
  added: []
  patterns:
    - "A streamed SSE read on a POST route is fetch + getReader() + a \\n\\n buffer; EventSource stays on the keyless GET feed"
    - "A refusal renders the SERVER's note and the SERVER's reset instant — the browser derives neither"
    - "Both HTTPException detail shapes are handled at one site: `typeof detail === \"string\" ? detail : detail.note`"

key-files:
  created: []
  modified:
    - src/relay/templates/dashboard.html
    - tests/test_dashboard.py

key-decisions:
  - "The three examples are asserted against evals/golden.jsonl itself rather than against literals repeated in the test — a page carrying its own paraphrase would be demoing something the eval suite does not measure"
  - "No address field, and the test asserts the absence over the SECTION MARKUP only: the script names customer_email in the request body it builds, so a whole-document grep would let the script satisfy the markup's assertion"
  - "The visitor's run stays IN the public feed and is badged rather than suppressed — the same run rendered in full above and redacted below is the security story as an interface"
  - "The badge is keyed on runNodes.get(uid) from the feed block rather than a CSS attribute selector built from the uid, and the try-it block is placed above the feed block so nothing but call order couples them"
  - "Task 1 shipped a submitTryIt that created the ticket and stopped there; task 2 replaced it with the streaming path. Each commit leaves a page that works — that is the TDD increment, not a stub"

patterns-established:
  - "_section(html, id) — markup-only extraction, the counterpart to _block for script-only assertions"
  - "Counting pinned literals in their written form (`customer_email: \"`) so a dynamic use of the same key cannot pad the count"

requirements-implemented: [DASH-05]

metrics:
  duration: ~35 min
  completed: 2026-08-12
  tasks: 2 of 3 (task 3 is the phase's human checkpoint — NOT performed)
  commits: 4
  tests-added: 7
  suite: 405 passed (from 398)
---

# Phase 6 Plan 07: The Try-it form — Summary

A visitor picks one of three grounded examples, edits it if they like, and sends it with the key published on the page; the run streams onto the page as it happens through `fetch` and a frame-buffered SSE parse, the same run appears in the ambient feed badged as theirs and redacted, "see the full trace" opens its full-fidelity drill-down — and when the demo is out of allowance, the page says so in the service's own words, with the service's own reset time.

## What was built

**Task 1 — the form and its three examples (`9ce8126` RED, `c5367c3` GREEN).**
`section#try-it` replaced 06-05's seam comment, above `section#summary`, because the phase goal is "understand it in under a minute — and run it themselves". It carries the copy that makes the offer honest (runs for real against Claude, the trace is publicly readable for 30 days, 5 runs/hour per IP and a daily ceiling), three example chips, an editable `input#try-subject` and `textarea#try-body`, and a status/stream region.

`TRY_EXAMPLES` holds the three golden cases verbatim — `refund-monthly` (billing → escalation), `rate-limits-pro` (technical → reply citing `api.md#rate-limits`), `password-reset` (how-to → reply citing `account.md#password-reset`). Each pins a seeded customer address (`mia@datalane.ai`, `liam@brightco.io`, `noah@freetier.dev`) and **there is no address field**: a demo-origin run's drill-down is full fidelity and publicly retained, so a typed address would let one visitor publish another person's identifier into a 30-day world-readable record with this form as the mechanism (T-06-27). Selecting a chip *fills* the fields; what is submitted is what the fields hold at submit time.

`TRY_CONFIGURED = DEMO_KEY !== "(not configured)"` guards setup: on a deployment with no keys the chips and fields still render, the controls are disabled, and the state line says which of the two it is. The whole page is one script — a throw during setup here would take the feed, the charts and the drill-down with it.

**Task 2 — submit, stream, badge, deep-link, refuse (`02f7657` RED, `17f45c4` GREEN).**
`submitTryIt()` makes the two calls `scripts/demo.sh` makes — `POST /tickets` then `POST /tickets/{id}/process` — both with `"X-API-Key": DEMO_KEY`, and both branch on `res.ok` before reading a body (`/tickets` has its own allowance, so a refusal can land on either).

`streamRun(ticketId)` is the research sketch as written: `fetch` with `method: "POST"`, then `res.headers.get("X-Relay-Run-Uid")`, then `res.body.getReader()` decoded through `TextDecoder(…, {stream: true})` into a buffer that is cut on `"\n\n"` with the partial tail retained. Each frame's `event:` and `data:` lines are parsed and rendered as one text line per step, `textContent` throughout — this stream is the **owner-facing full-fidelity** one, so it carries the model's prose and raw tool names and is treated as untrusted data accordingly.

Once the uid is known: `offerTheTrace(uid)` adds a "see the full trace" control calling 06-06's `openDrill(uid)` (full fidelity, because the ticket was *created* with the demo key), and `badgeOwnRun(uid)` marks the ambient feed's node for that run — `runNodes.get(uid)` from the feed block, so the page needs no selector built from a server string. The run is **left in** the public feed: the same run rendered in full above and redacted below is exactly the disclosure boundary D-02 draws, made visible.

`renderRefusal(status, detail)` is one renderer for every refusal the path can meet — 429 rate limit (either route), 503 daily budget, 503 shutting-down, an auth refusal's plain-string detail, and a detail that could not be parsed at all. It renders `typeof detail === "string" ? detail : detail.note`, prints `detail.resets_at` **verbatim** and `retry_after_seconds` when present, and styles the whole thing as `.refusal` — a designed state with its own styling, not the error path. In-stream `event: error` frames (a drain, a persistence failure) take the same box with the server's own note.

## Mutation testing — every named mutation applied, run, confirmed red, restored

Restored via a pristine copy (`diff -q`) for task 1 and `git checkout --` for task 2; `git status --short` clean after each batch.

| # | Mutation | Target test | Result |
|---|---|---|---|
| 1 | Delete the how-to example from `TRY_EXAMPLES` | `..._offers_three_editable_examples` | **RED** |
| 2 | Fill the example into `textContent` instead of `.value` (D-06's "editable" quietly gone) | same | **RED** |
| 3 | Add `<input id="try-email" type="email">` and read it in the submit body | `..._exposes_no_email_field` | **RED** |
| 4 | Delete the `(not configured)` guard and wire submit unconditionally | `..._renders_disabled_without_a_demo_key` | **RED** |
| 5 | `new EventSource("/tickets/{id}/process")` in place of the fetch | `..._streams_with_fetch_not_eventsource` | **RED** |
| 6 | `(await res.text()).split("\n\n")` once at the end instead of the buffer loop | same | **RED** |
| 7 | Drop the `X-Relay-Run-Uid` read (`const uid = null`) | `..._deep_links_its_own_run` | **RED** |
| 8 | Badge from `crypto.randomUUID()` instead of the server's uid (T-06-30) | same | **RED** |
| 9 | `new Date().setUTCHours(24,0,0,0).toISOString()` instead of `detail.resets_at` | `..._renders_refusals_as_designed_states` | **RED** |
| 10 | `alert(...)` + `console.error` + the ordinary step class for a refusal | same | **RED** |
| 11 | **Server side:** drop `note` from the daily-budget refusal detail | `test_refusals_render_as_product_copy` | **RED** (`KeyError: 'note'`) |

No test passed under its own mutation, so none required fixing.

One test defect was caught during task 1 and fixed before the GREEN commit: `code.count("customer_email:") == 3` counted **four** — the request body's `customer_email: tryEmail` padded it. Counting the pinned literal form (`customer_email: "`) instead makes the assertion about the examples and only the examples.

## Assertion strength — stated plainly

**Genuine integration assertions (2 of 7).**
- `test_try_it_renders_disabled_without_a_demo_key` — a real response with `demo_key` unset: 200, `(not configured)` present, the section still present, and the case-sensitive `"None"` absent.
- `test_refusals_render_as_product_copy` — drives the real routes through the `client` fixture with the demo key: a second demo `POST /tickets` under a patched `demo_create_limit`, a second `POST /process` under a patched `demo_process_limit`, and a `POST /process` after `record_run` puts recorded spend at the ceiling. Asserts `error`, a non-empty `note`, `retry_after_seconds`, and that `resets_at` parses as a timezone-aware ISO instant.
  **STATED PLAINLY: this one passed the moment it was written** — the server already sends those fields, and 06-01's own tests assert several of them. It is a *contract pin* for the page, not a discovery: its whole value is failing the day a rename empties the refusal box in production, on the exact path a visitor hits when the demo is doing its job. Mutation 11 is what proves it is not vacuous.

**Weak by construction (5 of 7) — labelled in every docstring.** There is no DOM in this suite: no jsdom, no headless browser, nothing executes a line of the page's JS. `..._offers_three_editable_examples`, `..._exposes_no_email_field`, `..._streams_with_fetch_not_eventsource`, `..._deep_links_its_own_run` and `..._renders_refusals_as_designed_states` are all greps over the served document, scoped with `_block` (script) or the new `_section` (markup) and run through `_code_only` for every absence assertion — 06-05's defect (a comment satisfying an absence grep) is not repeated. They are regression guards on what shipped. **The DOM-level proof is precisely what task 3's human checkpoint exists to supply, and nothing here closes it.**

One assertion is stronger than it looks and worth naming: `_section` exists because the script legitimately contains `customer_email`, so a whole-document "no address field" grep would have been satisfied by the very code that sends the request. Splitting markup from script is what makes that assertion mean anything.

**Local one-off execution, deliberately not committed** (committing it would make Node a test dependency, which the phase's threat register rules out). The `<script>` was extracted, `node --check`ed, and executed under a ~90-line DOM stub that **throws** if any `textContent` is set to `undefined`, `null`, `NaN` or anything matching `\bNone\b`, or if any attribute stringifies to `NaN`:

- A full run streamed through a fake `fetch` whose body is chunked at **37 bytes**, which splits every one of the eight frames — most of them twice. All 7 renderable frames (`done` renders nothing) came out in order, correctly reassembled: `usage`, `text`, `tool_use`, `tool_result`, `guardrail`, `notice`, `resolution`. Nothing threw.
- `X-Relay-Run-Uid` was read, the feed node for that uid was badged exactly once (`dataset.mine`, plus the "your run — redacted here, in full above" line), and clicking "see the full trace" issued **`GET /runs/a1b2…`** — the deep link observed rather than grepped.
- Five refusal shapes rendered: 429 with `retry_after_seconds`, 503 budget (with `resets_at` printed **verbatim**, `2026-08-13T00:00:00+00:00`), 503 shutting-down, a 401 with a plain-string detail, and a detail that failed to parse (falls back to `HTTP 503`). No `Invalid Date`, `NaN` or `undefined` in any of them.
- A live 429 on the real submit path rendered the server's note, re-enabled the button, and offered **no** trace control for a run that never started.
- Chip selection filled `input.value` / `textarea.value` and displayed the pinned seeded customer.

**The harness was itself checked for vacuity:** replacing the frame terminator `"\n\n"` with `"\n"` turned **6** of its checks red; restored, all 29 pass. This is stronger than grep and weaker than a browser. It does not replace the human checkpoint.

## Deviations from Plan

**None.** No auto-fixes to product code were needed, no blocking issues arose, no architectural decisions came up, no packages were installed, and no authentication gate was hit. Two shaping choices worth recording, neither a departure from the plan's intent:

- The plan's acceptance criterion says `grep -c "X-Relay-Run-Uid"` returns **>= 1**; it returns exactly 1 — the header is read once, in `streamRun`, and the uid is threaded from there. A second read site would be a second definition of "which run is mine".
- The refusal renderer also handles the two shapes the plan did not enumerate: an in-stream `event: error` (a drain or a persistence failure mid-run, which arrives on a 200 and so cannot come through `res.ok`) and a body that fails to parse as JSON at all. Both are Rule-2 shaped — a refusal path that renders a blank box is the exact failure D-08 exists to prevent — but neither required new copy: both print the server's own `note` where there is one.

## Verification

- `.venv/bin/python -m pytest -q` → **405 passed** (floor 398; +7). `.venv/bin/ruff check src tests` → All checks passed.
- `.venv/bin/python -m pytest tests/test_dashboard.py -q` → 52 passed; `tests/test_auth.py tests/test_run_events.py` → 81 passed.
- `grep -c "new EventSource" src/relay/templates/dashboard.html` → **1** (the live feed's, and only that one).
- `grep -c "getReader()" …` → 1; `grep -c "X-Relay-Run-Uid" …` → 1.
- `grep -cE "innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(" …` → **0** — the page still has zero markup sinks.
- `grep -c "None" …` → **0** (case-sensitive); `tests/test_auth.py`'s whole-document check green.
- `grep -cE "full=|fidelity=|X-Demo" …` → **0** — no fidelity switch reachable from the page.
- `git diff --stat HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/evals.yml` → **empty**, and `git diff --stat main...HEAD` for the same three paths → **empty**. The frozen surfaces stayed frozen across the whole branch; `evals/golden.jsonl` was read as data only.
- No test performs a real Anthropic or Voyage call: the two 429 paths are raised by the perimeter before any route body runs (the ticket id used does not exist), the 503 is raised from `record_run`-recorded spend, `settings.voyage_api_key` is pinned to `None` in the new integration test, and conftest's autouse `_no_outbound_http` guard covers the rest.
- `.planning/STATE.md` / `.planning/ROADMAP.md` → **untouched**; the orchestrator owns them.

## Known Stubs

None. Every branch renders real content. The only empty regions are `#try-actions` and `#try-stream` before a submission, which hold nothing by design rather than a placeholder.

## Threat Flags

None — no new network surface, auth path, file access pattern or schema change. This plan adds a *caller* of two existing routes, not an exemption from anything. Register dispositions as implemented:

| Threat | How it landed |
|---|---|
| T-06-27 (visitor types real personal data) | No address field at all; the address is pinned per example to a seeded customer. Explicit copy that the run is real and its trace is publicly readable for 30 days. Mutation 3 pins it |
| T-06-28 (cost / DoS: paid runs one click away) | Perimeter unchanged — the form presents the published key and takes the same 5/hour, 20/hour and daily-ceiling refusals as curl. Nothing retries: a refusal renders and stops |
| T-06-29 (XSS through refusal copy and streamed data) | `textContent` via `el()` for every value including `detail.note` and the model's own prose; the whole-page sink grep covers this block, and it is 0 |
| T-06-30 (a visitor's run confused with someone else's) | The badge is keyed on the `X-Relay-Run-Uid` the server returned to *this* submitter; mutation 8 (a client-minted id) is red |
| T-06-31 (full-fidelity trace of a demo run) | Accepted per D-02 — the visitor authored the ticket and the pinned examples carry no real PII |

## Phase 6 awaits its human checkpoint — Task 3 (NOT performed)

Everything automatable in this phase is green: **405 tests**, ruff clean, the whole-page markup-sink and `"None"` greps, the leak test and its demo inverse, and a CI docker smoke that curls `/dashboard` and `/metrics` in the built image. What no check in this repo can see is whether a **browser renders any of it** — there is no DOM in the suite. That is what this checkpoint is for, and it was deliberately not attempted here.

**Start the app:**

```bash
.venv/bin/python -m uvicorn relay.main:app --reload
```

Then open <http://127.0.0.1:8000/dashboard>. `RELAY_DEMO_KEY` and `ANTHROPIC_API_KEY` must be set in `.env` for steps 4-6 (without a demo key the form renders disabled on purpose — worth a five-second look, but it is not the check).

1. **The under-a-minute read.** Without scrolling into the weeds: can you tell what this system costs, how well it does, and what it just did? That is the phase goal's literal bar.
2. **The drill-down.** Click a run id in *Recent runs*. Steps are ordered and timed, retrieved chunks show their scores, and the chunks the reply actually cited are visibly distinguished from the ones it merely saw.
3. **Try it, for real.** Click the **billing** chip (`Refund for yesterday's charge`) and press *send it*. Expect: the run streams onto the page line by line as it happens — not all at once at the end — and it ends in an escalation.
4. **Both views of one run, at once.** While it runs, the same run appears in the *Live feed* below, badged **"your run — redacted here, in full above"**. Compare the two: the feed shows tool names and outcomes; the stream above shows the model's prose. That difference is the point.
5. **The deep link.** Press **see the full trace**. It opens the drill-down for *your* run at full fidelity — raw tool inputs and outputs, your ticket's subject and body — and **not** any customer's email address.
6. **The fidelity boundary.** Now open the drill-down of a run created with the **owner** key (any run in the table you did not submit from the form, or one created with `curl -H "X-API-Key: $RELAY_API_KEY" …`). Its trace is redacted: tool names, argument keys, doc ids and scores, guard denials — no raw text. The difference is the *ticket's origin*, not who is looking.
7. **A refusal reads as a feature.** Either submit six runs within the hour, or restart with `RELAY_DEMO_PROCESS_LIMIT=1/hour` and submit twice. The second submission must render a *designed state* — the server's own note, in an amber box, with a reset time — and never an error toast or a browser alert. For the budget shape instead, restart with `RELAY_MAX_DAILY_COST_USD=0.0001` after at least one recorded run: expect **"the demo has spent its budget for today"** plus `resets at <ISO instant>`.
8. **The feed heals itself.** Leave the page idle past the feed's idle ceiling. The status line should read `live · reconnecting` and then `live` again on its own — never a fault.

**Resume signal:** reply **"approved"**, or describe what reads wrong — layout, copy, or any state that renders as a fault when it is a feature.

## Self-Check: PASSED

- `src/relay/templates/dashboard.html` — FOUND
- `tests/test_dashboard.py` — FOUND
- `.planning/phases/06-dashboard-experience/06-07-SUMMARY.md` — FOUND
- Commits `9ce8126`, `c5367c3`, `02f7657`, `17f45c4` — all FOUND in `git log`

---

## Checkpoint result (recorded 2026-08-13)

**Partially confirmed by the user.** First pass was run without `RELAY_DEMO_KEY` set, so the
form rendered read-only and steps 3-6 did not execute; the static render (exported to PDF) did
confirm layout, copy, the cards, the outcome distribution, the cost chart, the server-read gauge
and the absence of any stringified missing value in the document.

That pass also surfaced the sparse-data finding closed by the `dashboard-sparse-states` quick
task: the latency chart and the gauge were rendering correctly but read as broken whenever the
data was sparse — which on a scale-to-zero demo is the common case.

The user then set the key, ran the agent, and reported it "working properly."

**Still not independently evidenced in this repo:** step 5 (the `X-Relay-Run-Uid` deep link
opening the submitter's own run at full fidelity) and step 6 (the demo-vs-owner fidelity
contrast). Step 6 is the phase's central disclosure control. It is pinned server-side by
`test_run_detail_never_leaks_a_non_demo_runs_content`, its demo inverse, and the NULL-origin
route test — so the control itself is proven; what remains unwitnessed is only that the browser
presents the difference as intended.

---
phase: 06-dashboard-experience
verified: 2026-08-14T07:05:00Z
status: gaps_found
score: 9/12 must-haves verified
behavior_unverified: 2
overrides_applied: 0
gaps:
  - truth: "The demo drill-down does not disclose a tool's output about anyone else (CR-01's stated rule, project_run_detail docstring)"
    status: failed
    reason: >-
      CR-01's fix closed the raw-payload vector (_DEMO_RAW_TOOLS) and the email-literal
      vector (withheld). The MODEL PROSE vector is still open. On a demo-origin run the
      drill-down publishes `text` steps and `create_escalation.reason` / `send_reply.body`
      raw, masked only for the ticket's own `customer_email`. lookup_customer's result —
      the customer row plus up to ten of that address's ticket subjects — is restated by
      the model into exactly those fields and republished verbatim on a keyless route.
      Demonstrated end to end with zero credentials: GET /metrics -> harvest every
      `run_uid` (restored to _PUBLIC_RUN_COLUMNS this phase) -> GET /runs/{uid} ->
      another visitor's ticket subject, plus the looked-up customer's name and plan.
      The system prompt actively drives this ("Look up the customer ... so you know their
      plan and history", "Address the customer by name").
    artifacts:
      - path: "src/relay/events.py"
        issue: >-
          project_run_detail: full_fidelity publishes step["text"] (L540-543),
          create_escalation/send_reply raw `input` (L553-557) and raw `result` (L589-596).
          `withheld` (L285-310) masks only literals the route passes.
      - path: "src/relay/main.py"
        issue: >-
          run_detail L613-615 builds `withheld` from `ticket["customer_email"]` alone. The
          customer NAME, PLAN and the `recent_tickets` SUBJECTS that lookup_customer
          returned are never added, so nothing keeps them out of the model's prose.
      - path: "src/relay/tools.py"
        issue: >-
          lookup_customer L34-45 returns `dict(row)` (SELECT *) plus the last 10 ticket
          subjects for that address, regardless of who filed them.
    missing:
      - "Withhold the looked-up customer's values (name, plan, and each recent_tickets subject), not just the ticket's customer_email — i.e. build `withheld` from the run's own lookup_customer RESULT, not from the tickets row."
      - "Or: drop `text` / raw write-tool `input` from the demo branch whenever the run called lookup_customer."
      - "Or: scope `recent_tickets` in lookup_customer to the ticket's own origin tier."
  - truth: "The Try-it form's interaction is guarded against silent removal (DASH-05 / SC-4)"
    status: partial
    reason: >-
      Three deletions each leave the suite at 417 green: the send-button binding
      (`trySend.addEventListener("click", submitTryIt)`), the example-chip binding
      (`chip.addEventListener("click", () => chooseExample(i))`), and the deep-link call
      site (`if (uid) offerTheTrace(uid);`). The grep tests assert token PRESENCE
      (`openDrill(uid)` survives inside offerTheTrace's body even when nothing calls it),
      not the call chain. The deep link is additionally the one checkpoint step
      (06-07 step 5) never witnessed by the user, and the Node DOM stub that did observe
      it was deliberately not committed — so that capability rests on nothing
      reproducible in this repo.
    artifacts:
      - path: "tests/test_dashboard.py"
        issue: "test_try_it_deep_links_its_own_run asserts 'openDrill(uid)' in code — satisfied by a dead definition."
      - path: "tests/test_dashboard.py"
        issue: "No test references trySend.addEventListener or chooseExample's binding."
    missing:
      - "Assert the call sites, not the tokens: `offerTheTrace(uid)` reachable from streamRun, `submitTryIt` bound to trySend, `chooseExample` bound to a chip."
  - truth: "Every frame field the page renders is routed through the dash() placeholder (WR-10)"
    status: partial
    reason: >-
      WR-10 was fixed for the two feed describers and renderChunks. The drill panel's own
      8-branch describer `renderStepBody` interpolates ~10 raw `s.` fields bare
      ("-> " + s.tool, "guard " + s.guard + " -> " + s.action, "error · " + s.reason,
      "resolved via " + s.via, "notice · " + s.kind + " on " + s.tool + " · " + s.cause,
      "reply " + s.reply_id + " · " + s.status, "category " + s.category), contradicting
      the review's premise that "the drill-down has dash() and uses it consistently".
      The regression guard cannot see it: _BARE_FRAME_FIELD matches only `f.`/`d.`/`r.`
      prefixes adjacent to a `+`, and the test scans four renderers that exclude
      renderStepBody. Two mutations proved the hole — unwrapping `dash(s.char_count)` in
      renderStepBody, and unwrapping `dash(r.score)` to a bare el() text argument inside
      the scanned renderChunks — both leave the suite 417 green.
    artifacts:
      - path: "src/relay/templates/dashboard.html"
        issue: "renderStepBody (~L660-712) interpolates raw step fields without dash()."
      - path: "tests/test_dashboard.py"
        issue: "_BARE_FRAME_FIELD (L2516) covers neither `s.` fields nor bare el() text arguments."
    missing:
      - "Add renderStepBody to the scanned renderers and extend the prefix class to `s.`."
      - "Catch a bare frame field passed as an el() text argument, not only one adjacent to `+`."
behavior_unverified_items:
  - truth: "A visitor can open their own submitted run's full-fidelity drill-down from the Try-it panel (the X-Relay-Run-Uid deep link)"
    test: "Set RELAY_DEMO_KEY, open /dashboard, submit an example, wait for the run to finish, click 'see the full trace'."
    expected: "The dialog opens on THAT run, shows demo=true content (raw tool input, the model's prose, the reply body), and the network tab shows GET /runs/{the uid the response header carried}."
    why_human: "No DOM in the suite; the only test guarding it is satisfied by a dead function definition (see gaps). Checkpoint step 5 was never witnessed."
  - truth: "The demo-vs-owner fidelity contrast is visible in the browser (SC-2 / D-02)"
    test: "Open one demo-origin run's drill-down and one owner-origin run's drill-down side by side."
    expected: "The demo panel shows the 'raw — your own run' <details> block; the owner panel shows arg keys and shapes only, with no raw section and no empty holes where it would have been."
    why_human: "Checkpoint step 6, never witnessed. The server-side control IS proven by three mutation-bound tests; what is unwitnessed is only the presentation."
human_verification:
  - test: "Set RELAY_DEMO_KEY, open /dashboard, submit an example, click 'see the full trace'."
    expected: "The dialog opens on that run at full fidelity; the request is GET /runs/{X-Relay-Run-Uid}."
    why_human: "Checkpoint step 5, never witnessed; the guarding test is satisfied by a dead definition."
  - test: "Compare a demo-origin and an owner-origin drill-down in the browser."
    expected: "Raw block present on the demo one, absent (and leaving no hole) on the owner one."
    why_human: "Checkpoint step 6, never witnessed."
---

# Phase 6: Dashboard Experience — Verification Report

**Phase Goal:** A visitor can understand the system's cost, quality, and behavior in under a minute — and run it themselves.
**Verified:** 2026-08-14T07:05:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification
**Baseline confirmed independently:** `pytest -q` → **417 passed**; `ruff check src tests` → **All checks passed**. Tree restored to `ed3afb2` at finish (`git status --short` empty).

---

## Method

Every verdict below rests on evidence I gathered myself: reading the shipped source, running the routes end to end against a real `TestClient` with `settings.voyage_api_key = None` and `tests/helpers.py` fakes (no paid call made), and **mutation-testing 27 named controls** — each mutation applied to source, full suite run, result recorded, source restored byte-for-byte. SUMMARY claims are cited only where they are the only record of something (the uncommitted DOM stub, the checkpoint result).

---

## Goal Achievement

### Observable Truths

| # | Truth (SC / requirement) | Status | Evidence |
|---|---|---|---|
| 1 | **SC-1 / DASH-02** — aggregate cards + outcome distribution computed by SQL aggregation | VERIFIED | Live `/metrics`: `outcome_distribution` returns all 7 buckets zero-filled from `OUTCOME_DISTRIBUTION_SQL`'s `GROUP BY`; totals from `TOTALS_SQL`. Mutations bind: reordering the CASE arms reds `test_outcome_distribution_buckets_every_outcome`; unbounding `LAST_RUNS_SQL` reds `test_last_runs_is_bounded_and_newest_first`. |
| 2 | **SC-2 / DASH-03** — drill-down shows tool inputs/outputs, timings, retrieval chunks with scores + cited-vs-not, guardrail denials (server side) | VERIFIED (as amended by D-01/D-02) | Live `/runs/{uid}` on a real run returned 13 ordered steps carrying `arg_keys`/`input`, `result`/`reply_id`/`status`, `elapsed_ms` **and** `duration_ms`, and `results: [{id: billing.md#refunds, score: 5.49, cited: True}]`. Guardrail facts pinned by the leak test (`guard == "citation"`, `missing_count == 1`). **Amendment:** on the public (non-demo) branch "inputs/outputs" are argument KEY names and result shapes, never values — D-01, recorded in 06-CONTEXT. |
| 3 | **DASH-03** — timings are real (`elapsed_ms`), not `created_at` at second resolution | VERIFIED | `RunRecorder._t0 = time.monotonic()` per run; stamped in `_insert_event`, so both the read path and the in-transaction write path are covered by construction. Three mutations each red exactly one test: nulling the stamp, using an absolute origin, zeroing `duration_for`. `created_at` is never published (asserted by the projector's own field list). |
| 4 | **DASH-03** — cited-vs-not uses the citation guard's own accept-set | VERIFIED | `project_run_detail` calls `retrieval.normalise_citation` and licenses `doc`, `id` and every `anchor` — the same set `agent.py` builds. Forcing `cited = True` reds three tests including `test_cited_vs_not_matches_the_citation_guards_accept_set`. |
| 5 | **SC-3 / DASH-04** — inline SVG charts + budget gauge, no CDN, no build step | VERIFIED | Served page has **0** `<script src=`, `<link href=` or CDN hosts; charts built via `createElementNS(SVGNS, …)`. `/metrics.budget` is `budget_snapshot(conn)` verbatim — replacing it with a hand-rolled dict reds `test_budget_gauge_matches_the_gate`. Live: `{spent_today_usd, daily_ceiling_usd, remaining_usd, exhausted, resets_at}`. |
| 6 | **DASH-04 / WR-09** — the p50 card and the p50 chart are one statistic over one population | VERIFIED | `WINDOW_PERCENTILE_SQL` and `DAILY_BUCKETS_SQL` share `_window_offset()` and a character-identical half-up rank. Drifting the card's window reds `test_the_p50_card_and_the_p50_chart_are_one_population`; truncating either rank reds the oracle tests. |
| 7 | **DASH-04 / D-10** — daily series is dense, ascending, window-bounded, empty-safe | VERIFIED | Live: 14 dense days, idle days `runs=0, p50_ms=None`. Removing the zero-fill reds 4 tests. |
| 8 | **SC-4 / DASH-05** — a visitor submits a prefilled example with the demo key and watches it stream live (server half) | VERIFIED | Real run via the demo key returned 200 with `X-Relay-Run-Uid` and `X-Accel-Buffering: no`; the form posts `X-API-Key: DEMO_KEY` to both `/tickets` and `/process`, streams with `fetch` + `getReader()` + `\n\n`-buffered parse. Dropping the header, the anti-buffering header, or the key each reds a test. |
| 9 | **D-04** — the page is a packaged template, no HTML literal in `main.py` | VERIFIED | `grep -cE "<(div\|span\|section\|html\|body)" src/relay/main.py` → **0**; `_TEMPLATE_PATH` resolved from `relay.__file__`; CI docker smoke curls `/dashboard` for markup only the shipped page has. |
| 10 | **D-13 / deployability** — the guarded ALTERs are safe against the live volume | VERIFIED | `SCHEMA` names neither `elapsed_ms` nor `origin`, so fresh and existing DBs take the *same* migration path; dropping the `PRAGMA` guard reds 4 tests. The CI docker job creates a named volume, boots, `docker rm -f`, boots **again on the same volume** and re-curls — genuinely exercising `_add_column_if_missing`'s skip branch (the ADD branch runs on first boot). |
| 11 | **SC-2 / D-02, CR-01** — the demo drill-down does not disclose a tool's output about anyone else | **FAILED** | See the CR-01 section below. Reproduced anonymously, with no credentials, using only the address the form itself pins. |
| 12 | **SC-4 / DASH-05** — the Try-it interaction (choose example, send, deep link) survives as wiring, not as tokens | **FAILED (partial)** | Three separate deletions of the actual event bindings each leave the suite at **417 green**. Detail in gaps. |
| 13 | Try-it deep link opens the submitter's own run in a browser | PRESENT_BEHAVIOR_UNVERIFIED | Code present and shaped correctly; no binding test, and checkpoint step 5 never witnessed. |
| 14 | The demo-vs-owner fidelity contrast is visible in a browser | PRESENT_BEHAVIOR_UNVERIFIED | Server control proven three ways; presentation unwitnessed (checkpoint step 6). |

**Score:** 9/12 counted must-haves verified (2 further truths present, behaviour-unverified).

---

## Verdict: is the CR-01 class closed?

**No.** CR-01's fix closed two of the three vectors. The third is live and anonymously reachable.

**What the fix closed (both independently confirmed by mutation):**
- Deleting `and raw_tool in _DEMO_RAW_TOOLS` / `and payload.get("tool") in _DEMO_RAW_TOOLS` reds `test_a_demo_originated_run_is_full_fidelity`. `lookup_customer` is genuinely off the raw allowlist on the demo branch — live probe confirms its step carries only `arg_keys` and `is_error`.
- Passing `withheld=()` from the route reds the same test. The `customer_email` is masked **by value**, not by key name — CR-02's confusion is genuinely fixed.
- Forcing `full_fidelity=True` reds T1, T2 and the NULL-origin test. The authorisation half binds.

**What is still open — model prose.** The demo branch publishes, raw:
- `step["text"]` — the model's reasoning (`events.py:540-543`)
- `create_escalation.reason` and `send_reply.body` via `step["input"]` (`events.py:553-557`)

…masked only against `tickets.customer_email`. Everything else `lookup_customer` returned — the customer's **name**, **plan**, and up to ten of that address's **ticket subjects** — is restated by the model into exactly those fields and republished. The system prompt actively drives it: *"Look up the customer … so you know their plan and history"*, *"Address the customer by name"*.

**Reproduced with zero credentials, using only the form's own pinned address:**

1. Visitor A submits a demo ticket as `mia@datalane.ai` with subject `VISITOR-A-PRIVATE-SUBJECT-8812`.
2. Visitor B picks the same "billing" chip (no email field needed — the address is pinned). The agent calls `lookup_customer`, and the model writes *"Mia Torres is on the pro plan. Her recent tickets include 'VISITOR-A-PRIVATE-SUBJECT-8812'."*
3. A wholly anonymous client with **no API key**: `GET /metrics` → harvest every `run_uid` (restored to `_PUBLIC_RUN_COLUMNS` this phase) → `GET /runs/{uid}`.

```
uids harvested anonymously from /metrics: 2
  visitor A's subject    reachable anonymously: True
  customer name          reachable anonymously: True
  customer plan          reachable anonymously: True
```

A second probe (arbitrary sentinels, demo run, prose + escalation reason) returned:
`leaked via demo drill-down: ['third-party name', 'third-party plan', 'third-party ticket subject']`.

**This is the rule the code states, violated.** `project_run_detail`'s docstring: *"It does NOT cover a tool's output about ANYONE ELSE. 'The visitor authored this content' is a claim about what they SENT; it was never a claim about what the service went and looked up in response."* The prose path is the service's lookup, re-emitted.

**Severity, stated honestly (bounded, not dismissed).** `lookup_customer` returns `{"found": false}` for any address without a `customers` row, and no route inserts one — so an attacker cannot pivot to an arbitrary victim's PII. The reachable set is: the four **fictional** seeded identities' name/plan (low value), and **every ticket subject ever filed against those four addresses** — by other demo visitors, and by the owner (`scripts/demo.sh`, evals). Bodies are not exposed (`recent_tickets` selects `id, subject, status, created_at`). So: real-PII exposure is not reachable; **cross-visitor and owner-authored subject-line disclosure on a keyless, 30-day-retained, anonymously-enumerable route is.**

**Why the shipped test did not catch it.** `test_a_demo_originated_run_is_full_fidelity` asserts `DRILL_SUBJECT not in whole`. That assertion is *not* vacuous — mutation B reds it, because `DRILL_SUBJECT` rides `lookup_customer.recent_tickets`. But the scripted model never writes `DRILL_SUBJECT` into prose, so the test covers the raw-payload vector for that value and not the prose vector. The docstring's claim (*"What the SERVICE looked up about someone else does not [survive], by VALUE and not by column name"*) is broader than what the test proves. Not the CR-02 failure shape repeated — an adjacent, narrower one: **a real property proven on one of its two vectors, documented as if proven on both.**

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/relay/events.py` | `project_run_detail` allowlist + demo branch + `mask_withheld` | VERIFIED (with the gap above) | 822 lines; three public serialisers named in the module docstring; `_project_tool_result` genuinely reused so the drill-down cannot out-disclose `/events` on the `result` field. |
| `src/relay/main.py` | `GET /runs/{uid}`, `/metrics.budget`, template serve, run-uid header | VERIFIED | Uid regex-gated before any query; `origin == "demo"` by equality; own rate-limit bucket; no query/header/cookie input to fidelity. |
| `src/relay/telemetry.py` | SQL-aggregated metrics, half-up percentiles, dense daily series | VERIFIED | Six named SQL constants; `_PUBLIC_RUN_COLUMNS` explicit; `run_uid` restored deliberately. |
| `src/relay/db.py` | Guarded idempotent ALTERs | VERIFIED | `_add_column_if_missing` with a `PRAGMA table_info` guard; DDL owns neither new column. |
| `src/relay/ratelimit.py` | `budget_snapshot` as the one arithmetic | VERIFIED | `/metrics.budget` is its output verbatim; reservations included. |
| `src/relay/templates/dashboard.html` | The whole page, no build step | VERIFIED (wiring partly unguarded) | 1183 lines, 0 external resources, 0 markup sinks, `createElementNS` SVG. |
| `.github/workflows/ci.yml` | Docker smoke incl. restart on a persistent volume | VERIFIED | Named volume, two boots, content-specific greps on both. |

---

## Key Link Verification

| From | To | Via | Status |
|---|---|---|---|
| `/runs/{uid}` route | `events.project_run_detail` | `full_fidelity=demo`, `known_tools`, `withheld` | WIRED — all three mutation-bound |
| `tickets.origin` | full-fidelity decision | `create_ticket` takes the gate as a **parameter** (not `dependencies=[…]`) | WIRED — mutation-bound |
| `/metrics.budget` | `enforce_daily_budget` | shared `budget_snapshot` | WIRED — mutation-bound |
| dashboard gauge | `/metrics.budget` | `spent_today_usd` / `daily_ceiling_usd` | WIRED — no JS spend arithmetic |
| runs table row | `openDrill(uid)` | `run_uid` from `last_runs` | WIRED — renaming `openDrill` reds 2 tests |
| Try-it `streamRun` | `openDrill(uid)` | `offerTheTrace(uid)` | **NOT GUARDED** — deleting the call site leaves 417 green |
| `trySend` / example chips | `submitTryIt` / `chooseExample` | `addEventListener` | **NOT GUARDED** — deleting either leaves 417 green |

---

## Data-Flow Trace (Level 4)

| Rendered value | Source | Real data? | Status |
|---|---|---|---|
| outcome distribution bars | `OUTCOME_DISTRIBUTION_SQL` GROUP BY | yes (live-probed) | FLOWING |
| daily cost / p50 / p95 | `DAILY_BUCKETS_SQL` window functions | yes | FLOWING |
| budget gauge | `budget_snapshot(conn)` | yes | FLOWING |
| drill-down steps | `run_events` rows via `project_run_detail` | yes (13 real steps) | FLOWING |
| retrieval chunk scores + cited flag | real `search_docs` result + `normalise_citation` | yes (`5.493061`, `cited=True`) | FLOWING |
| Try-it stream lines | live `/process` SSE | yes | FLOWING |

No static returns, no hardcoded fixtures, no hollow props found.

---

## Behavioural Spot-Checks

| Behaviour | Command | Result | Status |
|---|---|---|---|
| Full suite | `.venv/bin/python -m pytest -q` | 417 passed | PASS |
| Lint | `.venv/bin/ruff check src tests` | All checks passed | PASS |
| `/metrics` end to end | live `TestClient` | all 9 keys, 7 buckets, 14 dense days, budget object | PASS |
| `/runs/{uid}` end to end | live `TestClient` | complete/demo, 13 steps, timings, cited chunk | PASS |
| `/process` headers | live `TestClient` | `X-Relay-Run-Uid`, `X-Accel-Buffering: no` | PASS |
| No external page resources | grep served template | 0 | PASS |
| Anonymous disclosure probe | `/metrics` → `/runs/{uid}` with no key | third-party name/plan/subject reachable | **FAIL** |
| All 29 named 06-VALIDATION tests | individually | 29/29 passed | PASS |
| Browser rendering | — | no DOM in suite | SKIP → human |

---

## 06-VALIDATION.md map — row by row

Every named test **exists and passes**. The column below is my own mutation verdict, not the map's claim.

| Row | Test | Exists | Passes | Non-vacuous? |
|---|---|---|---|---|
| T1 leak | `test_run_detail_never_leaks_a_non_demo_runs_content` | ✓ | ✓ | **Yes** — reds under both named mutations |
| T2 tamper | `test_full_fidelity_is_server_decided` | ✓ | ✓ | **Yes** — reds on forced full fidelity |
| T2b inverse | `test_a_demo_originated_run_is_full_fidelity` | ✓ | ✓ | **Yes** for the raw-payload + email vectors; **blind** to the prose vector (see CR-01) |
| projector allowlist | `test_project_run_detail_publishes_only_named_fields` | ✓ | ✓ | Yes |
| T9 cited | `test_cited_vs_not_matches_the_citation_guards_accept_set` | ✓ | ✓ | **Yes** — reds on forced `cited=True` |
| T7 swept | `test_run_detail_of_a_swept_run_renders_as_swept` | ✓ | ✓ | Yes (4 states distinguishable) |
| T8 bucket | `test_run_detail_is_rate_limited_per_ip` | ✓ | ✓ | Yes |
| T14 elapsed | `test_run_events_carry_elapsed_ms` | ✓ | ✓ | **Yes** — reds on 2 independent timing mutations |
| origin tier | `test_ticket_origin_is_the_creation_tier` | ✓ | ✓ | Yes |
| D-13 migrations | `test_phase6_migrations_are_idempotent` | ✓ | ✓ | **Yes** — reds on dropping the PRAGMA guard |
| T3 buckets | `test_outcome_distribution_buckets_every_outcome` | ✓ | ✓ | **Yes** — reds on CASE reorder |
| public columns | `test_metrics_publishes_exactly_these_columns` | ✓ | ✓ | Yes |
| bounded last_runs | `test_last_runs_is_bounded_and_newest_first` | ✓ | ✓ | **Yes** — reds on unbounding |
| T10 oracle | `test_daily_percentiles_match_the_oracle` | ✓ | ✓ | **Yes** — reds on rank truncation |
| T15 dense | `test_daily_series_is_dense_and_empty_safe` | ✓ | ✓ | **Yes** — reds on undensify |
| half-up | `test_percentile_is_half_up` | ✓ | ✓ | Yes |
| T4 gauge=gate | `test_budget_gauge_matches_the_gate` | ✓ | ✓ | **Yes** — reds on re-derived budget |
| snapshot/gate | `test_budget_snapshot_and_the_gate_cannot_disagree` | ✓ | ✓ | Yes |
| inline SVG | `test_charts_are_built_as_inline_svg_without_a_library` | ✓ | ✓ | Grep-level (declared) |
| gauge reads server | `test_the_gauge_reads_the_servers_budget_object` | ✓ | ✓ | Grep-level (declared) |
| T6 no markup sink | `test_dashboard_never_renders_through_a_markup_sink` | ✓ | ✓ | Grep-level (declared) |
| T5 packaged template | `test_dashboard_is_served_from_the_packaged_template` | ✓ | ✓ | Yes — genuine integration |
| T12 docker smoke | `.github/workflows/ci.yml` docker job | ✓ | n/a locally | **Yes** — restarts on a persistent volume; content-specific greps |
| runs table opens drill | `test_the_runs_table_opens_the_drill_down` | ✓ | ✓ | **Yes** — reds on renaming `openDrill` |
| panel step types | `-k "drill_panel"` (6 tests) | ✓ | ✓ | Partly — cited/uncited class collapse reds; **`renderStepBody`'s null-guards are unscanned** |
| no fidelity switch | `test_the_page_never_asks_for_full_fidelity` | ✓ | ✓ | Grep-level (self-declared weak in 06-REVIEW NT-07) |
| T11 run uid | `test_process_returns_the_run_uid_to_the_submitter` | ✓ | ✓ | **Yes** — reds on dropping the header |
| fetch not EventSource | `test_try_it_streams_with_fetch_not_eventsource` | ✓ | ✓ | Partly — reds on dropping the key; blind to unbinding the button |
| three examples | `test_try_it_offers_three_editable_examples` | ✓ | ✓ | Data only — **blind to unwiring the chips** |
| T16 refusals | `test_refusals_render_as_product_copy` | ✓ | ✓ | Yes — real 429/429/503 through the real perimeter |
| disabled w/o key | `test_try_it_renders_disabled_without_a_demo_key` | ✓ | ✓ | Yes |
| DASH-01 regression | `tests/test_run_events.py` | ✓ | 53 passed | Yes |

---

## Review fixes: are the 10 genuinely fixed?

| Finding | Claimed fixed | My verdict |
|---|---|---|
| CR-01 | yes | **Partially** — raw-payload + email vectors closed and mutation-bound; **prose vector open** |
| CR-02 | yes | **Yes** — the test now pins the production shape; both mutations red it |
| WR-02 | yes | **Yes** — `public=True` skips the shared `auth` bucket; `test_a_drill_down_flood_leaves_the_live_feed_connectable` passes |
| WR-05 | yes | **Yes** — two escapers, two contexts; `\uXXXX` re-encoding present |
| WR-06 | yes | **Yes** — `finally` is the only re-enable site; the reader loop is inside the guard |
| WR-07 | yes | **Yes** — generation token, and the positional regex genuinely binds (deleting the guard reds) |
| WR-08 | yes | **Yes** — docker smoke greps `id="try-examples"` / `openDrill`, not `Relay` |
| WR-09 | yes | **Yes** — shared `_window_offset()` and identical rank; drifting the window reds |
| WR-10 | yes | **Partially** — feed describers + `renderChunks` fixed; `renderStepBody` untouched and unscanned (see gaps) |
| WR-03 (comment only) | comment only, by intent | **Yes as scoped** — the query-plan table is now accurate and pinned by a test |

---

## Known-open, accepted by the user (NOT counted as failures)

| Item | Substance | Risk if left |
|---|---|---|
| **WR-01** | Model-chosen tool name clamped on only one of the four publishing branches. The drill-down branch **is** clamped (verified: unclamping reds `test_tool_use_arg_keys_are_clamped`); the feed's `project()` is not. | A model-authored tool name reaches the live feed unclamped. `textContent`-only rendering contains it. |
| **WR-03 substance** | `/metrics` has no perimeter and 3 of its 6 reads full-scan `runs` while holding the process-wide DB lock; polled every 5s per tab. | Grows with volume; contends with `RunRecorder`. Also the enumeration surface the CR-01 residual rides. |
| **WR-04** | `budget_snapshot` iterates a module-level dict from a worker thread the loop concurrently writes. | Rare `RuntimeError` on the ungated `/metrics`. |
| Phase 5 carryover | WR-03/08/11/12 and W-3 (no bound on total `/events` connection-holding time) | Perimeter work, explicitly out of this phase's boundary. |

---

## Decisions D-01..D-11

| D | Verdict |
|---|---|
| D-01 public but server-redacted | Honoured — allowlist, field by field, `_project_tool_result` reused |
| D-02 try-it full-fidelity exception, server-decided | Honoured — `tickets.origin`, equality compare, no client input |
| D-03 uid stays a correlation token | **Honoured with a documented tension.** Literally, *"holding a uid gets you nothing that isn't already redacted"* is false for a **demo** uid — it grants raw input/result/prose the feed never published. That is D-02's exception, not a D-03 breach; but combined with `run_uid` restored to `/metrics`, it is the mechanism the CR-01 residual rides. Worth restating in the docstring. |
| D-04 template file | Honoured — 0 HTML literals in `main.py` |
| D-05 single page, dialog panel | Honoured — native `<dialog>`, no SPA |
| D-06 three editable examples | Honoured in data; **interaction unguarded** |
| D-07 real runs, not dry-run | Honoured |
| D-08 refusals as designed states | Honoured — `renderRefusal` covers 5 shapes; server carries `note` + `resets_at` |
| D-09 client-built inline SVG | Honoured |
| D-10 daily buckets | Honoured |
| D-11 gauge reads server budget | Honoured — mutation-bound |

---

## Requirements Coverage

| Req | Status | Evidence |
|---|---|---|
| **DASH-02** | ✓ **SATISFIED — can be marked complete** | SQL-aggregated cards + 7-bucket outcome distribution; live-probed; 4 mutation-bound tests |
| **DASH-03** | ⚠️ **SATISFIED WITH A DEFECT — do not mark complete yet** | All five named elements delivered (inputs, outputs, timings, chunks+scores+cited-vs-not, guardrail denials) and mutation-bound. Blocked only by the CR-01 prose residual on the demo branch, which is a DASH-03 surface. |
| **DASH-04** | ✓ **SATISFIED — can be marked complete** | Hand-rolled inline SVG, 0 external resources, server-computed gauge; WR-09 population fix binds |
| **DASH-05** | ⚠️ **SUBSTANTIALLY SATISFIED — do not mark complete yet** | Server half fully verified. The client half's three event bindings are unguarded, and the deep link is neither tested nor witnessed. |

No orphaned requirements: `REQUIREMENTS.md` maps exactly DASH-02..05 to Phase 6, and all four are claimed by plans.

---

## Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| — | — | `TBD`/`FIXME`/`XXX`/`TODO`/`HACK` in phase-6 source | ℹ️ none | Only false positives: `\uXXXX` in two docstrings, and "placeholder" naming the `dash()` helper |
| `dashboard.html` | ~660-712 | `renderStepBody` interpolates ~10 raw `s.` fields without `dash()` | ⚠️ Warning | Defence-in-depth inconsistency; matches the class WR-10 fixed elsewhere |
| `tests/test_dashboard.py` | 2516 | `_BARE_FRAME_FIELD` prefix class excludes `s.`; scan excludes `renderStepBody`; misses bare `el()` args | ⚠️ Warning | Two proven-green mutations |
| `dashboard.html` | try-it / drill blocks | Three event bindings with no test | 🛑 Blocker for DASH-05 completion | Silent feature loss |
| `events.py` / `main.py` | demo branch | Prose republishes `lookup_customer`'s third-party output | 🛑 Blocker | Anonymous disclosure, demonstrated |

No stubs, no placeholder returns, no hollow props.

---

## Gaps Summary

The phase goal is **substantially achieved**: a visitor can read cost, quality and behaviour from SQL-computed cards, an outcome distribution, hand-rolled SVG charts and a server-computed budget gauge; can open any run's redacted trace with real timings, scored retrieval chunks, cited-vs-not highlighting and guardrail denials; and can submit a prefilled example with the published key and watch it stream. That is genuinely built, genuinely wired to real data, and genuinely mutation-tested to an unusually high standard — 27 mutations, 24 of which reddened the test they should have.

Three gaps stop it being signed off:

1. **The CR-01 class is not closed.** The fix closed the raw-payload and email-literal vectors; the model-prose vector republishes `lookup_customer`'s output about a third party — name, plan, and other people's ticket subjects — on a keyless route whose uids are anonymously enumerable from `/metrics`. Demonstrated with no credentials. Severity is bounded (no arbitrary-victim PII: unseeded addresses return `{"found": false}`), but cross-visitor and owner-authored subject-line disclosure over a 30-day window is real, and it is the phase's own stated rule being violated.

2. **The Try-it interaction is guarded by token presence, not by wiring.** The send button, the example chips and the deep-link call site can each be deleted with the suite at 417 green. The deep link is also the one checkpoint step never witnessed, and the Node DOM stub that observed it was deliberately not committed — so it rests on nothing reproducible here.

3. **WR-10 was fixed on the feed and not on the drill panel**, and its regression guard cannot see the panel — a documented-as-fixed finding that is half-fixed, which is the shape most likely to be re-broken silently.

**Recommendation:** close gap 1 before deploy (it meets a live volume and a public URL); close gaps 2 and 3 before marking DASH-03/DASH-05 complete. DASH-02 and DASH-04 can be marked complete now.

---

*Verified: 2026-08-14T07:05:00Z*
*Verifier: Claude (gsd-verifier)*
*Working tree restored to `ed3afb2`; `pytest -q` = 417 passed, `ruff check src tests` clean at finish.*

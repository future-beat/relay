---
phase: 6
slug: dashboard-experience
status: planned
nyquist_compliant: true
wave_0_complete: true
created: 2026-08-12
---

# Phase 6 — Validation Strategy

> Derived from `06-RESEARCH.md` § Validation Architecture. One test is load-bearing above all
> others: **T1**, the non-demo drill-down leak test. It is the phase's central control rendered
> as an assertion, and it must be mutation-checked twice. Its **inverse** (T2b, a demo-origin
> run DOES return raw payloads) is load-bearing in the other direction — without it, D-02's
> full-fidelity exception can silently regress to redacted-for-everyone and nothing fails.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_dashboard.py -x -q` |
| **Second new module** | `tests/test_metrics.py` — the SQL-aggregation surface (plan 06-02), kept apart so its plan can run in parallel with the redactor's |
| **Full suite** | `.venv/bin/python -m pytest -q` (**341 passing at Phase 5 close — the floor, must not regress**) |
| **CI path** | `.github/workflows/ci.yml` `test` job (free, no keys) + the `docker` job, extended this phase to curl `/dashboard` and `/metrics` |
| **Lint gate** | `.venv/bin/ruff check src tests` |
| **Fixtures reused** | `client`, `conn`, `db`, `registry`, `capture_frames`, autouse `_reset_limits`, autouse `_no_outbound_http` |
| **Doubles reused** | `tests/helpers.py`: `FakeClient`, `TicketAwareFakeClient`, `response`, `tool_use_block`, `text_block`, `usage` |
| **Cost** | Zero. Every new test pins `settings.voyage_api_key = None` where retrieval is reachable and drives the agent through `FakeClient`; the REAL keys in `.env` must never be spent by the suite |

---

## Sampling Rate

- **After every task commit:** `pytest tests/test_dashboard.py -x -q` (plus `tests/test_run_events.py` for any task touching the template or `events.py`, and `tests/test_metrics.py` for plan 06-02)
- **After every wave:** `pytest -q && ruff check src tests`
- **Phase gate:** full suite green (341 + new), ruff clean, the CI docker job green — the docker job is the only automated check that can see a packaging regression — and the human checkpoint in plan 06-07 approved
- **Max feedback latency:** 10 seconds

---

## Per-Requirement Verification Map

| Req / SC | Behavior | Type | Automated command | Plan | Status |
|----------|----------|------|-------------------|------|--------|
| DASH-03 / SC-2 | **T1 (load-bearing):** a non-demo run's drill-down leaks no seeded sentinel, presence proved twice first | integration | `pytest tests/test_dashboard.py::test_run_detail_never_leaks_a_non_demo_runs_content -x` | 06-04 | ⬜ |
| DASH-03 | **T2 tampering:** `?full=1&fidelity=raw` + `X-Demo`/`X-Relay-Origin` headers return a byte-identical response | integration | `pytest tests/test_dashboard.py::test_full_fidelity_is_server_decided -x` | 06-04 | ⬜ |
| DASH-03 | **T2b inverse (load-bearing):** a demo-origin run's drill-down DOES return raw inputs/outputs — and never `customer_email` | integration | `pytest tests/test_dashboard.py::test_a_demo_originated_run_is_full_fidelity -x` | 06-04 | ⬜ |
| DASH-03 | The projector publishes only named fields; unknown type and malformed payload are dropped, not raised | unit | `pytest tests/test_dashboard.py::test_project_run_detail_publishes_only_named_fields -x` | 06-03 | ⬜ |
| DASH-03 | **T9:** `cited` matches the citation guard's accept-set, through one shared normalisation | integration | `pytest tests/test_dashboard.py::test_cited_vs_not_matches_the_citation_guards_accept_set -x` | 06-03 | ⬜ |
| DASH-03 | **T7:** a swept run renders as swept; in_flight and unrecorded are distinguishable; only absent-both is 404 | integration | `pytest tests/test_dashboard.py::test_run_detail_of_a_swept_run_renders_as_swept -x` | 06-04 | ⬜ |
| DASH-03 | **T8:** the drill-down route is rate-limited per IP on its own bucket | integration | `pytest tests/test_dashboard.py::test_run_detail_is_rate_limited_per_ip -x` | 06-04 | ⬜ |
| DASH-03 | **T14:** `elapsed_ms` is stamped from a per-run monotonic origin on both recorder paths | integration | `pytest tests/test_dashboard.py::test_run_events_carry_elapsed_ms -x` | 06-01 | ⬜ |
| DASH-03 / D-02 | `tickets.origin` is the CREATION tier; the gate is not metered twice | integration | `pytest tests/test_dashboard.py::test_ticket_origin_is_the_creation_tier -x` | 06-04 | ⬜ |
| DASH-03 / D-13 | The two new columns are added by a guarded, idempotent ALTER; legacy rows stay NULL | unit | `pytest tests/test_dashboard.py::test_phase6_migrations_are_idempotent -x` | 06-01 | ⬜ |
| DASH-02 / SC-1 | **T3:** outcome buckets are SQL-computed and map every `outcome` string the one call site can write | unit | `pytest tests/test_metrics.py::test_outcome_distribution_buckets_every_outcome -x` | 06-02 | ⬜ |
| DASH-02 | `_PUBLIC_RUN_COLUMNS` stays an exact key set — now including `run_uid`, deliberately | integration | `pytest tests/test_run_events.py::test_metrics_publishes_exactly_these_columns -x` | 06-02 | ⬜ |
| DASH-02 | `last_runs` is bounded and newest-first (a `LIMIT`, not a Python slice) | unit | `pytest tests/test_metrics.py::test_last_runs_is_bounded_and_newest_first -x` | 06-02 | ⬜ |
| DASH-04 / D-10 | **T10:** SQL daily percentiles equal a half-up Python oracle over randomised data | property | `pytest tests/test_metrics.py::test_daily_percentiles_match_the_oracle -x` | 06-02 | ⬜ |
| DASH-04 | **T15:** the daily series is dense, ascending, window-bounded and empty-safe | unit | `pytest tests/test_metrics.py::test_daily_series_is_dense_and_empty_safe -x` | 06-02 | ⬜ |
| DASH-04 | One definition of median: `_percentile` is half-up and matches the SQL rank formula | property | `pytest tests/test_metrics.py::test_percentile_is_half_up -x` | 06-02 | ⬜ |
| DASH-04 / D-11 | **T4:** the gauge and the gate cannot disagree — `/metrics.budget` equals `budget_snapshot`, reservations included | integration | `pytest tests/test_dashboard.py::test_budget_gauge_matches_the_gate -x` | 06-04 | ⬜ |
| DASH-04 / D-11 | `budget_snapshot` is the gate's own arithmetic; the 503 detail keys did not move | unit | `pytest tests/test_dashboard.py::test_budget_snapshot_and_the_gate_cannot_disagree -x` | 06-01 | ⬜ |
| DASH-04 | Charts are inline SVG with no CDN, no `<script src>`, no build step; every chart has an empty state | grep | `pytest tests/test_dashboard.py::test_charts_are_built_as_inline_svg_without_a_library -x` | 06-05 | ⬜ |
| DASH-04 | The gauge reads `spent_today_usd`/`daily_ceiling_usd` and never re-derives spend from `last_runs` | grep | `pytest tests/test_dashboard.py::test_the_gauge_reads_the_servers_budget_object -x` | 06-05 | ⬜ |
| DASH-02/03/04/05 | **T6:** no markup sink anywhere in the served page (widening Phase 5's block-scoped rule) | grep | `pytest tests/test_dashboard.py::test_dashboard_never_renders_through_a_markup_sink -x` | 06-05 | ⬜ |
| D-04 | **T5:** the page is served from the PACKAGED template, resolved through `relay.__file__`, key substituted per request | integration | `pytest tests/test_dashboard.py::test_dashboard_is_served_from_the_packaged_template -x` | 06-05 | ⬜ |
| D-04 | **T12:** the built image serves `/dashboard` and `/metrics` | CI smoke | `.github/workflows/ci.yml` `docker` job | 06-05 | ⬜ |
| DASH-03 / SC-2 | The runs table and the live feed both open the drill-down; a null-uid row has no control | grep | `pytest tests/test_dashboard.py::test_the_runs_table_opens_the_drill_down -x` | 06-06 | ⬜ |
| DASH-03 | The panel renders all eight step types, cited-vs-not, timings (dash for legacy NULLs) and the four run states | grep | `pytest tests/test_dashboard.py -k "drill_panel" -x` | 06-06 | ⬜ |
| DASH-03 / D-02 | Nothing on the page asks for full fidelity (`full=`, `fidelity=`, `X-Demo` all absent) | grep | `pytest tests/test_dashboard.py::test_the_page_never_asks_for_full_fidelity -x` | 06-06 | ⬜ |
| DASH-05 / SC-4 | **T11:** `/process` returns the run uid to its submitter; both streams set anti-buffering headers | integration | `pytest tests/test_dashboard.py::test_process_returns_the_run_uid_to_the_submitter -x` | 06-04 | ⬜ |
| DASH-05 | Try-it streams with `fetch` + a `\n\n`-buffered parse, never `EventSource` (which cannot POST) | grep | `pytest tests/test_dashboard.py::test_try_it_streams_with_fetch_not_eventsource -x` | 06-07 | ⬜ |
| DASH-05 / D-06 | Three editable examples, no email field | grep | `pytest tests/test_dashboard.py::test_try_it_offers_three_editable_examples -x` | 06-07 | ⬜ |
| DASH-05 / D-08 | **T16:** 429 (create), 429 (process) and 503 (budget) carry a renderable `note` and an ISO `resets_at` | integration | `pytest tests/test_dashboard.py::test_refusals_render_as_product_copy -x` | 06-07 | ⬜ |
| DASH-05 | An unconfigured deployment renders the form disabled and prints no `"None"` | integration | `pytest tests/test_dashboard.py::test_try_it_renders_disabled_without_a_demo_key -x` | 06-07 | ⬜ |
| DASH-01 (regression) | Phase 5's feed assertions still pass: markers, EventSource wiring, FEED_TYPES, CLOSED branch, 5s poll | integration | `pytest tests/test_run_events.py -q` | 06-05/06/07 | ✅ exists |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**The three that cannot be allowed to pass vacuously:**

1. **T1 (leak).** Sentinels must be shown present TWICE before absence is asserted — once in the run's
   own owner-facing SSE body and once in the raw `run_events` payloads — and the assertions must
   include `steps` non-empty plus `{tool_use, tool_result, guardrail} ⊆ step types`. Without those, a
   projector that publishes nothing passes. Two independent mutations must red it: forwarding the raw
   payload on the public branch, and defaulting `full_fidelity=True`.
2. **T2b (demo inverse).** Asserts the sentinels ARE present. If the demo branch were deleted entirely,
   every other test in this phase would stay green.
3. **T9 (cited-vs-not).** Must derive the expected accept-set from the run's own `search_docs` results
   rather than hardcoding ids, and must assert the retrieved set is non-empty first — otherwise a
   change in `kb/` content silently empties both sides and the comparison is trivially true.

**Known-weak by construction (say so, do not dress it up):** every `grep` row above asserts served HTML.
There is no DOM in this suite — no jsdom, no headless browser — so those rows are regression guards on
the page's source, not evidence a browser renders anything. The human checkpoint is what closes that gap,
and it is the last task of plan 06-07.

---

## Wave 0 Requirements

- [x] `tests/test_dashboard.py` — created by plan 06-01, task 1 (before any other plan needs it)
- [x] `tests/test_metrics.py` — created by plan 06-02, task 1, with `seed_runs(conn, *, days, per_day, ...)` writing `runs` rows at controlled `created_at` offsets via direct SQL (T3, T10 and T15 all need backdated rows and `record_run` cannot backdate)
- [x] `_demo_ticket(client, ...)` helper posting with `X-API-Key: test-demo-key` so `origin == 'demo'` — plan 06-04, task 3; the counterpart to `test_run_events.py`'s `_make_ticket`, which rides the owner header
- [x] `_block(html, name)` marker-extraction helper with the "markers gone → every assertion below is vacuous" guard — plan 06-05, task 2; copied in spirit from `tests/test_run_events.py:2068-2073`
- [x] `SENTINELS` tuple covering the four vectors the raw payload carries (email, ticket body, fake key, fabricated citation) — plan 06-04, task 3
- [ ] No framework install — pytest/pytest-asyncio already present, and this phase installs nothing

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| The under-a-minute read: cost, quality and behaviour legible without scrolling into the weeds | Phase goal / SC-1 | A layout judgement no assertion can make | Plan 06-07 checkpoint, step 2 |
| A browser actually renders the feed, the charts, the panel and the stream | DASH-01..05 | No DOM in the suite | Plan 06-07 checkpoint, steps 3-5 |
| A refusal reads as the cost control working rather than as a fault | DASH-05 / D-08 | Copy and styling judgement | Plan 06-07 checkpoint, step 6 |
| The idle feed reconnects without looking broken | DASH-01 / D-09 | Requires a real EventSource over a real idle ceiling | Plan 06-07 checkpoint, step 7 |
| `elapsed_ms` and `origin` present on the live volume after deploy | DASH-03 / D-13 | The ALTERs run against the existing prod DB, not a fresh one | After deploy: `sqlite3 /data/relay.db '.schema run_events'` and `'.schema tickets'` |
| The template shipped inside the deployed image | D-04 | Hatchling honours `.gitignore`; the suite reads the source tree either way | Covered automatically by the CI docker smoke (T12); after deploy, `curl -sf https://relay-agent.fly.dev/dashboard` |
| Window functions work in the runtime image's SQLite (assumption A1) | DASH-04 | Docker absent on the dev machine | Covered by the CI docker smoke's `/metrics` curl |

---

## Validation Sign-Off

- [x] All tasks have an `<automated>` verify, or are the single human checkpoint
- [x] Sampling continuity: no 3 consecutive tasks without an automated verify
- [x] Wave 0 covers every helper the map's tests depend on, created by the first plan that needs it
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] T1, T2b and T9 mutation-checked, with the mutations named in their docstrings
- [x] No test spends money: `voyage_api_key` pinned to None where retrieval is reachable, agent driven by `FakeClient`, `_no_outbound_http` autouse
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** covered by plans 06-01..06-07 (2026-08-12).

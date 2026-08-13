---
phase: 06-dashboard-experience
plan: 04
subsystem: api
tags: [security-boundary, redaction, sse, rate-limiting, budget, fastapi, sqlite]

requires:
  - phase: 06-dashboard-experience
    provides: "06-01's tickets.origin, run_events.elapsed_ms, budget_snapshot and the ('run_detail','anon') bucket; 06-03's project_run_detail"
  - phase: 05-run-event-persistence-live-feed
    provides: "run_events rows, RunRecorder, the runs.run_uid soft join key, purge_expired_run_events"
provides:
  - "GET /runs/{run_uid} — the public, keyless, rate-limited, LIMIT-bounded drill-down; this phase's security boundary made reachable"
  - "tickets.origin written from the CREATION tier (D-02 / T-06-15)"
  - "X-Relay-Run-Uid on POST /process — the submitter's correlation token, with the SSE event contract byte-unchanged"
  - "Cache-Control: no-cache + X-Accel-Buffering: no on both streaming routes (05-REVIEW IN-02, closed)"
  - "/metrics.budget — budget_snapshot's own arithmetic on the gauge (D-11)"
  - "main._TICKET_COLUMNS / _DETAIL_RUN_COLUMNS — no star select survives in main.py"
affects: [06-05 dashboard template, 06-06 try-it, 06-07 polish]

tech-stack:
  added: []
  patterns:
    - "The authorisation decision is taken from stored state and is absent from the handler's signature — tampering has nothing to reach"
    - "Read the field only on the branch that publishes it, so the redacted path never holds the secret in a local"
    - "Absence is a status string, never a 404: a swept run must be distinguishable from a forged uid"

key-files:
  created: []
  modified:
    - src/relay/main.py
    - tests/test_dashboard.py

key-decisions:
  - "origin is anchored on the CREATION tier and never the processing tier — a process-anchored flag makes an owner-authored ticket full fidelity for anyone holding the published demo key (T-06-15)"
  - "full_fidelity is derived by `origin == \"demo\"` equality; the route signature takes exactly one path parameter, so no query param, header or cookie exists to tamper with (T-06-14)"
  - "The demo branch reads tickets.subject/body in a SECOND query taken only when origin == 'demo', so the redacted path never materialises the text at all"
  - "A swept run is 200 with status 'swept' and a note naming the window, never a 404 (T-06-18)"
  - "The retention comparison is asked of SQLite with the same datetime('now', '-N days') expression purge_expired_run_events uses, rather than re-derived from a Python clock"
  - "The uid is returned as a response HEADER, not a new SSE frame — the milestone's compatibility constraint keeps the event contract byte-identical"
  - "X-Relay-Run-Uid is minted in the handler; the generator closes over it, so the header and the rows provably carry one value"

patterns-established:
  - "Route-level fail-closed pinning: the NULL-origin path is tested through the live HTTP route, not only at the stored column"
  - "Presence-proved-twice sentinel leak testing extended to a fourth vector (guardrail.missing_citations)"
  - "An _ExplodingConn stand-in for app.state.conn as the assertion that a rejection happened before the database"

requirements-completed: [DASH-03, DASH-04, DASH-05]

duration: 32min
completed: 2026-08-12
---

# Phase 6 Plan 04: The Drill-Down Route Summary

**`GET /runs/{run_uid}` is live: public, keyless, metered in its own bucket, bounded by a LIMIT, honest about swept runs — and its full-fidelity exception is decided from `tickets.origin` written by the CREATION tier, so no request input can widen it.**

## Performance

- **Duration:** ~32 min
- **Tasks:** 3/3
- **Files modified:** 2
- **Suite:** 381 passed (floor 367; +14 from this plan)
- **Lint:** `ruff check src tests` clean

## Accomplishments

### Task 1 — the plumbing (`45cfc24` RED, `2f46a46` GREEN)

| Change | Why |
|---|---|
| `create_ticket` takes `tier: Tier \| None = _CREATE_GATE` and stores it as `tickets.origin`; `dependencies=[Depends(create_gate)]` removed from the decorator | `_gate`'s dependency RETURNS the resolved tier and `dependencies=[...]` throws it away. Declared in both places it would meter the perimeter twice per request, silently halving every demo limit in D-04. |
| `run_uid = uuid.uuid4().hex` moved into the `/process` HANDLER; generator closes over it; returned as `X-Relay-Run-Uid` | Try-it streams `/process` with `fetch` while watching the redacted `/events` mirror of the same run. Without the uid the page renders one run twice, in two fidelities, unconnected — and cannot deep-link its own drill-down. |
| `Cache-Control: no-cache` + `X-Accel-Buffering: no` on **both** StreamingResponses | 05-REVIEW IN-02, closed. The difference between "the feed is live" and "the page hangs for 20s behind a buffering proxy". |
| `_get_ticket` reads `_TICKET_COLUMNS` instead of a star select | `tickets` just grew `origin`, which is deliberately NOT part of the `Ticket` API model. A star select would have pushed it into `Ticket(**dict(row))` the moment the migration ran — the exact shape WR-10 exists to prevent. |

`grep -c "SELECT \*" src/relay/main.py` → **0** (two prose occurrences in comments were reworded, following 06-01's precedent; one of them — the `/metrics` comment claiming `run_metrics` does a star select — was also factually stale since 06-02's SQL aggregations landed).

**The SSE event contract is byte-unchanged.** The uid rides a response header; no event type was added and no payload altered. The test asserts no `run`/`run_uid` event name appeared in the stream.

### Task 2 — the route and the gauge (`d0fcee8` RED, `d25eae1` GREEN)

`run_detail_gate = _gate("run_detail", public=True)` — its **own** bucket, so a drill-down flood cannot spend the live feed's reconnect allowance (breaking that visitor's own feed) or hide inside the feed's log line.

`_RUN_UID_RE = re.compile(r"\A[0-9a-f]{32}\Z")` rejects before any DB touch; `_DETAIL_RUN_COLUMNS` is the explicit published tuple (no `id`, no `run_uid` — the envelope carries the latter).

One `asyncio.to_thread` around a callable that reads, in this order:

1. `SELECT seq, type, payload, elapsed_ms, ticket_id FROM run_events WHERE run_uid = ? ORDER BY seq LIMIT ?` — **events first**, because every event row commits before the `runs` row exists, so a run finishing between the two reads renders complete rather than holed.
2. `SELECT {_DETAIL_RUN_COLUMNS} FROM runs WHERE run_uid = ?`
3. the retention comparison, only when a `runs` row exists with no rows: `SELECT ? < datetime('now', ?)` — SQLite's own clock and the same expression `purge_expired_run_events` uses, so the page cannot contradict the sweep.
4. `SELECT origin FROM tickets WHERE id = ?`, and **only if `origin == "demo"`**, a second `SELECT subject, body`. A single unconditional select would work identically today and leave the redacted path holding the text in a local, one edit from disclosing it.

`fetchall()`/`fetchone()` all happen inside the callable.

Absence matrix, as built:

| `runs` | `run_events` | Render |
|---|---|---|
| present | present | `status: "complete"` |
| absent | present | `status: "in_flight"`, `run: null` |
| present | absent, older than the window | `status: "swept"` + `note` naming the retention days |
| present | absent, inside the window | `status: "unrecorded"`, no note |
| absent | absent | **404** `"unknown run"` |

`/metrics` now composes `run_metrics` + `budget_snapshot` in ONE offload under `budget`. main.py composes so `telemetry` never imports `ratelimit`, and the gauge reads the gate's own arithmetic including reserved spend (D-11).

### Task 3 — the load-bearing test (`c6abdfe`)

Four sentinels, four **observed** payload fields (prose alone would be vacuous — the public branch reduces `text` to a character count):

| Sentinel | Vector |
|---|---|
| customer email | `lookup_customer.input.email` **and** `tool_result.result.customer.email` |
| ticket body | `create_escalation.reason` (plus the same string in model prose) |
| fake API key | `search_docs.input.query` |
| fabricated citation | `guardrail.missing_citations`, via a real citation-guard denial |

Presence is proved **twice** — in the run's own owner-facing SSE body and in the raw `run_events` payload rows — before any absence is claimed. Leaks are collected **per step and per sentinel** (the failure names both), then the whole envelope is checked, then the anti-vacuity assertions: steps non-empty, `{tool_use, tool_result, guardrail}` present, all three tool names published, `arg_keys == ["email"]` with no `input`, `missing_count == 1` with no `missing_citations`. A drill-down that leaked nothing by publishing nothing fails this.

## Mutation Log

Every mutation was applied to source, run, confirmed RED, and restored; `diff -q` against a pristine copy confirmed restoration after each batch.

| # | Mutation | Test | Result |
|---|---|---|---|
| 1 | Keep `dependencies=[Depends(create_gate)]` and hardcode `origin` | `test_ticket_origin_is_the_creation_tier`, `test_create_gate_is_not_charged_twice` | **RED** (both) |
| 2 | **T-06-15:** anchor the flag on the /PROCESS tier (origin set by an `UPDATE` when the run starts) | `test_ticket_origin_is_the_creation_tier` | **RED** — the never-processed owner ticket reads NULL |
| 3 | Mint `run_uid` back inside `event_stream` | `test_process_returns_the_run_uid_to_the_submitter` | **RED** — no header |
| 4 | Drop the `headers={...}` from /events' StreamingResponse | `test_streaming_routes_are_not_buffered` | **RED** |
| 5 | Return 404 when the event rows are missing | `test_run_detail_of_a_swept_run_renders_as_swept` | **RED** |
| 6 | Drop the `_RUN_UID_RE` guard | `test_run_detail_404s_on_a_malformed_or_unknown_uid` | **RED** — `_ExplodingConn` fired |
| 7 | Drop `dependencies=[Depends(run_detail_gate)]` | `test_run_detail_is_rate_limited_per_ip` | **RED** |
| 8 | Drop `LIMIT ?` from the run_events read | `test_run_detail_read_is_bounded` | **RED** |
| 9 | Gauge spend summed from the response's own `last_runs` | `test_budget_gauge_matches_the_gate` | **RED** — the live reservation vanished |
| 10 | `demo = origin != "owner"` (fail-open on NULL) | `test_a_legacy_null_origin_run_is_redacted_at_the_route` | **RED** |
| 11 | **Leak, mutation A:** public branch forwards the raw payload (`step.update(payload)`) | `test_run_detail_never_leaks_a_non_demo_runs_content` | **RED — 8 (step, secret) pairs covering ALL FOUR sentinels** |
| 12 | **Leak, mutation B (independent):** route passes `full_fidelity=True` | same, **and** the NULL-origin route test | **RED** (both) |
| 13 | **T-06-14:** route accepts `full: bool = False` and ORs it in | `test_full_fidelity_is_server_decided` | **RED** — byte comparison differed |
| 14 | Demo branch removed (`full_fidelity=False` + no `ticket`) | `test_a_demo_originated_run_is_full_fidelity` | **RED**; the leak test stayed green, which is the point of having the inverse |

Mutation 11's failure output is the useful one — it named all four sentinels across `tool_use`, `tool_result`, `guardrail` and `text` steps, so a partial fix that closed one field cannot pass.

### Stated plainly — what is a regression guard rather than proof

- **`test_create_gate_is_not_charged_twice` passes against the pre-change code too.** One declaration is charged once, so it proves nothing new; its entire value is failing the moment someone adds the parameter without deleting the decorator. Its docstring says so.
- **`test_streaming_routes_are_not_buffered`** asserts headers, which is all that can be asserted in-process. Whether a real proxy honours `X-Accel-Buffering` is a deployment fact, not a test fact.
- **The Phase-5 invariant greps** (`async with` in agent.py == 0, one `broker.publish` site, frozen files unchanged, `SELECT *` == 0) were run by hand this plan, not as tests.
- **The traversal cases in the 404 test** (`../../etc/passwd`, `%2e%2e%2f…`) never reach the route — httpx removes dot segments client-side and a decoded `%2f` stops the path matching the single-segment route. They are asserted on status alone, with the exploding connection still installed, and the comment says which hop refuses them. The uid regex is genuinely exercised by the five other malformed forms.

## Deviations from Plan

### Auto-fixed issues

**1. [Rule 1 — Bug] The plan's traversal test case could not assert what it claimed.**
- **Found during:** Task 2. `anon.get("/runs/../../etc/passwd")` is normalised to `GET /etc/passwd` by the client, so it 404s in the router with `detail: "Not Found"` and never reaches the handler; the percent-encoded form behaves the same after decoding.
- **Fix:** Split the assertion — five malformed-but-single-segment forms (including SQL-injection shapes) assert the `"unknown run"` domain detail under `_ExplodingConn`, and the two traversal forms assert status only, with a comment naming the hop that refuses them. Both groups still run with the database sentinel installed.
- **Commit:** `d0fcee8`.

### Ordering change

**2. The NULL-origin-at-the-route test was implemented in Task 2, not Task 1.**
The brief folded it into Task 1's behaviour list, but it drives the live `GET /runs/{uid}`, which Task 1 does not build. Implementing it in Task 1 would have left the task un-committable green. It exists, it is named `test_a_legacy_null_origin_run_is_redacted_at_the_route`, and mutations 10 and 12 both turn it red. Task 1's `origin` test still pins the stored NULL by identity (`is None`, never falsiness).

**3. The leak/inverse tests drive `/process` through the TestClient rather than the `capture_frames` fixture.**
The plan suggested `capture_frames` for the presence half. `resp.text` from `client.post(.../process)` **is** the owner-facing, full-fidelity stream — the same bytes — and it additionally carries `X-Relay-Run-Uid`, which the tests need. The presence half is unchanged in strength: it asserts every sentinel appears in that stream and again in the raw rows.

No Rule 4 (architectural) situations arose. No packages installed.

## Files Not Touched

Frozen per D-03 and asserted `git diff --quiet HEAD` clean before every commit: `src/relay/mcp_server.py`, `src/relay/evals.py`, `.github/workflows/evals.yml`.

`src/relay/events.py` was not modified — `project_run_detail` is consumed exactly as 06-03 shipped it, with `full_fidelity` derived by boolean equality, `known_tools` built from `app.state.registry` as `{name: frozenset(spec.schema["input_schema"]["properties"])}`, and `SELECT seq, type, payload, elapsed_ms` (plus `ticket_id`, which the route needs and the projector ignores).

`.planning/STATE.md` and `.planning/ROADMAP.md` — untouched, as instructed; the orchestrator owns them.

## Phase 5 invariants

```
grep -c "async with" src/relay/agent.py    -> 0
grep -c "broker.publish" src/relay/*.py    -> main.py: 1, everything else: 0
grep -c "SELECT \*" src/relay/main.py      -> 0
grep -c "run_detail_gate" src/relay/main.py -> 2  (definition + decorator)
grep -c "asyncio.to_thread" src/relay/main.py -> 6 (lifespan, gate, create, get_ticket, metrics, run_detail)
tests/test_run_events.py::test_events_output_comes_only_from_two_serialisers -> green
```

The `/events` generator is untouched: the drill-down is a different route with its own tests, not a third path in that generator.

## Threat Flags

None beyond the plan's own register. The new surface is exactly `GET /runs/{run_uid}`, which T-06-13 through T-06-18 anticipated; every `mitigate` disposition has a named, mutation-checked test:

| Threat | Where it is pinned |
|---|---|
| T-06-13 information disclosure | `test_run_detail_never_leaks_a_non_demo_runs_content` (mutations 11, 12) |
| T-06-14 tampering | `test_full_fidelity_is_server_decided` (mutation 13) |
| T-06-15 elevation of privilege | `test_ticket_origin_is_the_creation_tier` (mutation 2) |
| T-06-16 third-party email | `test_a_demo_originated_run_is_full_fidelity` — absent by key AND by value |
| T-06-17 denial of service | `test_run_detail_is_rate_limited_per_ip`, `test_run_detail_read_is_bounded`, `..._404s_on_a_malformed_...` (mutations 6, 7, 8) |
| T-06-18 swept → 404 | `test_run_detail_of_a_swept_run_renders_as_swept` (mutation 5) |

## Known Stubs

None.

## Handoff to 06-05 / 06-06

- The drill-down envelope is `{run_uid, ticket_id, demo, status, note?, run, steps}`, with `ticket: {subject, body}` added **only** when `demo` is true. `run` is `null` on the `in_flight` branch — the template must render that, not assume a dict.
- `status` is one of `complete | in_flight | swept | unrecorded`. Only `swept` carries `note`. A 404 means the uid is unknown, and is the only 404 the route emits.
- Try-it reads `X-Relay-Run-Uid` off the `POST /tickets/{id}/process` response to de-duplicate its own run against the ambient feed and to open `openDrill(uid)` — which will be full fidelity, because Try-it posts `/tickets` with the demo key.
- `/metrics.budget` carries `spent_today_usd`, `daily_ceiling_usd`, `remaining_usd`, `exhausted`, `resets_at`. It includes reserved spend, so it can jump by up to `max_run_cost_usd` while a run is in flight — the UI copy should say "includes runs in flight" rather than the number being smoothed.
- Every value from these responses is model-influenced somewhere (`tool` is clamped, but `status`, `note` and ticket text are strings): the whole-page `textContent` rule stands.

## Self-Check: PASSED

- `src/relay/main.py` — FOUND (`async def run_detail` at L503; `X-Relay-Run-Uid` header; `_TICKET_COLUMNS`, `_DETAIL_RUN_COLUMNS`, `_RUN_UID_RE`)
- `tests/test_dashboard.py` — FOUND (28 tests, all passing)
- Commit `45cfc24` — FOUND
- Commit `2f46a46` — FOUND
- Commit `d0fcee8` — FOUND
- Commit `d25eae1` — FOUND
- Commit `c6abdfe` — FOUND
- `.venv/bin/python -m pytest -q` → **381 passed** (floor 367)
- `.venv/bin/ruff check src tests` → All checks passed
- Frozen files → `git diff --quiet HEAD` clean

---
phase: 05-run-event-persistence-live-feed
plan: 03
subsystem: agent-persistence
tags: [sse, persistence, redaction, transactions, security, ordering]

# Dependency graph
requires:
  - phase: 05-run-event-persistence-live-feed
    plan: 01
    provides: "run_events table, runs.run_uid column"
  - phase: 05-run-event-persistence-live-feed
    plan: 02
    provides: "RunEventBroker, project(), RunRecorder.execute_and_record"
  - phase: 02-async-safe-data-layer-graceful-shutdown
    provides: "Database.transaction() nest-safe savepoints; the to_thread offload seam"
provides:
  - "run_ticket(recorder=...) — optional per-run persistence, write-tool events atomic (D-04)"
  - "event_stream mints run_uid, persists via the recorder, publishes project(event) post-commit (D-06)"
  - "app.state.broker — the live broker, built in lifespan"
  - "conftest capture_frames fixture: drives a real run, returns every published frame"
affects: [05-04 /events endpoint, phase-06 dashboard drill-down]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "yield await _persisted(AgentEvent(...)) — persistence is inside the expression that yields, so it cannot be reordered after the yield"
    - "Post-commit publish falls out of the seam's placement rather than from an ordering rule anyone must remember"
    - "Ordering asserted by sampling COUNT(*) inside publish itself, so the reverse order is observable"
key-files:
  created: []
  modified:
    - src/relay/agent.py
    - src/relay/main.py
    - tests/conftest.py
    - tests/test_run_events.py

key-decisions:
  - "recorder is a positional-or-keyword optional collaborator beside policy/budget, not keyword-only: it mirrors the two params it sits next to, and evals.py/mcp_server.py stay byte-unchanged either way"
  - "A denied WRITE tool is the one place run_events seq inverts the stream's cause-before-effect order — the price of D-04, and only visible where nothing was written to be atomic with"
  - "The caller's own SSE frame stays full-fidelity; only the broadcast fan-out is projected"
  - "The leak test collects every leaking (frame, sentinel) pair instead of failing on the first — a fix that closes one vector and leaves two is not a fix"

patterns-established:
  - "capture_frames subscribes inside the coroutine, so no asyncio.Queue can bind to the TestClient's lifespan loop"

requirements-completed: [DATA-03, DASH-01]

# Metrics
duration: 34min
completed: 2026-08-12
---

# Phase 5 Plan 03: Recorder Wired End to End Summary

**A run now writes every step to `run_events` before the caller can see it, and mirrors a field-by-field redaction of each step to the broker — with the redaction proven by a real run that carries a customer email, a ticket body and a fake API key through three different observed tool fields and publishes none of them.**

## Performance

- **Duration:** ~34 min
- **Tasks:** 3, one commit each
- **Files:** 4 modified (`agent.py`, `main.py`, `tests/conftest.py`, `tests/test_run_events.py`)
- **Suite:** 310 passed (floor 306); `ruff check src tests` clean

## Accomplishments

- **`run_ticket` gained `recorder=None`** — the same optional-collaborator shape as `policy` and `budget`. Every one of the 15 yield sites became `yield await _persisted(AgentEvent(...))`, a nested helper that offloads `recorder.record` and returns the event. Writing it *inside the yield expression* is the point: persistence cannot be reordered after the yield by a later edit, because there is no statement to move.
- **The D-04 seam** — the write-tool offload now branches. A `spec.tier == "write"` call with a recorder goes through `asyncio.to_thread(recorder.execute_and_record, ...)`, so the `run_events` row is inserted inside the transaction the tool opens on that worker thread; everything else keeps today's `to_thread(execute_bound, ...)` and is persisted at its yield. The write tool's `tool_result` is not re-recorded at the yield (a `tool_result_persisted` flag), or the same step would exist twice, once non-atomically.
- **`event_stream`** mints `run_uid`, builds the `RunRecorder`, and passes it in. `project(event)` is published to the broker when the event surfaces — which is already after `agent.py` committed it, so **D-06 holds by construction, not by an ordering rule**. `None` is a drop, never an empty frame. `record_run` gained `run_uid=run_uid`; the CR-01 `finally` was not restructured. `lifespan` builds `app.state.broker` beside `app.state.runs`.
- **`grep -c "async with" src/relay/agent.py` → 0.** No context manager held across a yield; `_persisted`'s transaction is already closed when it returns.

## Task Commits

1. **Task 1: recorder + write-tool seam** — `e05ea14`
2. **Task 2: event_stream wiring + lifespan broker** — `e0fe7f3`
3. **Task 3: capture helper + the three integration tests** — `3def923`

## Mutation Testing (all applied, all confirmed red, all restored)

| Test | Mutation applied | Result |
|------|------------------|--------|
| `test_no_projection_leaks_sensitive_data` | `return {"type": t, "tool": d.get("tool"), **d}` in `project()`'s `tool_use` branch | **FAILED for all three sentinels at once**: `[(1, 'tool_use', 'lookup_customer', 'customer email'), (4, 'tool_use', 'search_docs', 'api key'), (8, 'tool_use', 'create_escalation', 'ticket body')]`. Restored; green. |
| `test_broker_never_leads_the_database` | `asyncio.create_task(asyncio.to_thread(recorder.record, event))` instead of `await` in `_persisted` | **FAILED**: `publish #1 ran with only 0 rows committed — the broker led the database`. Restored; green. |
| `test_a_run_persists_its_full_event_sequence` | dropped `await _persisted(...)` from the `usage` yield | **FAILED**: `At index 0 diff: 'tool_use' != 'usage'`; four rows missing. Restored; green. |

**On the load-bearing leak test specifically.** It is not a happy-path check and it is not vacuous in either direction. The run is real — scripted model, real tools, real SQLite — and it carries three improbable sentinels in through three *different* fields `project()` actually inspects: the customer email via `lookup_customer` (input *and* the returned customer row), a fake API key via `search_docs.query`, and the ticket body via `create_escalation.reason`. The body vector is the one Pitfall 4 exists for: a `text` event is dropped unconditionally, so routing the body only through model prose would have made the "body never leaks" half prove nothing. Before asserting absence the test asserts *presence* — each sentinel must appear in the run's own private SSE stream **and** in the raw `run_events` payloads — so a run that quietly never carried the secrets cannot pass. Only then does it check every published frame. Under the named mutation all three vectors open, which is why the mutation column above lists three tuples rather than one.

`test_recorder_untouched_files` is a regression guard, stated plainly, not a proof of anything: it asserts `git diff --quiet HEAD` over `mcp_server.py`, `evals.py` and `ci.yml`. Its value is that it fails loudly if a later plan reaches for those files rather than fixing the seam.

## Deviations from Plan

1. **`tests/test_agent.py` does not exist.** Task 1's `<verify>` names it. Ran the actual agent-covering suites instead (the full suite: `test_guardrails.py`, `test_lifecycle.py`, `test_api.py`, `test_observability.py` all drive `run_ticket`). 310 passed. No behavioural difference — the intended regression coverage ran.
2. **The leak test collects all leaks rather than asserting per frame in place.** The plan asked for a per-frame assertion so one leaking frame fails it; a list comprehension over `(frame, sentinel)` pairs asserted `== []` keeps exactly that granularity while making the mutation run report *every* vector that opened instead of only the first. This is what produced the three-tuple evidence above; a first-failure assert would have shown only the email.
3. **`recorder` is imported concretely** (`from .events import RunRecorder`) rather than under `TYPE_CHECKING`. `events.py` imports only `config`/`db`/`models`, so there is no cycle, and a real annotation beats a string one.

## Issues Encountered

**A real ordering nuance, documented in code rather than papered over.** For a *denied* write-tier tool (ticket-binding or citation violation), `run_events` inverts the stream's deliberate cause-before-effect order: the `tool_result` row is written inside the offload and so takes the lower `seq`, while the `guardrail` event that explains it is recorded afterwards at its yield. This is the price of D-04's atomicity and it is only ever visible on a denial — the case where nothing was written to be atomic with. It is commented at the guardrail yield site in `agent.py`. The current tests do not pin it (their scripts contain no denials); phase 6's drill-down should either sort by `(seq)` and accept it or the ordering should be revisited there.

## Constraint Compliance

- `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/ci.yml` → exit 0. Asserted in the suite, not just by hand.
- `grep -c "async with" src/relay/agent.py` → **0**.
- `/events` viewers are not wired into `RunRegistry` (D-12); the broker is a separate object on `app.state`, and the `/events` route itself is untouched — that is wave 4.
- `settings.voyage_api_key` pinned to `None` in all three tests that reach `search_docs`; `_no_outbound_http` remains the backstop. No paid Anthropic or Voyage call is reachable — every run is driven by `helpers.FakeClient`.
- `STATE.md` and `ROADMAP.md` not modified by this executor.
- Every commit made without `--no-verify`, on `phase-5-run-events`.

## Known Stubs

None. `run_ticket`, `event_stream` and `lifespan` are fully wired; nothing returns placeholder data. The broker has no consumer yet — the `/events` route is wave 4 — but that is a missing caller, not a stub: the fan-out is exercised end to end by `capture_frames`, which subscribes to the real broker.

## Self-Check: PASSED

All four modified files exist on disk; `e05ea14`, `e0fe7f3`, `3def923` all resolve in `git log`. Full suite 310 passed, `ruff check src tests` clean, both re-run after the last mutation was restored.

---
*Phase: 05-run-event-persistence-live-feed*
*Completed: 2026-08-12*
</content>
</invoke>

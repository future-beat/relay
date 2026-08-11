---
phase: 05-run-event-persistence-live-feed
plan: 02
subsystem: events
tags: [sse, pubsub, redaction, sqlite, transactions, security]

# Dependency graph
requires:
  - phase: 05-run-event-persistence-live-feed
    plan: 01
    provides: "run_events table, runs.run_uid column, events_queue_maxsize/heartbeat/idle settings"
  - phase: 02-async-safe-data-layer-graceful-shutdown
    provides: "Database.transaction() nest-safe savepoints — the mechanism D-04 rests on"
provides:
  - "RunEventBroker: bounded, drop-oldest, synchronous fire-and-forget fan-out + _CLOSE_SENTINEL"
  - "project(event) -> dict | None: the SC-3 allowlist redaction for the public feed"
  - "RunRecorder: per-run event persistence, with execute_and_record as the D-04 atomic seam"
affects: [05-03 agent wiring, 05-04 /events endpoint, phase-06 dashboard drill-down]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Drop-oldest bounded fan-out: put_nowait, then get_nowait+put_nowait on QueueFull"
    - "Fail-closed allowlist projection at a public boundary (mirrors the phase-3 citation accept-set)"
    - "One outer transaction() wrapping a tool exec and its audit insert; the tool's own transaction nests as a savepoint"
key-files:
  created:
    - src/relay/events.py
  modified:
    - tests/test_run_events.py

key-decisions:
  - "publish() is a plain def, asserted on the function object — an async publish could be made to await a stalled dashboard tab and suspend a paid run (D-10)"
  - "project() returns None for unknown event types and falls through to {type,tool,is_error} for unknown tools: a yield site or tool added later is redacted by default"
  - "error frames name the API error type as error_type, not type — a raw `type` would collide with the frame's own key"
  - "run_events.payload stays raw (json.dumps(event.data, default=str)); redaction is a publish-time transform only, or phase 6 has nothing to drill into"
  - "RunRecorder is synchronous end to end: transaction() re-entrancy is per-thread, so an await would leave the transaction open while the loop resumes elsewhere"

patterns-established:
  - "_offer(q, frame) shared by publish() and close() so the shutdown sentinel reaches a full queue by the same drop-oldest path as a frame"
  - "Mutation named in the docstring of every load-bearing test, run and confirmed red before the code shipped"

requirements-completed: [DATA-03, DASH-01]

# Metrics
duration: 22min
completed: 2026-08-12
---

# Phase 5 Plan 02: Live-Feed Contracts (Broker, Projection, Recorder) Summary

**`src/relay/events.py`: a drop-oldest broker that can never backpressure a paid run, a field-by-field allowlist projection that fails closed, and a recorder whose write-tool event row shares the tool's own transaction — proven by a rollback test that forces the insert to fail and finds the reply gone.**

## Performance

- **Duration:** ~22 min
- **Tasks:** 3 (all TDD, RED then GREEN)
- **Files:** 1 created (`src/relay/events.py`, 276 lines), 1 modified (`tests/test_run_events.py`)
- **Suite:** 306 passed (floor 292); `ruff check src tests` clean

## Accomplishments

- **`RunEventBroker`** — bounded per-subscriber `asyncio.Queue`, drop-oldest on full, `publish` a plain `def` that swallows `QueueEmpty`/`QueueFull` and returns `None`. `unsubscribe` uses `discard` (idempotent, safe from a generator `finally`). `close()` sets `closed` and pushes `_CLOSE_SENTINEL` through the same drop-oldest path so even a stalled viewer's stream terminates at shutdown. Nothing is constructed at import — the broker is built in `lifespan`, per the `RunRegistry` loop-binding hazard.
- **`project()`** — every public frame built field by field. `tool_use` publishes the tool name only; `text` publishes that the model spoke, not what it said; `lookup_customer` results drop the customer row and recent tickets wholesale; `search_docs` keeps `{doc, id, score}` and never `text` or `heading`; `guardrail` keeps `{guard, tool, action}` and not the denied payload. Unknown event type → `None`; unknown tool → `{type, tool, is_error}`.
- **`RunRecorder`** — `record()` for steps with no sibling write (its own single-INSERT transaction, mirroring `record_run`); `execute_and_record()` for write-tier tools, wrapping **one** outer `transaction()` around the tool exec *and* the event insert so the tool's own `transaction()` nests as a savepoint. Monotonic per-run `seq`, raw payload.

## Task Commits

1. **Task 1: RunEventBroker** — `28873dc` (test, RED) → `2e3f4b2` (feat, GREEN)
2. **Task 2: project() allowlist** — `28c1e27` (test, RED) → `ede24f4` (feat, GREEN)
3. **Task 3: RunRecorder + atomicity** — `621f3ad` (test, RED) → `e8ea7a2` (feat, GREEN)

## Mutation Testing (all applied, all confirmed red, all restored)

| Test | Mutation applied | Result |
|------|------------------|--------|
| `test_publish_drops_oldest_and_never_blocks` | Deleted the `except asyncio.QueueFull` drop-oldest branch from `_offer`, leaving a bare `q.put_nowait(frame)` | **FAILED** — `asyncio.QueueFull` raised out of the third publish (also took `test_close_wakes_every_subscriber_with_the_sentinel` red, since the sentinel rides the same path). Restored; green. |
| `test_project_never_spreads_raw_data` | Added `**d` to the `tool_use` frame in `project()` | **FAILED** — `LEAK_SENTINEL` appeared in `json.dumps(project(event))` (also took `test_project_tool_use_drops_input` red). Restored; green. |
| `test_send_reply_and_its_event_row_commit_atomically` | Moved the `_insert_event` call out of the outer `with self.conn.transaction():` into a second, separate transaction after the tool commits | **FAILED** — `AssertionError: the reply survived its failed event insert — the two are not in one transaction; assert 1 == 0`. Restored; green. |

**On the load-bearing test specifically.** It is not a happy-path check. It monkeypatches `RunRecorder._insert_event` to raise `sqlite3.OperationalError("database is locked")`, calls `execute_and_record` with a real `send_reply` bound executor against a real file-backed `Database`, asserts the call propagated the error, and then asserts `COUNT(*) FROM replies == 0`, `COUNT(*) FROM run_events == 0`, and that the ticket is still `open` (the tool's second write went back too). Only after that does it re-run the same call unmutated and re-open the database from disk to prove both rows are durably present. Under the named mutation the reply count is 1 and the test fails, so the rollback half is load-bearing rather than decorative.

The two extra tests written beyond the plan's list (`test_close_wakes_every_subscriber_with_the_sentinel`, `test_recorder_is_synchronous`) are regression guards, not independent proofs — stated plainly. The close test does fall to the drop-oldest mutation above; the synchronous test falls to `async def record`.

## Files Created/Modified

- `src/relay/events.py` (new) — module docstring states why the three contracts share a file and why nothing is built at import. `RunEventBroker` (`_offer` shared by `publish`/`close`), `_project_tool_result` + `project`, `RunRecorder`.
- `tests/test_run_events.py` — 14 tests appended to wave 1's 4. `KB_DIR` defined locally following `tests/test_index.py`, not imported from `conftest`.

## Deviations from Plan

Three small ones, none behavioural:

1. **`_offer(q, frame)` extracted** rather than inlining the drop-oldest sequence in both `publish` and `close`. The plan described `close()` pushing a sentinel; pushing it *without* drop-oldest would mean a full (stalled) subscriber never receives the sentinel and never terminates — exactly the viewer `close()` exists to end. Sharing the helper fixes that by construction. `put_nowait` still appears twice, per the acceptance grep.
2. **`error` frames expose `error_type`, not `type`** for the Anthropic error type named in RESEARCH Pattern 5. A key literally called `type` would collide with the frame's own `type`. Renamed at the boundary with an inline comment.
3. **`_project_tool_result` guards non-dict results.** `_execute_guarded` returns `{"error": ...}` JSON for denials and validation failures, and a tool error result is not the shape the per-tool branches assume. Falls through to `{type, tool, is_error}` (Rule 2 — fail closed rather than index into a string).

## Issues Encountered

The RED commits for Tasks 1 and 3 leave `tests/test_run_events.py` failing at import (the module/class does not exist yet), which also reds wave 1's four tests in that file for one commit. That is inherent to TDD-ing a new module into an existing test file; each GREEN commit immediately follows.

## Constraint Compliance

- `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/ci.yml src/relay/agent.py src/relay/main.py` passes — frozen files byte-unchanged, wave 3/4's files untouched.
- No `**` spread in any projection code path. `grep '\*\*event\.data\|\*\*d\b' src/relay/events.py` returns exactly one hit, on line 158 — the docstring sentence *prohibiting* the spread. No code match.
- `grep "async def publish"` returns 0; `RunRecorder` has no `async def` and builds no asyncio primitive.
- No module-level `RunEventBroker(` instance.
- `settings.voyage_api_key` pinned to `None` in the only test that builds a registry, so `build_registry`'s index load cannot reach Voyage; the suite-wide `_no_outbound_http` fixture remains the backstop. No paid call is possible from these tests.
- `STATE.md` / `ROADMAP.md` not modified by this executor.

## Known Stubs

None. Every function in `events.py` is fully implemented; nothing returns placeholder data.

## Notes for Wave 3/4

- `execute_and_record`'s signature is `(execute_bound, spec, name, raw_input, policy, *, event_type)` and it returns `(result, is_error)` — a drop-in for today's `to_thread(execute_bound, spec, name, raw_input, policy)` on the **write-tool branch only**. Read tools and non-tool events use `recorder.record(event)`.
- `project()` returning `None` means "drop this frame" — `/events` must skip `None` rather than publish an empty frame.
- `_CLOSE_SENTINEL` is compared with `is`. It is a bare `object()`, so a frame can never be mistaken for it.
- The end-to-end leak test (SC-3, with a real customer email and ticket body driven through a run) is wave 3's; the allowlist here is built so it can pass, and `test_project_never_spreads_raw_data` is its unit-level twin.

## Self-Check: PASSED

`src/relay/events.py` exists on disk; all six task commits (`28873dc`, `2e3f4b2`, `28c1e27`, `ede24f4`, `621f3ad`, `e8ea7a2`) resolve in `git log`.

---
*Phase: 05-run-event-persistence-live-feed*
*Completed: 2026-08-12*

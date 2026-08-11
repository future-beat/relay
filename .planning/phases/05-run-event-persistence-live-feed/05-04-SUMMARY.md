---
phase: 05-run-event-persistence-live-feed
plan: 04
subsystem: live-feed
tags: [sse, scale-to-zero, heartbeat, idle-close, public-endpoint, leak-safety]

# Dependency graph
requires:
  - phase: 05-run-event-persistence-live-feed
    plan: 02
    provides: "RunEventBroker.subscribe/unsubscribe/close, _CLOSE_SENTINEL, project()"
  - phase: 05-run-event-persistence-live-feed
    plan: 03
    provides: "event_stream publishes project(event); app.state.broker built in lifespan"
  - phase: 02-async-safe-data-layer-graceful-shutdown
    provides: "RunRegistry.snapshot()/drain() — the D-12 invariant this plan must not disturb"
provides:
  - "GET /events — public, projection-only SSE live feed (D-11)"
  - "D-14 connect snapshot frame: what is running right now, before any live event"
  - "D-09 heartbeat + idle-close: a forgotten tab cannot pin the Fly machine awake"
  - "lifespan order drain -> broker.close() -> conn.close()"
affects: [phase-06 dashboard live panel]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "subscribe() as the first statement of the generator body, unsubscribe() in a finally — the CR-02 shape, now used twice in main.py"
    - "Idle deadline reset written only on the real-frame path, so the heartbeat branch has no line to forget to omit"
    - "A second allowlist (_snapshot_frame) for registry state, written field by field rather than routed through project(), which takes an AgentEvent"
key-files:
  created: []
  modified:
    - src/relay/main.py
    - tests/test_run_events.py

key-decisions:
  - "started_at is published as running_for_ms, not raw: a monotonic reading is meaningless off this process, and ticket_id is already public on /metrics"
  - "The snapshot frame is its own named allowlist rather than a project() branch — project()'s input is an AgentEvent, and registry state is not one"
  - "The viewer test asserts the drain's RESULT (drained is True under a 0.05s timeout), not only active == 0, so the consequence is what fails"
  - "_CLOSE_SENTINEL is imported into main.py by its private name: events.py documents it as the /events contract, and comparing by `is` needs the object itself"

patterns-established:
  - "Feed chunks stamped with whether the run had already finished — the only way 'live' is distinguishable from 'flushed at the end' in a test"

requirements-completed: [DASH-01]

# Metrics
duration: 27min
completed: 2026-08-12
---

# Phase 5 Plan 04: The Public Live Feed Summary

**`GET /events` streams a run's redacted steps live to anyone, opens with a snapshot of what is already running, heartbeats through quiet periods, and closes itself when nothing has actually happened — with the heartbeat-resets-the-deadline bug applied and confirmed red at 142 keep-alive chunks on a stream that would never have ended.**

## Performance

- **Duration:** ~27 min
- **Tasks:** 2, one commit each
- **Files:** 2 modified (`src/relay/main.py`, `tests/test_run_events.py`)
- **Suite:** 315 passed (floor 310); `ruff check src tests` clean; slowest new test 0.21s

## Task Commits

1. **Task 1: public `GET /events` route + lifespan `broker.close()`** — `71e38bb`
2. **Task 2: the five integration tests** — `80ddf4e`

## Accomplishments

- **The route is public and carries no second serialisation path.** No `dependencies=[Depends(...)]`, no `Security` argument — it joins `/metrics` and `/dashboard`. Every frame it writes came off the broker, and everything on the broker was built by wave 2's `project()` before it got there. The route itself never touches an `AgentEvent`.
- **`subscribe()` is the first statement of the generator body; `unsubscribe(q)` is in a `finally`.** The same shape as `event_stream`'s register/deregister, for the same documented reason: Starlette can cancel a `StreamingResponse` before its generator starts, and a `finally` in a body that never ran does not execute. Four exit paths were tested and all four leave the broker empty.
- **The idle deadline is assigned in exactly two places** — once before the loop, once on the real-frame path. The heartbeat branch has no assignment to forget to leave out, which is the whole of D-09.
- **`_snapshot_frame()` (D-14)** projects `RunRegistry.snapshot()` field by field into `{"type": "snapshot", "runs": [{ticket_id, running_for_ms}]}` and is yielded before the receive loop. `started_at` is never published raw.
- **A viewer never enters `RunRegistry`.** Asserted as a consequence, not a count: a drain with a viewer attached returns `True` on a 0.05s timeout.
- **`lifespan` order is drain → `broker.close()` → `conn.close()`.** Verified by a test that calls `close()` on an open stream and reads it to exhaustion.

## Mutation Testing (all applied, all confirmed red, all restored)

| # | Mutation applied | Result |
|---|------------------|--------|
| 1 | **The trap.** `idle_deadline = time.monotonic() + settings.events_idle_seconds` added to the heartbeat branch beside the `yield ": keep-alive"` | **FAILED** — `a heartbeat-only /events stream never idle-closed (D-09) — read 142 chunks and the stream was still open`. Restored; green. |
| 2 | Deleted `yield _snapshot_frame()` before the receive loop | **FAILED** — `test_events_sends_initial_snapshot_on_connect`: `assert 'event: snapshot' in ': keep-alive\n\n'`. Three other tests fell with it (collateral, see below). Restored; green. |
| 3 | Moved `subscribe()` out of the generator body into the handler | **FAILED** — `a stream that never started leaked a subscriber; assert 1 == 0`. Restored; green. |
| 4 | Replaced the `finally: unsubscribe(q)` with `except GeneratorExit: raise` (the plausible "the loop only exits cleanly" regression) | **FAILED** on both halves — `a disconnected stream leaked a subscriber` and `the closed feed left a subscriber behind`. Restored; green. |
| 5 | `app.state.runs.register(ticket_id=0)` inside the `/events` generator | **FAILED** — `the /events viewer registered as a run — the drain will wait for it; assert 1 == 0`, and the file's own runtime went from 0.45s to **25.65s** because every later lifespan shutdown then waited out its drain. That number is the production symptom. Restored; green. |

**On mutation 1 specifically.** This is the mutation the orchestrator named, and it is the one that matters, because the bug it models is invisible everywhere except a Fly bill: a heartbeat that renews its own deadline produces a stream that behaves perfectly — frames arrive, keep-alives arrive, nothing errors — and simply never ends, so `min_machines_running = 0` is defeated by the server's own keep-alive. The test is written as the heartbeat-**only** case for exactly that reason: a quiet-then-busy test would reset the deadline on the real frame and pass under the mutation. Under it the stream emitted 142 keep-alives inside a 3s ceiling and was still open; unmutated it emits ~9 and returns in 0.21s.

**Honest note on mutation 2's collateral.** Only `test_events_sends_initial_snapshot_on_connect` proves D-14. The other three tests that fell (`delivers_a_live_run`, `disconnect_unsubscribes`, `heartbeats_then_idle_closes`) fell because each reads a first chunk that is no longer there — they are coupled to the snapshot's existence, not proof of its correctness. Removing the snapshot from the D-14 test alone would still be red.

**Honest note on which tests are proofs and which are guards.** `test_events_delivers_a_live_run`, `test_events_heartbeats_then_idle_closes`, `test_events_disconnect_unsubscribes`, `test_events_viewer_is_not_a_registered_run` and `test_events_sends_initial_snapshot_on_connect` are all load-bearing — each has a named mutation above that reds it specifically. Nothing in this plan is a regression guard dressed up as a proof.

**On the live smoke's falsifiability.** The absence half (four sentinels — customer email, ticket body, fake API key, reply body — none present) would be trivially green against a feed that delivered nothing, so it is paired with a presence half: `event: tool_use`, `"tool": "lookup_customer"`, `"tool": "send_reply"`, `"cost_usd"` and `event: resolution` must all appear. And "live" is asserted rather than assumed — each chunk is recorded with whether the run had already finished when it arrived, and at least one must have arrived while it had not. A batch-at-the-end or poll-based design passes every other assertion in that test and fails that one.

## Deviations from Plan

1. **`tests/test_main.py` does not exist** (Task 1's `<verify>` names it, as wave 3's did). Ran `tests/test_api.py` + `tests/test_lifecycle.py` instead — the actual routing and drain/registry regression suites — then the full 315. No coverage was skipped.
2. **Task 2 is marked `tdd="true"` but its subject already existed** (Task 1 in the same plan builds the route). A literal RED commit would have been a test of an unwritten route in a plan that writes it one task earlier. The RED evidence is the mutation table above instead: every one of the five tests has a named mutation that was applied, run, confirmed red and restored. Stated plainly rather than papered over with a synthetic failing commit.
3. **The snapshot frame is not routed through `project()`.** `project()` takes an `AgentEvent`; `RunRegistry.snapshot()` returns `ActiveRun` dataclasses. Rather than widen `project()` (which would have meant editing frozen `events.py`), `_snapshot_frame()` is its own named allowlist over exactly two fields, with a docstring saying why it is not a spread. This is the one place the "no second serialisation path" constraint had to be interpreted: the constraint exists so *run event data* cannot reach the public without redaction, and no run event data flows through this function.
4. **`running_for_ms` instead of `started_at`.** Publishing the raw `time.monotonic()` value would be both useless to a client (an arbitrary origin) and a small process-uptime disclosure.

## Issues Encountered

None blocking. One thing worth recording for phase 6: the `client` fixture builds the tool registry during lifespan, *before* a test body can pin `settings.voyage_api_key = None`, so the startup log line reads `retrieval.mode_selected mode=semantic` on a developer machine with a real key in `.env`. No paid call results — the new tests never script `search_docs`, the pin is still applied in the live-run test per wave 3's convention, and `_no_outbound_http` blocks `httpx.post` suite-wide — but a future test that both uses the `client` fixture and drives retrieval cannot rely on a test-body pin alone.

## Constraint Compliance

- `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/ci.yml src/relay/agent.py src/relay/events.py` → exit 0. Frozen files byte-unchanged; the route needed nothing from either wave-2 or wave-3 file.
- `/events` is a route, not middleware. No `BaseHTTPMiddleware` was added.
- lifespan order: `drain(...)` → `broker.close()` → `conn.close()`.
- No package installed. No network call reachable: every run is driven by `helpers.FakeClient`, `settings.voyage_api_key` is pinned in the run-driving test, `_no_outbound_http` is the backstop.
- Tests are fast and deterministic: the idle test uses 0.02s/0.2s intervals (real values 15s/300s) and takes 0.21s; nothing sleeps for minutes.
- `STATE.md` and `ROADMAP.md` not modified by this executor.
- Both commits on `phase-5-run-events`, neither with `--no-verify`.

## Known Stubs

None. The route, the snapshot frame and the lifespan close are fully wired. The dashboard has no `EventSource` consuming `/events` yet — that is phase 6's panel, a missing caller rather than a stub, and the transport is exercised end to end here by driving the real generator.

## Self-Check: PASSED

`src/relay/main.py` and `tests/test_run_events.py` both exist on disk with the changes; `71e38bb` and `80ddf4e` both resolve in `git log`. Full suite 315 passed and `ruff check src tests` clean, both re-run after the last mutation was restored.

---
*Phase: 05-run-event-persistence-live-feed*
*Completed: 2026-08-12*
</content>
</invoke>

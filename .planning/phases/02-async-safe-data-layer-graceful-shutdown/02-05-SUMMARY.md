---
phase: 02-async-safe-data-layer-graceful-shutdown
plan: 05
subsystem: testing
tags: [pytest, asyncio, concurrency, mutation-testing, fly-io, docker, graceful-shutdown]

# Dependency graph
requires:
  - phase: 02-01
    provides: "Database with materialised Result, re-entrant lock, transaction(); tests/test_db.py"
  - phase: 02-02
    provides: "RunRegistry and tests/test_lifecycle.py's shutdown-drain section; settings.shutdown_drain_seconds"
  - phase: 02-03
    provides: "asyncio.to_thread seam at _execute_guarded; tests.helpers.TicketAwareFakeClient"
  - phase: 02-04
    provides: "app.state.runs wiring, registration inside event_stream's body, the drain 503"
provides:
  - "tests/test_lifecycle.py complete: DATA-01-e/f/g, DATA-02-a/e, D-09, and the timeout-nesting invariant"
  - "A barrier-driven guard on the materialised-Result invariant (added during execution, see Deviations)"
  - "fly.toml kill_timeout = 30 (top-level, no kill_signal)"
  - "Dockerfile CMD that execs uvicorn as PID 1 with --timeout-graceful-shutdown 20"
affects: [03-retrieval, 05-dashboard, 06-deployment]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Contract tests asserted mechanically (inspect.iscoroutinefunction) where the failure mode is silent and CI has no type checker"
    - "Thread identity, not timing, as the deterministic proof that an offload happened"
    - "Cross-file invariants (TOML config vs Dockerfile CMD vs Python settings) pinned by a parsing test"
    - "Mutation testing as the acceptance step for a test, not just for the code under test"

key-files:
  created: []
  modified:
    - tests/test_lifecycle.py
    - fly.toml
    - Dockerfile

key-decisions:
  - "The concurrency test asserts row contents (6 runs rows, 6 replies with distinct ticket ids, 6 resolved tickets each carrying its own customer_email) — never the absence of an exception, because the measured failure of the broken wrapper was a null customer_email, not a raise"
  - "A pre-warm of the default executor was added inside the concurrency test: on a cold pool the six runs queue behind thread creation instead of overlapping, which quietly weakens the hazard the test exists for"
  - "task.cancel() and body_iterator.aclose() are both kept as separate tests — GeneratorExit is synchronous, CancelledError is not, and only the latter is uvicorn's real shutdown path"
  - "kill_timeout placed above [build] and read back through tomllib in the test, because a bare TOML key below a table header silently lands inside that table"
  - "No kill_signal (D-08) — asserted absent rather than left to convention"
  - "exec inside sh -c rather than dropping sh: ${PORT:-8000} still needs a shell, but uvicorn must be PID 1"

patterns-established:
  - "Every test added here was mutation-checked: break the thing it guards, confirm it goes red, revert, confirm the tree is clean"
  - "Non-vacuity assertions (assert the registry is non-empty / the probe actually ran) precede the real assertions in contract tests"

requirements-completed: [DATA-01, DATA-02]

# Metrics
duration: 38min
completed: 2026-08-10
---

# Phase 02 Plan 05: Proving the Phase Summary

**The four surfaces waves 1 and 2 built are now pinned by tests that provably fail when broken — including the six-way concurrency probe asserting row contents — and the three shutdown timeouts nest correctly across `fly.toml`, the `Dockerfile`, and `settings.py`.**

## Performance

| Metric | Value |
|--------|-------|
| Tasks | 3 |
| Commits | 3 |
| Suite | 117 → 126 passing (9 added), 0 failing |
| Lint | `ruff check src tests` clean |
| Concurrency gate | green 5/5 consecutive |
| Duration | ~38 min |

## What Was Built

### Task 1 — the sync executor contract and the offload proof (`4a60e45`)

`test_no_registered_executor_is_a_coroutine_function` builds **both** registries — `build_registry(conn, kb_dir)` and `build_mcp_registry(conn, kb_dir)` — asserts each is non-empty first so the loop below cannot pass vacuously, then asserts `inspect.iscoroutinefunction(spec.execute)` is False for every entry. The MCP registry is covered because D-03 freezes `mcp_server.py`: if it ever drifts async it cannot be adjusted to match, so the test has to be the thing that notices (D-02).

`test_tool_execution_runs_off_the_event_loop` wraps the real `search_docs` spec with `dataclasses.replace`, recording `threading.get_ident()` from inside the executor, drives one agent run through `FakeClient`, and asserts the recorded id differs from the test coroutine's. Thread identity rather than timing — no sleeps anywhere in the file (`grep -c 'time.sleep'` is 0).

### Task 2 — concurrency, registry lifecycle, drain refusal (`0d2ec7a`)

Five tests, of which the load-bearing one is `test_overlapping_runs_all_record_without_locking_errors`: six tickets with six distinct customer emails, `TicketAwareFakeClient` on `app.state.client`, all six `process_ticket` coroutines gathered and their `body_iterator`s fully drained. It then asserts **row contents** — six `runs` rows whose ticket-id set matches and whose outcomes are all `send_reply`, six `replies` rows with distinct ticket ids and bodies past `SendReplyInput`'s `min_length`, six tickets all `resolved` and each carrying *its own* `customer_email` (asserted per row, so a single corrupted read fails it).

Also: `test_registry_is_empty_after_a_run_completes` (D-06 scale-to-zero), `test_a_stream_that_never_starts_registers_nothing` (the CR-02 asymmetry — the body is never iterated), `test_process_returns_503_while_draining` (D-09, with `reserved_usd() == 0.0` proving the refusal precedes `reserve_run()`, and `draining` reset in a `finally`), and `test_a_cancelled_run_task_still_records_and_drains`.

That last one is the only test in the repo that drives `asyncio.Task.cancel()`. The run is parked inside its **second** model call by an `asyncio.Event` that is never set, so the cancellation is guaranteed to land mid-stream rather than racing a run that already finished — a rendezvous, not a sleep. After `task.cancel()` and `gather(..., return_exceptions=True)`, it drains and asserts `active == 0`, then asserts the `runs` row exists with `cost_usd > 0` and `outcome == "incomplete"`.

### Task 3 — platform timeout arithmetic (`3df1a7b`)

`fly.toml` gains a top-level `kill_timeout = 30`, placed between `primary_region` and `[build]`; no `kill_signal` (D-08). The `Dockerfile` CMD keeps `sh -c` (for `${PORT:-8000}`) but now `exec`s uvicorn so it becomes PID 1, and passes `--timeout-graceful-shutdown 20` in place of uvicorn's wait-forever default.

`test_shutdown_timeouts_nest_correctly` parses `fly.toml` with `tomllib` — deliberately through the parser, not a grep, because a bare key below a table header is valid TOML that silently lands inside that table — extracts the graceful window from the CMD with a regex, asserts `exec uvicorn` is present and `kill_signal` is absent, and asserts `30 > 20 > 5.0`.

## Mutation Checks — Run and Reported

Every test added here was checked by breaking what it guards. All source mutations were reverted and `git status --porcelain src/` is empty.

| Mutation | Test | Result |
|----------|------|--------|
| One `tools.py` executor made `async def` | `test_no_registered_executor_is_a_coroutine_function` | **Fails**, naming `agent registry's lookup_customer.execute` |
| `RunRegistry.deregister` stubbed to a no-op | `test_a_cancelled_run_task_still_records_and_drains` | **Fails** on `assert app.state.runs.active == 0` (`1 == 0`), exactly as the plan predicted |
| `kill_timeout = 3` in `fly.toml` | `test_shutdown_timeouts_nest_correctly` | **Fails** on `assert 3 > 20` |
| `Database._run` returns the live cursor instead of a materialised `Result` | `test_overlapping_runs_all_record_without_locking_errors` | **Fails ~50% of invocations** — see below |
| Same | `test_a_result_is_materialised_before_another_thread_touches_the_connection` | **Fails 5/5** |
| Same | entire `tests/test_db.py` | **Passes 3/3 — catches nothing** |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical coverage] Added a deterministic guard on the materialised-`Result` invariant**

- **Found during:** Task 2, while running the non-vacuity check the plan's own instructions demanded.
- **Issue:** Removing `Result` — handing callers a live `sqlite3.Cursor` to step after the lock drops — is the single failure mode `02-RESEARCH.md` measured and the reason the whole `Database` class exists. Under that mutation the six-way probe failed on only 4 of 8 standalone invocations (the exact `404: ticket not found` symptom research recorded), and **`tests/test_db.py` did not catch it at all**: its barrier test covers the *transaction boundary*, which the mutation leaves intact. The phase's central invariant was therefore guarded only by timing, on a test whose CI cost is one invocation.
- **Fix:** Added `test_a_result_is_materialised_before_another_thread_touches_the_connection` to `tests/test_lifecycle.py` — two threads on one file-backed `Database`, with `threading.Barrier` forcing the exact interleaving: the reader issues its `SELECT`, parks between issuing and reading, and the writer runs 20 inserts plus a commit on the same connection inside that window. Correct behaviour returns the one pre-existing row; under the mutation the reader's supposed point-in-time result comes back with 21 rows — the writer's inserts leaking into another caller's read (threat T-02-20, information disclosure across requests).
- **Files modified:** `tests/test_lifecycle.py` (in the plan's declared file set).
- **Commit:** `0d2ec7a`

**2. [Rule 2 - Test would under-exercise its hazard] Pre-warm the executor in the concurrency test**

- **Found during:** Task 2 mutation measurement.
- **Issue:** Every DB touch reaches SQLite through `asyncio.to_thread`. On a cold default executor the first six calls each pay for a thread being created, which staggers the runs enough that they queue rather than overlap — the test still passes, but stops exercising the hazard it exists for.
- **Fix:** `await asyncio.gather(*(asyncio.to_thread(bool) for _ in range(8)))` before driving the six runs, commented with why.
- **Files modified:** `tests/test_lifecycle.py`
- **Commit:** `0d2ec7a`

### Stale gate corrected

The plan's D-03 gate command names `8842c87` as the comparison base. That predates wave 1, so it reports `src/relay/ratelimit.py` as changed (legitimately, by Phase 1's WR-03 at `ae7b324`). All D-03 gates in this plan were run against **`db8eaf1`**, this plan's actual base. The gate prints nothing, and `git diff --name-only db8eaf1 -- src/relay/ratelimit.py` is also empty — WR-01's TOCTOU deferral is untouched.

## Observation, Not Fixed

Under the `deregister`-stubbed mutation, the fixture teardown raised `RuntimeError: <asyncio.locks.Event> is bound to a different event loop`. Cause: `test_a_cancelled_run_task_still_records_and_drains` drives the handler on an `asyncio.run` loop while the `client` fixture's lifespan runs on the `TestClient` portal loop, so `RunRegistry._idle.wait()` binds to whichever loop first *waits* on it. On the correct implementation `active` is 0 at both drain points, the fast path returns before touching the event, and nothing binds — hence 126/126 green. Production has a single loop, so this cannot occur there. Left alone deliberately: "fixing" it means editing `runs.py`, which is outside this plan's file set, and the symptom only appears when the registry is already broken.

## Deploy-Time Checks (cannot run in CI)

Recorded, not executed — `flyctl` is not available in this environment.

**New from this phase:**
1. After `fly deploy`, `fly config show` reports `kill_timeout = 30`.
2. `fly machine list` still reaches `stopped` when idle — the real-world counterpart to `test_registry_is_empty_after_a_run_completes` (D-06).
3. A `fly deploy` issued during an active run shows `shutdown.drain_complete` in the logs rather than a truncated stream.

**Still open from Phase 1 (`01-05-SUMMARY.md`):**
4. The live Fly proxy's `Fly-Client-IP` behaviour observed from two different networks.
5. That the README's `relay-demo-2026` literal matches what `fly secrets set RELAY_DEMO_KEY=...` actually sets — no code or test asserts that pairing.

## Deliberate Debt

**D-11 — stale type hints.** `src/relay/mcp_server.py` and `src/relay/evals.py` still annotate parameters as `sqlite3.Connection` when they now receive a `relay.db.Database`. The annotations are wrong; the runtime behaviour is correct, because `Database` covers the slice of the cursor API those files use. D-03 forbids editing either file in this phase, so the staleness is recorded rather than fixed. There is no type checker in CI, so nothing surfaces it — a future phase that touches `mcp_server.py` or `evals.py` for any other reason should correct the hints then.

**CI improvement not taken.** `02-RESEARCH.md` and `02-VALIDATION.md` both recommend adding `docker stop --time=35 relay` to the CI `docker` job and asserting exit code 0 plus `shutdown.drain_complete` in `docker logs`. It is the only automatable end-to-end check of the *signal path* — everything this phase added tests the drain mechanism, but nothing proves SIGTERM actually reaches uvicorn in a real container. The `exec` in the CMD removes the assumption at the source, and `test_shutdown_timeouts_nest_correctly` asserts it is there, but neither is a live observation. Not taken here because it is a CI-workflow change outside this plan's `files_modified`; it belongs with Phase 6's deployment work.

## Verification

```
pytest -q                                              → 126 passed, 0 failed
ruff check src tests                                   → All checks passed!
concurrency test × 5 consecutive                       → 5/5 passed
pytest tests/test_lifecycle.py -k draining             → 1 passed, 10 deselected
test_mid_stream_disconnect_still_records_the_spend     → passed (DATA-02-f)
python -c tomllib fly.toml                             → 30 False
grep -c 'exec uvicorn' Dockerfile                      → 1
grep -c -- '--timeout-graceful-shutdown 20' Dockerfile → 1
grep -c 'build_mcp_registry' tests/test_lifecycle.py   → 2
grep -c 'threading.get_ident' tests/test_lifecycle.py  → 2
grep -c 'time.sleep' tests/test_lifecycle.py           → 0
grep -c 'task.cancel()' tests/test_lifecycle.py        → 3
grep -c 'customer_email' tests/test_lifecycle.py       → 9
grep -c 'OperationalError' tests/test_lifecycle.py     → 0
grep -v '^\s*#' src/relay/agent.py | grep -c 'async with'                        → 0
grep -v '^\s*#' src/relay/main.py | grep -c 'app.middleware\|BaseHTTPMiddleware' → 0
D-03 gate (vs db8eaf1)                                 → prints nothing
git diff --name-only db8eaf1 -- src/relay/ratelimit.py → prints nothing
```

All 14 rows of `02-VALIDATION.md`'s verification map are green.

## Threat Register Outcomes

| Threat ID | Disposition | Outcome |
|-----------|-------------|---------|
| T-02-20 | mitigate | Row-content assertions across six overlapping runs, green 5/5; plus the new barrier test, which catches the exact leakage 5/5 where the probe caught it ~50% |
| T-02-21 | mitigate | Both registries covered; negative control confirmed |
| T-02-22 | mitigate | `30 > 20 > 5.0`, asserted through the TOML parser |
| T-02-23 | mitigate | `exec uvicorn` present and asserted |
| T-02-24 | mitigate | Registry-empty and never-started-stream tests green; `fly machine list` recorded as deploy-time |
| T-02-25 | mitigate | 503 detail asserted to carry `error` + `note` only |
| T-02-SC | accept | No packages installed; `pyproject.toml` untouched |

No new threat surface introduced — this plan adds tests and two deployment timeouts.

## Known Stubs

None.

## Self-Check: PASSED

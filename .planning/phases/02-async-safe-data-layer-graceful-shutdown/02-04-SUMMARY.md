---
phase: 02-async-safe-data-layer-graceful-shutdown
plan: 04
subsystem: api
tags: [fastapi, asyncio, sqlite, sse, graceful-shutdown, streamingresponse]

# Dependency graph
requires:
  - phase: 02-01
    provides: "Database with private connection, re-entrant lock, materialised Result, and transaction()"
  - phase: 02-02
    provides: "RunRegistry (register/deregister/active/snapshot/drain) and settings.shutdown_drain_seconds"
  - phase: 01-security-perimeter
    provides: "reserve_run/release_run reservations, enforce_daily_budget, the record_run-in-finally fix, structured-detail rejection convention"
provides:
  - "async _get_ticket whose SELECT and fetchone both run off the event loop"
  - "create_ticket as a single transaction, with lastrowid read inside the block"
  - "/metrics offloaded — the one read that grows unbounded"
  - "app.state.runs: a RunRegistry per app startup, drained before conn.close()"
  - "run registration inside event_stream's body, deregistration after release_run(token)"
  - "503 {\"error\": \"shutting_down\"} + Retry-After on POST /tickets/{id}/process while draining (D-09)"
affects: [02-05, 03-retrieval, 05-dashboard, 06-deployment]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "asyncio.to_thread as the single offload seam at the HTTP edge; executors below it stay sync"
    - "Registration of per-run state inside the generator body, so register/deregister are exactly balanced and need no TTL"
    - "Drain-before-close teardown ordering in lifespan"

key-files:
  created: []
  modified:
    - src/relay/main.py

key-decisions:
  - "enforce_daily_budget stays on the event loop deliberately — tens of rows behind idx_runs_created_at, and offloading it would move a read of ratelimit's in-process reservations onto a worker thread"
  - "register() goes inside event_stream's body, never beside reserve_run(), because a generator that never starts never runs its finally"
  - "The drain 503 carries only error + note, no active counts or timeouts (ASVS V13)"
  - "Retry-After is a flat 5s: this is a deploy window, not a computed reset like the daily ceiling's"
  - "The Phase 1 finally was preserved verbatim — plain finally, recorded guard, synchronous record_run — with deregister appended only"

patterns-established:
  - "Handler offload: every DB touch in main.py goes through asyncio.to_thread, with fetchone called inside the offloaded callable"
  - "Write path: an inner def _insert() holding Database.transaction(), offloaded as one unit, lastrowid read inside the block"
  - "Perimeter rejections use dict details (product copy); domain errors keep short strings"

requirements-completed: [DATA-01, DATA-02]

# Metrics
duration: 12min
completed: 2026-08-09
---

# Phase 02 Plan 04: HTTP Edge — Offload, Registry Wiring, Drain Summary

**Every handler DB call in `main.py` moved off the event loop via `asyncio.to_thread`, ticket creation became one transaction, and in-flight SSE runs are now tracked in `app.state.runs` and drained before the connection closes — with a 503 refusing new paid runs mid-drain.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-08-09T15:34Z
- **Completed:** 2026-08-09T15:46Z
- **Tasks:** 3
- **Files modified:** 1 (`src/relay/main.py`)

## Accomplishments

- `_get_ticket` is now `async`, running its SELECT **and** its `fetchone()` inside one offloaded callable — stepping the result back on the loop would put the read half a statement outside the thread that holds `Database`'s lock.
- `create_ticket` is a single unit of work: an inner `_insert()` closure holds `app.state.conn.transaction()`, reads `cur.lastrowid` **inside** the block, and the whole thing is offloaded, then re-read through `await _get_ticket(...)`.
- `/metrics` offloads `run_metrics` — `SELECT * FROM runs` is the one read that grows for the life of the Fly volume, and the dashboard polls it every 5s.
- `app.state.runs = RunRegistry()` is created per app startup and `await`ed to drain **before** `conn.close()`, so a cancelled stream's `finally` can still write the run's cost that SEC-03's daily ceiling reads back.
- `register(ticket_id=...)` is the first statement inside `event_stream`'s body; `deregister(run_token)` is the last statement of the existing `finally`, after `release_run(token)`.
- `POST /tickets/{id}/process` returns 503 `{"error": "shutting_down", "note": ...}` with `Retry-After: 5` while draining — raised after the 409 and before `reserve_run()`.

## Task Commits

1. **Task 1: Offload every handler DB call and make ticket creation one transaction** — `70f2377` (refactor)
2. **Task 2: Registry wiring — register inside the generator, drain before close** — `7fcf99c` (feat)
3. **Task 3: Refuse new paid runs while draining (D-09)** — `fcc0ae3` (feat)

## Files Created/Modified

- `src/relay/main.py` — async `_get_ticket`, offloaded `create_ticket`/`/metrics`, `RunRegistry` on `app.state`, drain-before-close in `lifespan`, in-generator registration, D-09 503.

## Decisions Made

- **`enforce_daily_budget` stays on the event loop, by decision, with a comment at the call site saying so.** It sums tens of rows behind plan 02-01's `idx_runs_created_at`, so contention is microseconds; offloading it would also move a read of `ratelimit._reservations` — Phase 1 state this phase is told not to disturb — onto a worker thread. This is the single remaining synchronous DB touch in `main.py`.
- **WR-01 (the TOCTOU between the budget check and the reservation) was deliberately left in place.** It lives in exactly this code and is deferred to gap closure; the ordering of `enforce_daily_budget`, `enforce`, and `reserve_run` is byte-identical to the base.
- **`Retry-After: 5` is a flat literal**, unlike the daily ceiling's computed seconds-to-midnight. A drain window is bounded by `shutdown_drain_seconds`, and echoing that value back would leak a timeout to the caller (T-02-17).
- **No drain check on `create_ticket`/`get_ticket`** — they are cheap and unpaid, and widening the refusal surface is scope this phase did not buy.

## Deviations from Plan

None — plan executed exactly as written. No deviation rules fired; no auto-fixes were needed.

## Issues Encountered

None. One note on a verification gate: the plan's `git diff 8842c87 -- src/relay/ratelimit.py` gate is **non-empty**, but not because of this plan. Wave 1 landed `ae7b324` (WR-03, proxy-header validation) on that file between `8842c87` and this plan's actual base `a0b73fe`. Against the real base the diff is empty:

```
git diff a0b73fe -- src/relay/ratelimit.py src/relay/auth.py   →  (empty)
git diff --name-only a0b73fe                                    →  src/relay/main.py
```

Only `main.py` was touched. The sibling-ownership gate (`tools.py`, `telemetry.py`, `agent.py`, `tests/helpers.py`) and the D-03 gate (`mcp_server.py`, `evals.py`, and all nine existing test files) both print nothing.

## Verification

| Gate | Result |
|------|--------|
| `pytest -q` | **117 passed**, 0 failed (baseline 117; floor 110) |
| `ruff check src tests` | `All checks passed!` |
| `test_mid_stream_disconnect_still_records_the_spend` | passes (D-07 / CR-01 regression) |
| `tests/test_ratelimit.py` | 39 passed, **unedited** (CR-02 reservation TTL intact) |
| `inspect` — drain precedes `conn.close()` | `order ok` |
| `inspect` — `reserve_run()` < `async def event_stream` < `register` | `placement ok` |
| `inspect` — `release_run(token)` < `deregister`, `if not recorded:` present | `finally ok` |
| `inspect` — drain check after 409, before `reserve_run()` | `order ok` |
| `grep -c 'app.middleware\|BaseHTTPMiddleware'` | `0` |
| `grep -c 'await record_run\|asyncio.to_thread(record_run')` | `0` |
| `grep -c 'async with'` (non-comment) | `0` |
| `await asyncio.to_thread` / `await _get_ticket(` / `transaction()` | 3 / 3 / 1 |

A throwaway (uncommitted, scratchpad-only) behavioural probe confirmed the D-09 path end-to-end before commit: status `503`, detail keys exactly `['error', 'note']`, `Retry-After: 5`, and `reserved_usd() == 0` after the refusal — proving the rejection claims no spend. The registry read `active == 0` on an idle server (D-06). The committed test for this lands in plan 02-05.

## Known Stubs

None.

## Threat Flags

None. The one new surface — the D-09 503 — is already in this plan's threat register (T-02-16 admission refusal, T-02-17 response-body disclosure) and was implemented to that disposition: the detail dict carries no active-run counts, ticket ids, or timeout values.

## Next Phase Readiness

**This plan's own gate is no-regression, which it meets. The new behaviours are gated by plan 02-05's integration tests in wave 3** — specifically:

- `test_a_stream_that_never_starts_registers_nothing` (T-02-15, the in-generator registration)
- the drain-before-close ordering under a real in-flight run (T-02-14)
- the 503-while-draining response shape (D-09), whose assertion shape copies `test_daily_budget_503`

Two carried notes for later plans:

- **D-11:** `mcp_server.py` and `evals.py` still carry `sqlite3.Connection` type hints that are now stale — they receive a `Database`. D-03 forbids editing them in this phase; recorded here deliberately so a later phase corrects them rather than a reader assuming the annotation is accurate.
- **Deployment (phase 6 / plan 02-06):** the drain is only useful if the platform grants time for it. RESEARCH.md §6 specifies `kill_timeout = 30` in `fly.toml` and `--timeout-graceful-shutdown 20` on uvicorn; without those, Fly's 5s default SIGKILLs the machine and skips the drain this plan just wired.

## Self-Check: PASSED

- `src/relay/main.py` — FOUND
- `.planning/phases/02-async-safe-data-layer-graceful-shutdown/02-04-SUMMARY.md` — FOUND
- Commits `70f2377`, `7fcf99c`, `fcc0ae3` — all FOUND in `git log`
- STATE.md / ROADMAP.md — untouched (orchestrator owns those writes)

---
*Phase: 02-async-safe-data-layer-graceful-shutdown*
*Completed: 2026-08-09*

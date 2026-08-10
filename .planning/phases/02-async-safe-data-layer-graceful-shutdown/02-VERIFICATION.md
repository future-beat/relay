---
phase: 02-async-safe-data-layer-graceful-shutdown
verified: 2026-08-10T02:10:00Z
status: passed
score: 4/4 roadmap success criteria verified (plus DATA-01/DATA-02 truths)
overrides_applied: 0
re_verification: false
---

# Phase 2: Async-Safe Data Layer & Graceful Shutdown Verification Report

**Phase Goal:** Concurrent runs and deploy restarts no longer block the event loop, corrupt connection state, or lose run records
**Verified:** 2026-08-10
**Status:** passed
**Re-verification:** No — initial verification

## Method

This phase had a code review (`02-REVIEW.md`, 1 Critical / 7 Warning / 8 Info) followed by a
fix cycle and an explicit user decision to defer the remaining Warning/Info items
(`02-DEFERRED.md`). Rather than re-litigate the review, this verification:

1. Read the final state of every file the review and plans touched.
2. Ran the full suite and `ruff` myself rather than trusting the SUMMARY-reported baseline.
3. **Mutation-tested the four fixes claimed since the review** (CR-01, WR-02, WR-03, WR-06) by
   reverting each one in isolation and confirming the relevant test(s) fail, then restoring the
   file and confirming the suite returns to green. This is the highest-value check this phase
   asked for — "would the test pass with the behaviour removed" — and I ran it as an experiment,
   not a read of the diff.
4. Cross-checked every deferred item (WR-01/04/05/07, 8 Info) against the current code to confirm
   it is genuinely still present (not silently fixed or silently reintroduced beyond what was
   reviewed) and to independently judge whether any of them actually falsifies DATA-01/DATA-02
   rather than being pure hardening.

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Multiple tickets processed concurrently all stream normally, no `SQLite objects created in a thread` errors, no stalled streams | VERIFIED | `tests/test_lifecycle.py::test_overlapping_runs_all_record_without_locking_errors` drives 6 concurrent `process_ticket` calls through `TestClient`'s real ASGI stack, asserts per-row content (not just "no exception") — the exact invariant research measured breaking silently (`customer_email=None`, `status=''`) on 4/5 runs pre-fix. Passes. |
| 2 | The suite and MCP server still work against the unchanged sync `ToolSpec.execute` contract (no executor is a coroutine function) | VERIFIED | Full suite: **135 passed**, `ruff check src tests`: **all checks passed** (baseline claim of 135 independently reproduced, not taken on faith). `tests/test_mcp.py`: **8 passed** unchanged. `test_no_registered_executor_is_a_coroutine_function` asserts `inspect.iscoroutinefunction` is false for every entry in both the agent registry and `build_mcp_registry`. `mcp_server.py`/`evals.py` have zero phase-2 commits touching them (`git log --stat` confirms), matching D-03. |
| 3 | Sending SIGTERM during an in-flight run lets that run finish streaming before the database closes, instead of erroring mid-stream | VERIFIED | Three tests exercise this, and I confirmed by mutation that two of them are load-bearing, not just present: `test_lifespan_drains_before_it_closes_the_connection` and `test_lifespan_shutdown_lets_an_in_flight_run_finish_writing_its_row` drive the **real** `lifespan()` (not a standalone `RunRegistry`). Deleting `await app.state.runs.drain(...)` from `lifespan` in `main.py` and re-running `tests/test_lifecycle.py` **fails both tests** (`ProgrammingError: Cannot operate on a closed database` — Pitfall 5, exactly). `test_a_cancelled_run_task_still_records_and_drains` additionally drives the actual uvicorn shutdown mechanism (`task.cancel()`, not `body_iterator.aclose()`) and asserts the row lands with `outcome="incomplete"` and `cost_usd > 0`. This closes the review's WR-02 finding (headline behaviour was previously untested — 126/126 green with the drain deleted). A literal SIGTERM-to-a-running-process test is deploy-time and out of CI's reach; `test_shutdown_timeouts_nest_correctly` pins the three-way nesting (`fly.toml kill_timeout=30 > Dockerfile --timeout-graceful-shutdown=20 > settings.shutdown_drain_seconds=5.0`) that makes the drain reachable in production, parsed with `tomllib`/`re`, not string-guessed. |
| 4 | A run interrupted by client disconnect or shutdown still appears in `runs` with its cost and outcome recorded | VERIFIED | `test_mid_stream_disconnect_still_records_the_spend` (Phase 1's CR-01, preserved per D-07) still passes. `test_a_cancelled_run_task_still_records_and_drains` covers the shutdown half end-to-end. The CR-01 (this phase's) fix — nesting `record_run`/`release_run`/`deregister` in `main.py`'s `finally` so a `record_run` exception can't skip the other two — is proven load-bearing: reverting the nesting to the pre-fix sequential form and re-running `test_a_failed_record_run_still_releases_the_reservation_and_the_registry` **fails** (`registry active after: 1`, matching the review's own probe output). Restored, suite is green again. |

**Score:** 4/4 roadmap success criteria verified.

### DATA-01 / DATA-02 Requirement Truths

| Requirement | Claim | Status | Evidence |
|---|---|---|---|
| DATA-01 | All SQLite access is async-safe; sync `ToolSpec.execute` contract preserved | VERIFIED | `Database`/`Result` in `src/relay/db.py` privatize the connection behind an `RLock` and materialise every result before the lock releases (`test_a_result_is_materialised_before_another_thread_touches_the_connection` forces the interleaving with `threading.Barrier`, not a sleep — this is the review's self-identified prior gap, now closed). WAL + `busy_timeout=5000` + `foreign_keys=ON` on file DBs, confirmed a silent no-op on `:memory:` and asserted as such (`test_wal_is_a_silent_no_op_on_memory_databases`). The single `asyncio.to_thread` seam is at `agent.py:203-207` around `_execute_guarded`; `test_tool_execution_runs_off_the_event_loop` proves it by thread-identity, not timing. All five multi-statement writers (`send_reply`, `create_escalation`, `set_category`, `record_run`, ticket creation) use `db.transaction()` — adoption (not just the primitive) is tested and mutation-confirmed load-bearing. |
| DATA-02 | Graceful shutdown drains in-flight SSE runs before closing the DB; `record_run` persists on interruption | VERIFIED | See SC-3/SC-4 above. `RunRegistry.drain()` in `src/relay/runs.py` is event-driven (`asyncio.wait_for(self._idle.wait(), ...)`), wired into `lifespan()` before `conn.close()`, and mutation-confirmed load-bearing at the wiring level (not just the primitive level). `POST /tickets/{id}/process` returns 503 while draining (`test_process_returns_503_while_draining`), refused before `reserve_run()` so no spend is claimed. |

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/relay/db.py` | `Result`, `Database` (RLock + materialised results + `transaction()`), pragmas, `idx_runs_created_at` | VERIFIED | All present; `class Database` at line 114, `transaction()` at 153, index at 67. |
| `src/relay/runs.py` | `ActiveRun`, `RunRegistry` (register/deregister/active/snapshot/drain/draining) | VERIFIED | All members present, `min_lines` satisfied (120 lines). |
| `src/relay/config.py` | `shutdown_drain_seconds` setting | VERIFIED | Present, `RELAY_SHUTDOWN_DRAIN_SECONDS`, `.env.example` updated. |
| `src/relay/tools.py` | Writers wrapped in `db.transaction()` | VERIFIED, adoption mutation-tested | `send_reply`, `create_escalation`, `set_category` all use `with db.transaction()`. |
| `src/relay/telemetry.py` | `record_run` wrapped in a transaction, still sync | VERIFIED, adoption mutation-tested | `with conn.transaction():` at line 71. |
| `src/relay/agent.py` | Single `asyncio.to_thread` offload seam | VERIFIED | One seam at `_execute_guarded` call site; `async with` count in file is 0 (no context manager spans a yield). |
| `src/relay/main.py` | Offloaded handlers, registry wiring, drain-before-close, 503-while-draining, CR-01 nesting | VERIFIED | All present and mutation-confirmed at the two riskiest points (drain-before-close, CR-01 nesting). |
| `fly.toml` | `kill_timeout = 30`, no `kill_signal` | VERIFIED | Confirmed by grep and by `test_shutdown_timeouts_nest_correctly` parsing the real TOML. |
| `Dockerfile` | exec-form CMD, `--timeout-graceful-shutdown 20` | VERIFIED | Confirmed by grep and by the same test. |
| `tests/test_db.py`, `tests/test_lifecycle.py` | DATA-01/DATA-02 coverage | VERIFIED | Present, and the specific tests named in the review as previously vacuous or missing are now genuinely load-bearing (mutation-confirmed above). |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `db.py:connect` | `Database` | returns wrapper, never raw connection | VERIFIED | `return Database(conn)` at `db.py:181`. |
| `lifespan` | `app.state.runs.drain` | awaited before `conn.close()` | VERIFIED, mutation-confirmed | `main.py:44-45`; removing/reordering fails the two lifespan-integration tests. |
| `event_stream` | `app.state.runs.register` | first statement inside generator body | VERIFIED | `main.py:188`, confirmed by `test_a_stream_that_never_starts_registers_nothing`. |
| `event_stream finally` | `app.state.runs.deregister` | nested so a `record_run` failure can't skip it | VERIFIED, mutation-confirmed | `main.py:228-248`; reverting the nesting fails `test_a_failed_record_run_still_releases_the_reservation_and_the_registry`. |
| `agent.py:run_ticket` | `_execute_guarded` | `await asyncio.to_thread` inside the tool span | VERIFIED | `agent.py:203-207`, mutation-confirmed by the review (replacing with a direct call fails `test_tool_execution_runs_off_the_event_loop`) and re-confirmed present in the final code. |

### Anti-Patterns Found

None. Scanned every file this phase touched (`db.py`, `runs.py`, `main.py`, `agent.py`,
`tools.py`, `telemetry.py`, `config.py`, `ratelimit.py`, `test_db.py`, `test_lifecycle.py`,
`conftest.py`, `helpers.py`) for `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` — zero hits.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|---|---|---|---|---|
| DATA-01 | 02-01, 02-03, 02-04, 02-05 | Async-safe SQLite access, sync executor contract preserved | SATISFIED | See table above. |
| DATA-02 | 02-02, 02-04, 02-05 | Graceful shutdown drain, interrupted runs still recorded | SATISFIED | See table above. |

`.planning/REQUIREMENTS.md` still shows `- [ ]` checkboxes for DATA-01/DATA-02 — a documentation
bookkeeping gap, not a code gap; both are functionally satisfied. Worth flipping to `[x]` as part
of closing this phase out, but it does not block the goal and I'm not treating it as a truth
failure.

## Independent Judgment on Deferred Items (per task instructions)

`02-DEFERRED.md` records WR-01, WR-04, WR-05, WR-07, and 8 unitemized Info findings as
deliberately deferred by user decision. I re-checked each against the current code (all
confirmed still present, none silently fixed or silently worsened) and independently judged
whether any of them actually falsifies DATA-01 or DATA-02 for **this phase's stated scope**
(single-process, single-instance HTTP deployment):

- **WR-01** (`transaction()` not nest-safe): no call site nests today (confirmed by reading all
  five writers). Latent, not live. Does not falsify DATA-01 as currently exercised. Correctly
  scoped as "fix before Phase 5" since `run_events` is the first writer likely to nest.
- **WR-04** (`register()` doesn't consult `draining`): the window requires a request that both
  passes the handler's `draining` check *and* a `drain()` fast-path return *and* a `conn.close()`
  all landing between the handler returning and the generator body starting — narrower in
  practice than it reads, since uvicorn stops accepting new connections before lifespan shutdown
  runs. Weakens the registry's own internal contract but does not, in the single-lifespan
  production topology this phase targets, produce an observable violation of SC-3/SC-4. Correctly
  deferred as hardening.
- **WR-05** (`_idle` binds to first loop): only reachable with multiple lifespans in one process
  (multiple `TestClient` contexts, or restarting the app in-process). Production has one lifespan
  for the life of the machine. Does not falsify DATA-02 for the deployed topology. Correctly
  deferred, and its primary consequence (masked by CR-01 removing the fast path) is now the
  *only* live escalation path per the review, which is a Phase-5-relevant risk, not a Phase-2 one.
- **WR-07** (stale `sqlite3.Connection` type hints in `ratelimit.py`): confirmed still present
  (`ratelimit.py:147,196`, `import sqlite3` at `:19`). Purely a documentation-accuracy defect —
  both functions are actually called with a `Database` and work correctly (confirmed by
  `test_the_daily_budget_read_runs_off_the_event_loop` passing and by manual read of every
  connection method `Database` exposes vs. what `ratelimit.py` calls). No functional gap.
- **8 Info findings**: spot-checked IN-03 (no test for `idx_runs_created_at`) — confirmed still
  untested, purely a coverage gap on an index that does exist and is used correctly. None of the
  Info findings touch DATA-01/DATA-02's observable behavior.

**My independent conclusion:** none of the deferred items falsify DATA-01 or DATA-02 as stated
for this phase's scope. They are legitimate hardening debt correctly routed to gap closure, not
gaps in the phase-2 goal itself.

## Behavioral / Probe Notes

No project probe scripts exist (`scripts/*/tests/probe-*.sh` — none found; only `scripts/demo.sh`,
a manual smoke script). Step 7c is not applicable. A literal `fly deploy` during an active run, and
`fly config show`/`fly machine list` checks, remain deploy-time and out of CI's reach — already
correctly logged in `02-DEFERRED.md` as "known-unverified, deploy-time" and not treated as gaps
here, per task instructions.

## Baseline Reproduction

Independently reproduced rather than trusted:

```
pytest -q        -> 135 passed
ruff check src tests -> All checks passed!
pytest -q tests/test_mcp.py -> 8 passed
```

Working tree confirmed clean (`git status`) after all mutation experiments were reverted.

### Human Verification Required

None. Every must-have for this phase is verifiable in CI (unit/integration tests, static
grep/parse checks) or was independently mutation-tested by this verification. The three
deploy-time items (`fly config show` kill_timeout, live drain log line during `fly deploy`,
`fly machine list` reaching `stopped`) are pre-existing, correctly-logged deferred items from
`02-DEFERRED.md`, not new human-verification asks — they don't block phase completion because
Phase 2's goal is about the code's behavior, and every code-observable half of that behavior
(the drain executing, the timeouts nesting correctly, the row surviving) has automated coverage.

### Gaps Summary

No gaps found. All four ROADMAP success criteria are genuinely met in the final code, not just
claimed in SUMMARY.md — I confirmed this by reverting each of the four fixes landed since the
code review (CR-01, WR-02, WR-03, WR-06) one at a time and watching the corresponding tests fail,
then restoring and re-confirming the suite is green (135 passed, ruff clean, working tree clean).
The remaining deferred items (WR-01, WR-04, WR-05, WR-07, 8 Info findings) are real but are
hardening/documentation debt correctly routed to a future gap-closure phase — I independently
verified none of them produces an observable violation of DATA-01 or DATA-02 in this phase's
single-process, single-lifespan deployment topology.

---

_Verified: 2026-08-10_
_Verifier: Claude (gsd-verifier)_

---
phase: 05-run-event-persistence-live-feed
plan: 01
subsystem: database
tags: [sqlite, migration, schema, telemetry, config, sse]

# Dependency graph
requires:
  - phase: 02-async-safe-data-layer-graceful-shutdown
    provides: "Database wrapper, nest-safe transaction(), the to_thread offload seam that later plans write run_events through"
provides:
  - "run_events table (id, run_uid, ticket_id, seq, type, payload, created_at) + idx_run_events_run_uid"
  - "runs.run_uid column, added by a guarded idempotent ALTER TABLE in init_db"
  - "record_run(..., run_uid=None) stamping the summary row with the run's uid"
  - "events_queue_maxsize / events_heartbeat_seconds / events_idle_seconds settings"
affects: [05-02 recorder, 05-03 broker, 05-04 /events endpoint, phase-06 dashboard drill-down]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PRAGMA table_info guard before ALTER TABLE ADD COLUMN — the repo's first schema migration idiom"
key-files:
  created:
    - tests/test_run_events.py
  modified:
    - src/relay/db.py
    - src/relay/telemetry.py
    - src/relay/config.py

key-decisions:
  - "runs.run_uid is added by a PRAGMA-guarded ALTER TABLE, never by editing the CREATE TABLE runs DDL — IF NOT EXISTS is a silent no-op on the populated live Fly volume (D-13)"
  - "run_events.run_uid is a soft join key, not a foreign key: the runs row is inserted at end of stream, long after the first event row, so a real FK would fail on every insert"
  - "payload stores RAW event data; redaction happens only at the public /events boundary (D-01/D-07)"
  - "record_run's run_uid defaults to None so evals.py, mcp_server.py and direct test callers stay untouched"

patterns-established:
  - "Guarded, idempotent column migration in init_db: read PRAGMA table_info, ALTER only when absent"
  - "Every new index carries an inline comment naming the query that justifies it (matches idx_runs_created_at)"

requirements-completed: [DATA-03]

# Metrics
duration: 14min
completed: 2026-08-12
---

# Phase 5 Plan 01: Run-Event Storage Foundation Summary

**`run_events` table plus a PRAGMA-guarded idempotent `ALTER TABLE` that actually adds `runs.run_uid` on the populated live volume, `record_run` stamping that uid, and three defaulted live-feed settings.**

## Performance

- **Duration:** ~14 min
- **Tasks:** 3 (2 TDD)
- **Files modified:** 4 (3 source, 1 new test file)
- **Suite:** 292 passed (floor 288); `ruff check src tests` clean

## Accomplishments

- `run_events` created by `SCHEMA` with the raw-payload column set and `idx_run_events_run_uid` (Phase 6 drill-down join), following the existing `runs`/`idx_runs_created_at` DDL conventions.
- `init_db` now performs the repo's first real migration: `PRAGMA table_info(runs)` → `ALTER TABLE runs ADD COLUMN run_uid TEXT` only when absent. This is the plan's central trap — a `CREATE TABLE IF NOT EXISTS` carrying `run_uid` would never add the column to the already-existing production `runs` table, failing silently in production only.
- `record_run` gained keyword-only `run_uid: str | None = None` and the matching INSERT column; omitting it stores NULL, so every existing caller is unbroken.
- Three defaulted settings (`events_queue_maxsize=256`, `events_heartbeat_seconds=15.0`, `events_idle_seconds=300.0`) with "why this default" comments; no new secret or env requirement.

## Task Commits

1. **Task 1: run_events + guarded run_uid migration** — `6343281` (test, RED) → `780f1fb` (feat, GREEN)
2. **Task 2: record_run stamps run_uid** — `00d12d7` (test, RED) → `c507d4d` (feat, GREEN)
3. **Task 3: broker/heartbeat/idle settings** — `ff8f1dd` (feat)

## Mutation Testing (all run, all confirmed red)

| Test | Mutation applied | Result |
|------|------------------|--------|
| `test_run_uid_migration_is_idempotent` | Removed the `PRAGMA table_info(runs)` guard, ran a bare unconditional `ALTER TABLE runs ADD COLUMN run_uid TEXT` | **FAILED** — `sqlite3.OperationalError: duplicate column name: run_uid` on the second `init_db`. Restored; green again. |
| `test_record_run_persists_run_uid` | Dropped `run_uid` from the INSERT column list, left it in the values tuple | **FAILED** — `sqlite3.ProgrammingError` arity mismatch (also took `test_record_run_without_run_uid_still_works` red). Restored; green again. |

`test_run_events_table_shape` is an exact-set assertion (`cols == {...}`), so any missing or renamed column fails it; it was red before the DDL existed.

Note on `test_record_run_without_run_uid_still_works`: it is a regression guard, not a new behavior — it passes on the un-mutated pre-Task-2 code (the column exists after Task 1 and defaults to NULL). Its falsifying mutation is the same INSERT-arity one above. Stated plainly rather than claimed as independent proof.

## Files Created/Modified

- `src/relay/db.py` — `run_events` DDL + `idx_run_events_run_uid` in `SCHEMA`; guarded `ALTER TABLE` in `init_db` between `executescript` and the seed logic. The `runs` CREATE TABLE block is byte-unchanged, as required.
- `src/relay/telemetry.py` — `record_run` signature + INSERT gain `run_uid`.
- `src/relay/config.py` — "Run-event live feed (phase 5)" settings block.
- `tests/test_run_events.py` — new; four tests covering migration idempotency, table shape, and both `record_run` paths.

## Decisions Made

Followed the plan as specified. Two notes worth carrying forward:

- The idle-ceiling comment records that the deadline must reset on **real frames only, never on heartbeats** — otherwise the server's own keep-alive keeps the machine awake forever, defeating D-09. Plan 05-04 must implement it that way.
- `run_events.payload` is documented in-schema as raw, with redaction stated as a public-boundary concern. This is the note that should stop a later plan from "helpfully" redacting at write time and starving Phase 6 drill-down.

## Deviations from Plan

None — plan executed exactly as written. No auto-fixes were needed.

## Issues Encountered

The plan's single test file spans Tasks 1 and 2. To keep each commit atomic and green, the Task-2 tests were held back from the Task-1 commit and appended in the Task-2 RED commit, rather than committing a file with known-failing tests for work not yet started.

## Constraint Compliance

- Frozen files byte-unchanged: `git diff --quiet HEAD -- src/relay/mcp_server.py src/relay/evals.py .github/workflows/ci.yml` passes.
- `src/relay/agent.py` and `src/relay/main.py` untouched (waves 3/4 own them) — asserted in the same diff check.
- No retrieval code path is exercised by the new tests, so no Voyage key pinning was needed and no paid call is possible; the suite-wide `_no_outbound_http` fixture remains the backstop.
- `STATE.md` / `ROADMAP.md` not modified by this executor.

## User Setup Required

None automated here. One manual post-deploy check belongs in `05-VALIDATION.md`: after the next Fly deploy, `sqlite3 /data/relay.db '.schema runs'` must show `run_uid` on the live volume. That is the only place the migration's real target is observable.

## Next Phase Readiness

Schema, join key and settings all exist, so the recorder (05-02), broker (05-03) and `/events` (05-04) plans have their foundation. No blockers.

## Self-Check: PASSED

All four modified/created source files exist on disk; all five task commits (`6343281`, `780f1fb`, `00d12d7`, `c507d4d`, `ff8f1dd`) resolve in `git log`.

---
*Phase: 05-run-event-persistence-live-feed*
*Completed: 2026-08-12*

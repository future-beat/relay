---
phase: 02-async-safe-data-layer-graceful-shutdown
plan: 01
subsystem: storage
tags: [sqlite, concurrency, threading, wal, transactions, pragmas]
status: complete
requires:
  - "nothing — wave 1, no upstream plan dependencies"
provides:
  - "relay.db.Database — private connection owner, threading.RLock, materialised results, transaction() context manager"
  - "relay.db.Result — materialised cursor slice (fetchone/fetchall/fetchmany/__iter__/lastrowid/rowcount/description)"
  - "connect() -> Database with journal_mode=WAL, busy_timeout=5000, foreign_keys=ON"
  - "idx_runs_created_at, self-applying through init_db's executescript (D-10)"
  - "tests/conftest.py `db` fixture — file-backed, the only fixture where WAL assertions are non-vacuous"
affects:
  - "plan 02-02 and everything downstream that offloads DB work to asyncio.to_thread — the wrapper is what makes that safe rather than a regression"
  - "the five multi-statement writers (tools.create_escalation/send_reply/set_category, main.create_ticket, telemetry.record_run) can now be wrapped in transaction()"
  - "phase 5's run_events table inherits the same connection-ownership contract"
tech-stack:
  added: []
  patterns:
    - "materialise-inside-the-lock: fetchall() before the lock releases, return a value object, never a live sqlite3.Cursor"
    - "re-entrant lock so a transaction() block can call execute() from inside its own critical section"
    - "except BaseException in the transaction unwind so a CancelledError cannot escape with the transaction still open"
    - "barrier-forced concurrency tests asserting on row contents, never on the absence of an exception"
key-files:
  created:
    - tests/test_db.py
  modified:
    - src/relay/db.py
    - tests/conftest.py
key-decisions:
  - "Result is a plain __slots__ class, not a dataclass — it is a hot-path value object constructed on every single statement, and it covers exactly the six-method/four-attribute contract the audit found, nothing more"
  - "connect() returns Database unconditionally, including for :memory: and for mcp_server.py/evals.py — that uniformity is precisely what keeps those two D-03-protected files working with zero edits"
  - "threading.RLock, not Lock — transaction() calls execute() re-entrantly and a plain Lock self-deadlocks on the first statement inside a transaction block"
  - "No sqlite3.Connection.autocommit anywhere — it is 3.12+ and pyproject declares requires-python >=3.11; transaction() relies on legacy implicit-BEGIN control"
  - "init_db keeps its parameter name `conn` despite now taking a Database — every call site passes positionally and two of them are D-03-protected"
  - "The concurrency test's second barrier carries a 0.5s timeout, and that timeout is the assertion rather than a delay: a correct transaction() leaves thread B blocked on the lock, so the rendezvous is unreachable. See the deviation below for why the first, timeout-free version was not a real test"
requirements-completed: [DATA-01]
metrics:
  duration: ~12 min
  tasks-completed: 2
  files-changed: 3
  tests-added: 4
  suite: 114 passed (from a 110 baseline)
  completed: 2026-08-09
---

# Phase 2 Plan 01: Async-Safe Data Layer — Storage Foundation Summary

`connect()` now hands back a `Database` that owns the SQLite connection privately behind a re-entrant lock, materialises every query result before releasing that lock, and exposes `transaction()` as the only way to group statements — closing the silent row-corruption and cross-request-commit hazards before wave 2 hands the connection to worker threads.

## What Was Built

**`src/relay/db.py`** — rewritten around two new classes, with the public function names and the `init_db(conn)` parameter name unchanged so no consumer needed editing:

- `Result` — a materialised query result (`__slots__` of `_i`, `_rows`, `description`, `lastrowid`, `rowcount`) implementing `fetchone`, `fetchall`, `fetchmany(size=1)`, and `__iter__`. It holds a list of `sqlite3.Row` captured at construction and never holds a cursor.
- `Database` — sole owner of the process's `sqlite3.Connection` (`self._conn`, private) plus a `threading.RLock` (`self._lock`). `_run(method, *args)` takes the lock, invokes the named connection method, and builds the `Result` **before** releasing. `execute`/`executemany`/`executescript` are built on `_run`; `commit`/`rollback`/`close` each take the lock.
- `Database.transaction()` — a `@contextmanager` holding the lock for the whole block, committing on clean exit and rolling back on `BaseException`.
- `connect()` — returns `Database`, sets `row_factory`, and issues `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout = 5000`, `PRAGMA foreign_keys = ON` one per `execute` in the existing style.
- `SCHEMA` gained `CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);` (D-10), self-applying via `init_db`'s `executescript`.

**`tests/conftest.py`** — one appended `db` fixture, file-backed at `tmp_path / "relay.db"`. `_reset_limits`, `conn`, `registry`, and `client` were not touched.

**`tests/test_db.py`** — four tests covering DATA-01-a..d: the three pragmas reading back on a real file, the `:memory:` WAL no-op pinned as a trap, foreign-key enforcement surviving the wrapper, and the barrier-forced partial-write isolation test.

## Key Implementation Details

The load-bearing detail is that `_run` calls `cur.fetchall()` *inside* the `with self._lock` block. Returning the live `sqlite3.Cursor` passes every single-threaded test and is still wrong: the caller steps it after the lock is released, so another thread's statement interleaves with an in-flight read. Research A/B-measured that variant failing 4 of 5 concurrent runs, and the failures were `customer_email=None` and `status=''` — **not** `OperationalError: database is locked`. Verified in place:

```
python -c "import inspect,relay.db as d; s=inspect.getsource(d.Database._run); assert 'fetchall' in s and 'self._lock' in s"  → exit 0
```

The API audit that bounded `Result`'s surface came back as exactly `execute` (28 sites), `commit` (10), `close` (3), `executescript` (1), `executemany` (1), plus `.lastrowid`, `.fetchone()`, `.fetchall()`, and iteration. No `with conn:`, no `.cursor()`, no `isolation_level`. `Result` covers that and stops.

## How to Verify

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q          # 114 passed
.venv/bin/ruff check src tests                                # All checks passed!
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q tests/test_db.py -x   # 4 passed, x5 consecutive
git diff --name-only 4b91681 -- src/relay/mcp_server.py src/relay/evals.py tests/test_*.py  # only tests/test_db.py
```

Suite went 110 → 114 with zero edits to any pre-existing test. `git diff 4b91681 -- tests/conftest.py | grep -c '^-[^-]'` is `0` — the conftest change is append-only.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] The first version of the concurrency test was vacuous — it passed against a deliberately broken `transaction()`**

- **Found during:** Task 2, while running an A/B check to prove the new test actually discriminated.
- **Issue:** The plan specified a single `threading.Barrier` to sequence the two threads. Implemented literally — thread A inserts, waits at the barrier, then raises — the barrier does not force the damaging interleaving. After the barrier releases, A only has to `raise` and roll back while B has to execute *and* commit, so A's rollback wins the race and the row is correctly discarded. Running that test against a `transaction()` monkeypatched to drop the lock between statements (the exact Pitfall 3 bug) produced `replies: [2]` and **passed**. A test that passes against the bug it exists to catch is worse than no test: it would have signed off wave 2's `to_thread` offload.
- **Fix:** Split the rendezvous in two. `inserted` releases B only once A's transaction is open with a row in it; `committed` is where A waits for B's commit, with a 0.5s timeout. The timeout is the assertion, not a delay — with a correct `transaction()` B is blocked on the lock and can never reach that rendezvous, so the barrier breaks, A raises, and the rollback discards its row. With a lock-dropping `transaction()` B reaches it in microseconds, A's rollback becomes a no-op, and the surviving rows are `[1, 2]`. Re-ran the A/B: naive variant now fails with exactly Pitfall 3's documented signature (`replies: [1, 2] expected: [2]`), real implementation passes 5 for 5.
- **Files modified:** `tests/test_db.py`
- **Commit:** `3e098e8`

**2. [Rule 3 - Blocking] Comment wording tripped its own acceptance gate**

- **Found during:** Task 2 verification.
- **Issue:** The test comment read "Barriers, never time.sleep", which made `grep -c 'time.sleep' tests/test_db.py` return `1` against a required `0`.
- **Fix:** Reworded to "never a sleep-and-hope". Same meaning, gate satisfied honestly rather than by weakening the gate.
- **Files modified:** `tests/test_db.py`
- **Commit:** `3e098e8`

### Deliberate Non-Changes

**D-11 recorded explicitly:** `src/relay/mcp_server.py` and `src/relay/evals.py` both still annotate their connection as `sqlite3.Connection` (`mcp_server.py` `amain()`, `evals.py`'s harness). Those hints are now **stale** — `connect()` returns a `Database`. They were left that way deliberately: D-03 forbids touching either file, and both work unedited because `Database` answers `execute`/`commit`/`close`/`executescript`/`executemany` identically and their `cur.lastrowid` reads land on `Result`. There is no runtime consequence (no mypy in CI); this is a documentation debt to clear whenever D-03's protection lapses.

Also left alone per the plan: the `conn` (`:memory:`) fixture, and WR-01's TOCTOU overshoot in `ratelimit.py` (deferred to gap closure, and it did not fall out for free here).

## TDD Gate Compliance

Task 2 carried `tdd="true"`, but the plan sequences implementation (Task 1) before tests (Task 2), so a literal RED gate was not reachable — the pragma and foreign-key tests passed on first run by construction. The RED signal was obtained differently and is the stronger version: the concurrency test was executed against a `Database.transaction()` monkeypatched into the Pitfall 3 bug, in a scratch script outside the repo, and confirmed to **fail** with the documented corruption signature before being accepted as green against the real implementation. Git gate sequence for the plan is `feat` (`dc8e357`) then `test` (`3e098e8`) rather than `test` then `feat`, which is the plan's task ordering, not a skipped gate.

## Known Stubs

None. Every symbol this plan introduces is fully wired and exercised by tests.

## Threat Flags

None. The threat register's two `mitigate` rows are both implemented and test-backed:

| Threat ID | Mitigation | Evidence |
|-----------|------------|----------|
| T-02-01 | `transaction()` holds the RLock across the statement group | `test_a_failed_write_does_not_leave_a_partial_row_when_another_thread_commits` |
| T-02-02 | `Result` materialises rows inside the lock | `Database._run` source assertion; `Result` never stores a cursor |

T-02-03 holds: the wrapper passes `params` through untouched and adds no string formatting — no f-string or `%`-format reaches `execute`. T-02-SC holds: `pyproject.toml` is untouched and this plan installed no packages.

## Requirements Satisfied

- **DATA-01** (storage half): connection ownership is explicit, results are materialised inside the lock, WAL/`busy_timeout`/`foreign_keys` verified on a file database. The `to_thread` offload seam itself belongs to plan 02-02.

## Self-Check: PASSED

- `src/relay/db.py` — FOUND (modified)
- `tests/conftest.py` — FOUND (modified, append-only)
- `tests/test_db.py` — FOUND (created)
- Commit `dc8e357` — FOUND
- Commit `3e098e8` — FOUND
- D-03 gate against `4b91681` — prints nothing
- STATE.md / ROADMAP.md — not modified (orchestrator owns those writes)

---
phase: 02-async-safe-data-layer-graceful-shutdown
reviewed: 2026-08-10T00:00:00Z
depth: standard
files_reviewed: 13
files_reviewed_list:
  - src/relay/db.py
  - src/relay/runs.py
  - src/relay/main.py
  - src/relay/agent.py
  - src/relay/tools.py
  - src/relay/telemetry.py
  - src/relay/config.py
  - tests/test_db.py
  - tests/test_lifecycle.py
  - tests/conftest.py
  - tests/helpers.py
  - fly.toml
  - Dockerfile
findings:
  critical: 1
  warning: 7
  info: 8
  total: 16
status: issues_found
---

# Phase 2: Code Review Report

**Reviewed:** 2026-08-10
**Depth:** standard (with targeted mutation and runtime probes)
**Files Reviewed:** 13
**Status:** issues_found

## Summary

The core of the data layer is sound. `Database`/`Result` genuinely closes the
cursor-escape hazard the research measured, the single `to_thread` seam is real and
proven off-loop, OTel span parenting survives the offload, and `conn.close()` cannot
interleave inside a `transaction()` because the RLock is held across the whole unit of
work. Baseline: `126 passed`, `ruff check src tests` clean.

What the phase does **not** have is defence at the edges of that core:

1. **One Critical.** An exception from `record_run` inside `event_stream`'s `finally`
   skips both `release_run` and `runs.deregister`. Verified by probe: the registry entry
   leaks **permanently** and a $0.50 spend reservation leaks for the full 300 s TTL. The
   single most likely trigger is the very race this phase exists to close (drain times
   out → `conn.close()` → a still-running stream's `record_run` hits a closed database).
   `runs.py`'s docstring asserts "register and deregister are exactly balanced — a future
   reader should not 'fix' this by adding a TTL". That claim is false on the error path.

2. **The phase's headline behaviour is not tested.** Deleting
   `await app.state.runs.drain(...)` from `lifespan`, or reordering it after
   `conn.close()`, leaves **126/126 green**. Removing `transaction()` from
   `tools.send_reply` or `telemetry.record_run` also leaves **126/126 green**. The
   primitives are well tested in isolation (`test_db.py`); their *adoption* is not tested
   anywhere. `test_shutdown_timeouts_nest_correctly` asserts the config arithmetic, not
   that anything drains.

3. **`transaction()` is re-entrant-acquirable but not nest-safe.** Verified by probe: an
   inner `with db.transaction()` commits the **outer** block's partial work, and the
   outer rollback then undoes nothing — Pitfall 3 reintroduced through the API built to
   prevent it. No call site nests today; Phase 5's `run_events` writer is the obvious
   place it will.

4. **`enforce_daily_budget` on the event loop is now a worse blocker than before the
   phase.** It acquires `Database`'s RLock. Measured stall: **0.81 s** with one worker
   transaction in flight; the bound is `busy_timeout = 5000` ms, which exceeds the
   container `HEALTHCHECK --timeout=3s`. The comment at `main.py:78-82` claiming
   "contention is microseconds" is measurably wrong.

On the three self-reported items: (1) the `_idle` cross-loop binding is **not** benign in
the way claimed — see WR-05, it is one scheduling change away from a hard suite failure
and CR-01 removes its only protection; (2) coverage of the materialisation invariant **is**
now adequate — the mutation is caught deterministically; (3) D-11 is confirmed
documentation-only for `mcp_server.py`/`evals.py`, but the audit missed `ratelimit.py`
(WR-07). Phase 1's WR-01 TOCTOU is deferred and not re-reported.

---

## Critical Issues

### CR-01: A failed `record_run` skips `release_run` and `runs.deregister`, leaking the drain registry permanently

**File:** `src/relay/main.py:202-230`
**Issue:**
The `finally` block runs three statements in sequence with no nesting:

```python
finally:
    if not recorded:
        recorded = True
        record_run(app.state.conn, ...)   # <- if this raises...
    release_run(token)                    # <- never runs
    app.state.runs.deregister(run_token)  # <- never runs
```

Any exception out of `record_run` abandons both cleanups. Verified by probe (patched
`record_run` to raise, drove one stream to completion):

```
stream raised: Boom db write failed
registry active after: 1
reservations outstanding: 0.5
```

Concrete failure scenarios, most likely first:

1. **The shutdown race this phase exists to close.** `drain()` times out (returns `False`,
   by design, `runs.py:96-111`), `lifespan` proceeds to `conn.close()`
   (`main.py:41-42`), and a still-in-flight run's `record_run` then raises
   `sqlite3.ProgrammingError: Cannot operate on a closed database`. This is the exact
   error string 02-RESEARCH.md Pitfall 5 predicts, and it lands in the exact code path
   Phase 1's CR-01 fix created.
2. **`sqlite3.OperationalError: database is locked`** after `busy_timeout` expires.
   `.planning/codebase/ARCHITECTURE.md` documents two runtimes (HTTP + MCP) sharing one
   file; a 5 s writer stall in the other process is enough.
3. Disk-full / I/O error on the Fly `relay_data` volume.

Consequences, in a process that keeps running:

- `runs._active` never empties → `_idle` is never set → **every subsequent `drain()`
  burns the full `shutdown_drain_seconds` and returns `False`**, logging a false
  `shutdown.drain_timeout`. The drain mechanism is dead for the life of the process.
- D-06 is violated: an idle server permanently holds a phantom run, and Phase 5's
  `snapshot()` will render it forever.
- The reservation leak is the CR-02 class Phase 1 rated Critical, here bounded to 300 s
  by `RESERVATION_TTL_S`: each leak silently consumes $0.50 of the $5/day ceiling.
- It compounds with WR-05: with `active` stuck > 0, a later `drain()` on a different
  loop raises `RuntimeError: ... bound to a different event loop` out of lifespan
  shutdown instead of returning `False`.

**Fix:** nest the cleanups so they cannot be skipped, and never let a telemetry write
take down lifecycle bookkeeping.

```python
finally:
    try:
        if not recorded:
            recorded = True
            record_run(app.state.conn, ...)
    except Exception:  # noqa: BLE001 — the run is over; losing the row must not
        # also leak the reservation and the registry entry.
        logger.exception("run.record_failed", extra={"ctx": {"ticket_id": ticket.id}})
    finally:
        release_run(token)
        app.state.runs.deregister(run_token)
```

Then correct `runs.py:55-63`'s docstring: registration and deregistration are balanced
only because the caller guarantees it, which is exactly the kind of claim that needs a
test (see WR-02).

---

## Warnings

### WR-01: `transaction()` is re-entrant-acquirable but not nest-safe — an inner block commits the outer block's partial work

**File:** `src/relay/db.py:153-168`
**Issue:** The lock is deliberately an `RLock` so `transaction()` can call `execute()`
(docstring, `db.py:118-120`). That same re-entrancy silently accepts a nested
`transaction()`, whose exit calls `self._conn.commit()` — which is connection-scoped and
therefore commits the *outer* transaction too. The outer block's rollback then undoes
nothing. Verified by probe:

```
outer failed: outer unit of work fails after the inner one committed
ticket status after outer rollback: resolved      # <- outer's UPDATE survived
replies rows surviving: [<sqlite3.Row ...>]       # <- inner's INSERT survived
```

This is Pitfall 3 — the bug `transaction()` was built to prevent — reintroduced through
`transaction()` itself, with no exception and no log. No current call site nests, so
nothing ships broken today; but `db.py`'s module docstring markets `transaction()` as
"the only way to group statements", and Phase 5's `run_events` writer composed with
`record_run` is the natural first nesting.

**Fix:** track depth and only commit/roll back at the outermost level.

```python
def __init__(self, conn: sqlite3.Connection):
    self._conn = conn
    self._lock = threading.RLock()
    self._depth = 0

@contextmanager
def transaction(self):
    with self._lock:
        self._depth += 1
        outermost = self._depth == 1
        try:
            yield self
            if outermost:
                self._conn.commit()
        except BaseException:
            # An inner failure must not be committable by an outer success.
            self._conn.rollback()
            raise
        finally:
            self._depth -= 1
```

(Or raise `RuntimeError("transaction() is not nestable")` on re-entry — cheaper, and
turns the trap into a loud failure. Either way, add a test.)

### WR-02: DATA-02's headline wiring — drain before `conn.close()` — has zero test coverage

**File:** `src/relay/main.py:41-42`
**Issue:** Two mutations, each run against the full suite:

| Mutation | Result |
|---|---|
| Swap to `conn.close()` then `await drain(...)` (reinstates Pitfall 5 exactly) | **126 passed** |
| Delete `await app.state.runs.drain(...)` entirely | **126 passed** |

`test_drain_*` in `test_lifecycle.py` all construct a standalone `RunRegistry`;
`test_a_cancelled_run_task_still_records_and_drains` calls `drain()` by hand rather than
through the lifespan; `test_shutdown_timeouts_nest_correctly` reads `fly.toml` and the
`Dockerfile`. Nothing observes that `lifespan` drains, or that it drains *first*. The
one deliverable the phase is named for is unguarded.

**Fix:** a test that drives the real teardown and pins the ordering, e.g. instrument the
`Database` and assert the close happened after the registry emptied:

```python
async def test_lifespan_drains_before_it_closes_the_connection(monkeypatch, tmp_path):
    order: list[str] = []
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    async with lifespan(app):
        app.state.runs.register(ticket_id=1)          # a stream still in flight
        real_close = app.state.conn.close
        monkeypatch.setattr(app.state.conn, "close",
                            lambda: order.append("close") or real_close())
        async def finish():
            await asyncio.sleep(0.05)
            order.append("deregistered")
            app.state.runs.deregister(0)
        asyncio.create_task(finish())
    assert order == ["deregistered", "close"]
```

### WR-03: No test asserts the five business writers actually use `transaction()`

**File:** `src/relay/tools.py:67-93`, `src/relay/telemetry.py:73-78`, `src/relay/main.py:115-123`
**Issue:** `test_db.py::test_a_failed_write_does_not_leave_a_partial_row_...` tests the
primitive directly and is genuinely load-bearing. But adoption is not tested. Mutations:

| Mutation | Result |
|---|---|
| `tools.send_reply` → bare `execute` + `execute` + `commit()` | **126 passed** |
| `telemetry.record_run` → bare `execute` + `commit()` | **126 passed** |

02-RESEARCH.md Pitfall 3 names exactly five multi-statement writers as the hazard
surface; a regression at any of those call sites is invisible to CI. Note `record_run`
runs on the event loop while worker threads write concurrently, so it is the most
exposed of the five.

**Fix:** either a structural assertion over the writers, or a per-writer barrier test in
the shape of `test_db.py`'s. Cheapest useful version:

```python
@pytest.mark.parametrize("fn", [tools.send_reply, tools.create_escalation,
                                tools.set_category, telemetry.record_run])
def test_business_writers_group_their_statements(fn, db, monkeypatch):
    entered = []
    real = db.transaction
    monkeypatch.setattr(db, "transaction",
                        lambda: entered.append(fn.__name__) or real())
    ...  # call fn against db
    assert entered, f"{fn.__name__} committed outside a transaction()"
```

### WR-04: `RunRegistry.register()` ignores `draining`, and `drain()`'s fast path lets a run register after the drain has already returned

**File:** `src/relay/runs.py:55-67`, `src/relay/runs.py:87-89`
**Issue:** `drain()`'s docstring says "Stop admitting runs, then wait for the in-flight
ones", but `register()` never consults `self.draining`. The refusal lives 100 lines away
in `main.py:152`, is checked in the *handler*, and registration happens later, in the
*generator body* (correctly, per Pitfall 4). Those two points are separated by an
arbitrary scheduling gap. Verified by probe:

```
drain returned: True draining: True
registered after drain; active = 1 token 0
second drain: False elapsed 0.203
```

Failure scenario: handler passes the `draining` check → returns `StreamingResponse` →
lifespan drains, `_active` empty, fast path returns `True` immediately →
`conn.close()` → the generator body then starts, registers, and runs the agent against
a closed connection. uvicorn's connection wait narrows this window in production, but
the registry's own stated contract does not hold, and Phase 5 will add a second
registration site that does not know about `main.py`'s check.

**Fix:** enforce the first half of the contract where it is declared.

```python
def register(self, *, ticket_id: int) -> int:
    if self.draining:
        # The handler's 503 is best-effort and fires earlier than this; the registry
        # is the only place that knows a drain has already taken its fast path.
        raise RuntimeError("registry is draining; refusing to admit a new run")
    ...
```

…and have `event_stream` translate that into the same `shutting_down` payload rather
than starting a run it cannot finish.

### WR-05: `RunRegistry._idle` binds to the first loop that waits on it, and the fast path is the only thing preventing a cross-loop `RuntimeError`

**File:** `src/relay/runs.py:51-52`, `src/relay/runs.py:95`
**Issue:** The self-report calls this benign because "on correct code `active == 0` takes
the fast path". Verified — that is true of the current suite. It is also an unstated,
unasserted invariant that nothing enforces:

```
after loop1: active = 1
loop2 RAISED: RuntimeError <asyncio.locks.Event object ...> is bound to a different event loop
```

Two ways it stops being benign:

- **CR-01 removes the protection outright.** Once a `record_run` failure pins `active` at
  1, every later `drain()` *must* wait, so the next drain on a different loop raises out
  of `lifespan` shutdown instead of returning `False`.
- **The suite is one scheduling change away.** `test_a_cancelled_run_task_still_records_and_drains`
  awaits `drain()` on an `asyncio.run` loop while the `client` fixture's lifespan drains
  on the `TestClient` portal loop. It only survives because the cancelled generator's
  `finally` happens to complete synchronously before the gather returns. 02-RESEARCH.md
  Pitfall 6 explicitly contemplates offloading `record_run` — doing so introduces a
  suspension point there and flips this to a hard failure.

Production is single-loop, so this is not a shipping blocker; it is a defect in a
primitive Phase 5 will reuse, with a cheap fix.

**Fix:** do not hold a loop-bound object across drains. Build the waiter inside `drain()`:

```python
async def drain(self, *, timeout: float) -> bool:
    self.draining = True
    if not self._active:
        return True
    # Created here, not in __init__: an asyncio.Event binds to the loop that first
    # waits on it, and the registry outlives no loop only by accident today.
    self._idle = asyncio.Event()
    if not self._active:          # re-check: deregister may have landed above
        return True
    ...
```

(and have `deregister` set `self._idle` only when it exists). Add a regression test that
drains the *same* registry from two `asyncio.run` loops.

### WR-06: `enforce_daily_budget` now blocks the event loop on `Database`'s RLock — measured 0.81 s, bounded only by `busy_timeout = 5000`

**File:** `src/relay/main.py:76-83`, `src/relay/ratelimit.py:147`, `src/relay/ratelimit.py:196`
**Issue:** The comment at `main.py:78-82` justifies leaving this read on the loop with
"It sums tens of rows behind `idx_runs_created_at`, so contention is microseconds". The
query time is microseconds; the *mutex acquisition* is not. `Database.execute` does a
blocking `with self._lock`, and worker threads hold that lock for the whole of a
`transaction()`. Measured with one worker transaction in flight:

```
event loop blocked for 0.81s inside spent_today()
largest loop gap: 0.81s
```

The upper bound is `PRAGMA busy_timeout = 5000` — a write blocked by the second runtime
on the same WAL file (`.planning/codebase/ARCHITECTURE.md`: HTTP process + MCP process,
"relies on SQLite's own locking, not application-level coordination"). A 5 s loop stall
means `/health` does not answer, and the container `HEALTHCHECK` has `--timeout=3s`:
the machine marks itself unhealthy and restarts, killing every in-flight run. This is a
net regression from the pre-phase state, in the phase whose stated purpose is removing
loop blocking.

**Fix:** the read is already `async def _dependency`, so the offload is one line and
`HTTPException` propagates through `to_thread` cleanly (02-RESEARCH.md Pitfall 7,
option 3):

```python
if meter_spend:
    await asyncio.to_thread(enforce_daily_budget, app.state.conn)
```

The stated objection — that this moves a read of `ratelimit._reservations` onto a worker
thread — is a GIL-atomic dict read/`list()` copy, and `_prune` already tolerates
concurrent mutation via `list(_reservations.items())` + `pop(token, None)`. If that is
still unacceptable, split the two: offload only `conn.execute(DAILY_SPEND_SQL)` and keep
`reserved_usd()` on the loop.

### WR-07: `ratelimit.py` carries stale `sqlite3.Connection` annotations that D-11 does not record

**File:** `src/relay/ratelimit.py:147`, `src/relay/ratelimit.py:196` (and `import sqlite3` at `:19`)
**Issue:** `connect()` now returns `Database`, and `tools.py`/`telemetry.py` were both
updated to match. `ratelimit.py` was not:

```python
def spent_today(conn: sqlite3.Connection, *, now: float | None = None) -> float:
def enforce_daily_budget(conn: sqlite3.Connection) -> None:
```

Both are called from `main.py:83` with a `Database`. Unlike `mcp_server.py` and
`evals.py`, `ratelimit.py` is **not** in D-03's protected set — it could have been fixed,
and the D-11 record in the SUMMARY (which names exactly two files) is now incomplete. No
runtime consequence today; it is a documented-accuracy defect that a future mypy gate or
a Phase 5 reader will trip on. `import sqlite3` at `:19` exists only to support these
hints.

**Fix:** `from .db import Database`, retype both signatures, drop `import sqlite3`, and
amend the D-11 note to say three files were affected and one was fixed.

---

## Info

### IN-01: `_get_ticket`'s comment states the opposite of `Result`'s contract

**File:** `src/relay/main.py:298-300`
**Issue:** "fetchone() is called inside the offloaded callable, not after it: … stepping
the result back on the event loop would put the read half a statement outside the thread
it belongs to." `Result` is materialised — `fetchone()` is list indexing and touches no
connection, which is the entire point of `db.py:78-87`. Mutating `_get_ticket` to step
the `Result` on the loop leaves **126 passed**, correctly: it is safe. The comment
teaches the pre-`Result` hazard as if it still applied, and directly contradicts
`Result`'s own docstring.
**Fix:** rewrite as a style note ("keeps the whole read in one place") or delete it.

### IN-02: `Result` exposes three members nothing uses, and `db.py` omits mandatory type hints

**File:** `src/relay/db.py:89-108`, `src/relay/db.py:91`, `:132`, `:135`, `:138`, `:153`
**Issue:** `rowcount`, `description` and `fetchmany` have zero call sites in `src/` or
`tests/` (`grep` confirms; `test_mcp.py:26`'s `tool.description` is unrelated).
02-RESEARCH.md's own API audit lists the used surface as `fetchone`/`fetchall`/
`lastrowid`/iteration. Separately, CLAUDE.md states "Type hints are mandatory on all
function signatures"; `Result.__init__`, `execute(params=())`, `executemany(seq)`,
`executescript`'s return and `transaction()`'s return are all unannotated.
**Fix:** drop the unused members (or annotate them as deliberate future surface with a
comment); add `params: tuple[Any, ...] | list[Any] = ()`, `seq: Iterable[Any]`, and
`-> Iterator["Database"]` on `transaction`.

### IN-03: D-10's `idx_runs_created_at` has no test

**File:** `src/relay/db.py:65-67`
**Issue:** Deleting the `CREATE INDEX` line leaves **126 passed**. The index is the whole
justification for the D-10 scope expansion and for `main.py:78-82`'s "contention is
microseconds" claim.
**Fix:** one assertion in `test_db.py`:
`assert db.execute("PRAGMA index_list('runs')").fetchall()` contains `idx_runs_created_at`.

### IN-04: `transaction()`'s `except BaseException` — a named key decision — is untested

**File:** `src/relay/db.py:166`
**Issue:** Changing it to `except Exception:` leaves **126 passed**. The choice is
correct and worth keeping (a `BaseException` escaping with the transaction open lets the
next `transaction()` on any thread commit the partial write), but nothing pins it. Note
the practical reachability is low: every `transaction()` body is synchronous code in a
worker thread, and `to_thread` cancellation does not interrupt the thread.
**Fix:** a test that raises `KeyboardInterrupt` (or `asyncio.CancelledError`) inside a
`transaction()` and asserts the row is gone.

### IN-05: `init_db()` is not atomic

**File:** `src/relay/db.py:184-192`
**Issue:** `executescript` → `SELECT COUNT(*)` → `executemany` → `commit()`, with the
lock released between each. Two processes booting against a fresh `/data/relay.db` (the
HTTP app and `python -m relay.mcp_server`) can both read `existing == 0` and both seed,
raising `sqlite3.IntegrityError` on the `customers` primary key and crashing one
startup. 02-RESEARCH.md notes `init_db` "runs single-threaded at startup" — true within
one process, not across the two runtimes ARCHITECTURE.md documents.
**Fix:** wrap the count-and-seed in `with conn.transaction():`, or use
`INSERT OR IGNORE`.

### IN-06: `event_stream` re-reads `app.state.runs` / `app.state.conn` at teardown instead of capturing them

**File:** `src/relay/main.py:183`, `:217`, `:230`
**Issue:** `register()` and `deregister()` resolve `app.state.runs` independently. Each
lifespan installs a fresh `RunRegistry` whose `itertools.count()` restarts at 0, so a
generator that outlives a restart deregisters *token 0 of the new registry* — retiring a
different, live run and potentially letting `drain()` return early. Only reachable with
an in-process app restart (repeated `TestClient` contexts); production has one lifespan.
**Fix:** bind once at the top of the generator body: `runs = app.state.runs`,
`conn = app.state.conn`.

### IN-07: the 503-while-draining refusal is checked after a database read

**File:** `src/relay/main.py:141-152`
**Issue:** `await _get_ticket(ticket_id)` and the `409` status check run before the
`draining` check, so a request that will be refused still performs an offloaded read and
can instead surface a 404/409 during shutdown.
**Fix:** move the `draining` check to the top of `process_ticket`, above `_get_ticket`.

### IN-08: `Database` parameters are still named `conn`

**File:** `src/relay/db.py:184`, `src/relay/tools.py:96`, `src/relay/telemetry.py:57`, `:88`
**Issue:** `init_db(conn: Database)`, `build_registry(conn: Database, ...)`,
`record_run(conn: Database, ...)`, `run_metrics(conn: Database)`. `tools.py`'s module-level
helpers already use `db:` (`lookup_customer(db: Database, ...)`), so the codebase now
uses both names for the same type. `app.state.conn` holds a `Database` too. Minor, but
the phase's stated goal was making connection ownership explicit.
**Fix:** rename to `db` at the remaining sites (all internal; no external callers).

---

## Verified as Correct — do not re-litigate

Each of these was checked by mutation or runtime probe during this review.

| Item | How verified | Result |
|---|---|---|
| `Result` materialisation is genuinely covered | Replaced `_run` with a live-cursor return; ran full suite | `test_a_result_is_materialised_before_another_thread_touches_the_connection` **fails deterministically**. Self-report #2 is adequate for this invariant. |
| The `to_thread` offload seam is covered | Replaced the `await asyncio.to_thread(_execute_guarded, ...)` with a direct call | `test_tool_execution_runs_off_the_event_loop` **fails**. Load-bearing. |
| OTel span parenting survives `to_thread` | Runtime probe reading `trace.get_current_span()` inside the worker | Returns `tool.search_docs` — parenting preserved, no change needed. |
| `conn.close()` cannot interleave inside a `transaction()` | Read of `Database.close`/`transaction` lock discipline | Both take the same RLock and `transaction()` holds it across the whole block, so a close can only land between units of work. A post-close statement in a worker raises `ProgrammingError`, which `_execute_guarded` converts to a model-facing error. Connection lifetime is sound (the *fallout* on the `record_run` path is CR-01, not a close-ordering bug). |
| A stream cancelled before its body starts leaks nothing | `test_a_stream_that_never_starts_registers_nothing` + read of `main.py:173-183` | Registration is genuinely inside the generator body; register/deregister are balanced on all non-error paths. Pitfall 4 is closed. |
| The 503 refusal precedes `reserve_run()` | `test_process_returns_503_while_draining` asserts `reserved_usd() == 0.0` | Correct ordering; no spend is claimed by a refused caller. |
| `drain()` is idempotent and never raises on timeout | `test_drain_times_out_rather_than_hanging_shutdown` + probe calling `drain()` twice | Returns `False` and leaves `draining` set; a second call is safe. |
| Timeout nesting (30 > 20 > 5), `exec uvicorn`, no `kill_signal` | `test_shutdown_timeouts_nest_correctly` parses `fly.toml` with `tomllib` and greps the `Dockerfile` | Correct, and the test is load-bearing for D-05/D-08. |
| D-11 is documentation-only for `mcp_server.py` and `evals.py` | Full audit of connection methods used: `execute`, `commit`, `close`, `executescript`, `executemany`, `row_factory`; cursor members `fetchone`, `fetchall`, `lastrowid` | `Database`/`Result` satisfy all of them. `mcp_server._create_ticket` reads `cur.lastrowid` **after** `commit()` — safe precisely because `Result` captured it at execute time. `evals.run_case` likewise. No runtime consequence. (`ratelimit.py` is the one file the audit missed — WR-07.) |
| SQL injection surface | Read of `Database.execute`/`_run` | Every statement is parameterised; the wrapper forwards `params` untouched and adds no string formatting. No new injection surface. |
| Phase 1 WR-01 (budget-check/reservation TOCTOU) | `01-DEFERRED.md` | Deliberately deferred; unchanged by this phase; **not** re-reported. |
| Baseline health | `pytest -q`, `ruff check src tests` | `126 passed`; `All checks passed!`. Working tree restored clean after all mutations. |

---

_Reviewed: 2026-08-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_

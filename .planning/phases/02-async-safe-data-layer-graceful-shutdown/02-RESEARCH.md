# Phase 2: Async-Safe Data Layer & Graceful Shutdown - Research

**Researched:** 2026-08-09
**Domain:** Thread-safe SQLite under asyncio, SSE stream draining, container/platform shutdown signalling
**Confidence:** HIGH — every load-bearing claim was verified by executing code in this repo's own environment, not inferred

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Async DB seam**
- **D-01:** Async-safety comes from a **single `asyncio.to_thread` offload seam**, not an `aiosqlite` rewrite. Offload at the `_execute_guarded` call site in `agent.py`, plus the direct DB calls in the HTTP handlers. `ToolSpec.execute` stays a sync `Callable[..., str]`.
- **D-02:** The sync-executor contract is enforced **mechanically, by test** — assert no registered `ToolSpec.execute` is a coroutine function. Rationale: the failure mode is silent (a coroutine object is returned as a tool result — no exception, garbage into the model's context) and there is no mypy in CI to catch it. This test converts a silent corruption into a loud CI failure.
- **D-03:** `mcp_server.py`, `evals.py`, and all existing tool tests must remain untouched by the async change. If a proposed approach requires editing them, it is the wrong approach.

**Shutdown drain**
- **D-04:** Drain via a **hand-rolled in-flight task registry**, not `sse-starlette`'s built-in SIGTERM hook. Rationale: Phase 5's live feed needs the same registry, so building it here avoids building it twice; it also avoids a new dependency that owns the response type. (This deliberately overrides STACK.md's `sse-starlette` recommendation — ARCHITECTURE.md's registry approach wins on reuse.)
- **D-05:** Lifespan shutdown awaits in-flight runs with a **~30s grace period** before closing the DB. A typical run is ~20s and is bounded by the existing step/budget caps. `fly.toml` needs a matching `kill_timeout` — Fly's default SIGTERM→SIGKILL window is 5s, which would defeat the drain.
- **D-06:** The registry holds **only active agent runs**. An idle server holds nothing, so Fly's autostop still suspends the machine. This must be covered by an explicit test asserting the registry is empty after a run completes — scale-to-zero is a core-value constraint ("cheap to keep running"), not a nice-to-have.

**Scope boundary**
- **D-07:** DATA-02's "record on interruption" half is **already done** — Phase 1's CR-01 fix (`b6da97e`) moved `record_run` into a `finally` in `event_stream`, with a `recorded` guard. This phase must **preserve** that behaviour (a regression test already exists: `test_mid_stream_disconnect_still_records_the_spend`) and add only the shutdown-drain half.

### Claude's Discretion

- Connection ownership specifics: whether to keep one shared connection behind a lock, adopt connection-per-thread, or introduce a thread-safe `Database` wrapper. Derive from D-01. **Hard constraint:** today's single shared connection is only accidentally correct because access is single-threaded — handing that same connection to worker threads makes `commit()` cross-request, committing another request's partial transaction. Whatever is chosen must make connection ownership explicit.
- WAL and `busy_timeout` pragma placement, and how tests stay representative given the trap below.
- Whether the MCP server's independently-opened connection needs the same treatment.

### Deferred Ideas (OUT OF SCOPE)

- `run_events` persistence (DATA-03) — Phase 5, per the roadmap's split at the persistence seam
- WR-01 TOCTOU overshoot between budget check and reservation — deferred to gap closure (`01-DEFERRED.md`), even though it lives in code this phase touches
- Adding mypy to CI — would catch the coroutine-contract bug class generally, but it is a new CI gate and broader than this phase; D-02's targeted test covers the specific risk
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **DATA-01** | All SQLite access is async-safe (thread offload at the `_execute_guarded`/handler seam, per-connection ownership fixed, WAL + busy_timeout on file databases); the sync `ToolSpec.execute` contract is preserved for MCP and tests | Pattern 1 (`Database` wrapper with materialised results), Pattern 2 (the `to_thread` seam), Pitfall 1 (the lock-per-statement trap, with a verified A/B), Pitfall 2 (WAL `:memory:` no-op, verified), Code Examples §1-3, Validation Architecture rows DATA-01-a..f |
| **DATA-02** | Graceful shutdown drains in-flight SSE runs before closing the database; `record_run` persists even when a run is interrupted (moved to a `finally` path) | Pattern 3 (`RunRegistry`), Pattern 4 (the shutdown budget), Pitfall 4 (register-inside-the-generator), Pitfall 5 (uvicorn cancels but does not await), Code Examples §4-6, Validation Architecture rows DATA-02-a..e. The `record_run`-in-`finally` half is already shipped (D-07) and covered by `test_mid_stream_disconnect_still_records_the_spend` |
</phase_requirements>

## Summary

Two independent problems share one phase because both are teardown/ownership problems in `main.py`'s lifespan.

**The data-layer half is more dangerous than the existing research says.** PITFALLS.md #9 correctly predicts that handing today's shared `sqlite3.Connection` to worker threads makes `commit()` cross-request. The obvious remedy — wrap the connection in an object that takes a `threading.Lock` around each `execute()` — is **still broken**, and I proved it: `Database.execute()` returns a live `sqlite3.Cursor`, and the caller steps that cursor (`.fetchone()`, `.fetchall()`) *after* the lock has been released. A concurrent statement on the same connection then interleaves with the in-flight cursor. The observable damage in a 6-way concurrency test on this codebase was not `database is locked` — it was **`Ticket` rows materialising with `customer_email=None` and `status=''`**, plus spurious 404s. Fixing the topology means the lock must cover fetch as well as execute, which means `execute()` must return a materialised result, not a cursor.

**The shutdown half hinges on a uvicorn detail nobody documents.** I read uvicorn 0.52.0's `Server.shutdown()`: it waits for in-flight connections (default timeout `None` = **forever**), then on timeout calls `task.cancel()` on every request task and immediately proceeds to `lifespan.shutdown()` **without awaiting the cancellations**. So `conn.close()` in the lifespan teardown can run while a cancelled `event_stream`'s `finally` — the one Phase 1's CR-01 fix put `record_run` into — has not executed yet. That is the precise, verified race the app-level drain registry closes. It is not belt-and-braces; it is the only thing standing between a truncated deploy and a lost spend record.

**The headline result: the entire change set can land with zero edits to any existing test.** I built a shadow copy of `src/relay/`, applied the full design (materialising `Database` wrapper, WAL/busy_timeout pragmas, `await asyncio.to_thread(_execute_guarded, ...)`, async handlers, transactions in `tools.py`/`telemetry.py`, `RunRegistry` + lifespan drain) and ran the real 110-test suite against it: **110 passed, zero test edits**. Then I wrote 12 candidate new tests for DATA-01/DATA-02 and ran them: **122 passed**. D-03 is satisfiable in full, and the "which tests break" answer is *none*.

**Primary recommendation:** `connect()` returns a `Database` that owns one connection behind a `threading.RLock`, materialises every query result inside the lock, and exposes a `transaction()` context manager that the five multi-statement writers adopt; offload `_execute_guarded` and the handler reads with `asyncio.to_thread`; track in-flight runs in an `app.state.runs` registry registered *inside* `event_stream`'s body and deregistered in its existing `finally`; set `kill_timeout = 30` in `fly.toml` and `--timeout-graceful-shutdown 20` on the uvicorn CMD.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Connection ownership, pragmas, transaction boundaries | Storage (`db.py`) | — | Ownership must be expressed once, in the module that creates the connection. Spreading `commit()` discipline across five call sites is what created the hazard. |
| Serialising concurrent DB access | Storage (`db.py`) | — | The lock belongs to the object that owns the connection; callers must not be able to forget it. |
| Moving blocking work off the event loop | Agent loop (`agent.py`) + HTTP edge (`main.py`) | — | D-01 fixes the seam at the *call site*, so the executor contract stays sync for MCP/evals. This is a scheduling concern, not a storage concern. |
| Transaction grouping for a business operation (reply = INSERT + UPDATE) | Tools (`tools.py`) | Storage (provides `transaction()`) | Only the tool knows which statements are one unit of work; `db.py` provides the primitive. |
| Tracking which runs are in flight | New module (`runs.py`) | HTTP edge (registers/deregisters) | Phase 5's live feed needs the same registry (D-04). Keeping it out of `main.py` means it is testable without a TestClient. |
| Deciding when it is safe to close the DB | HTTP edge lifespan (`main.py`) | Registry (answers "is anything running?") | Teardown ordering is a lifespan concern; the registry only reports state. |
| Bounding the shutdown window | Platform (`fly.toml`) + container (`Dockerfile` CMD) | Config (`settings.shutdown_drain_seconds`) | Three timeouts must nest correctly; only one of them lives in Python. |

**Explicitly NOT owned here:** the MCP server's connection. `mcp_server.amain()` runs in its own process over single-threaded stdio and never uses `to_thread`. It needs `connect()`'s pragmas (it opens the *same file* the HTTP process may have open — that is exactly what WAL is for) but not the lock. It gets the lock for free by using `connect()`; that is harmless and requires no edit to `mcp_server.py`. **Answer to the discretion question: no separate treatment needed.** [VERIFIED: read `src/relay/mcp_server.py:145-154`; shadow run of `tests/test_mcp.py` green]

## Standard Stack

### Core

**No new packages.** Every mechanism this phase needs is in the Python standard library or already installed.

| Module | Version | Purpose | Why Standard |
|--------|---------|---------|--------------|
| `asyncio.to_thread` | stdlib, 3.9+ | The single offload seam (D-01) | Copies the current `contextvars.Context` into the worker thread — verified below — so OpenTelemetry span parenting survives the offload with no code change |
| `threading.RLock` | stdlib | Serialises the shared connection | Re-entrant, so `transaction()` can call `execute()` without deadlocking |
| `sqlite3` PRAGMAs | SQLite 3.50.4 (bundled) | `journal_mode=WAL`, `busy_timeout`, `foreign_keys` | Per-connection settings; WAL persists in the file header |
| `asyncio.Event` + `asyncio.wait_for` | stdlib | Drain signalling and its timeout | `wait_for` raising `TimeoutError` is the whole drain-timeout mechanism |
| `contextlib.contextmanager` | stdlib | `Database.transaction()` | Matches the codebase's existing preference for plain, visible control flow |

### Supporting (already installed, unchanged)

| Library | Installed | Purpose | Note |
|---------|-----------|---------|------|
| `uvicorn[standard]` | 0.52.0 | ASGI server; owns the connection-drain window | Only change is a CLI flag in the Dockerfile CMD |
| `fastapi` / `starlette` | 0.141.1 / 1.3.1 | `StreamingResponse` lifecycle | Unchanged |
| `limits` | 5.8.0 | Existing perimeter | Unchanged |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `to_thread` + `Database` | `aiosqlite>=0.22` | STACK.md's recommendation. Makes every executor `async`, which propagates through `tools.py`, `agent.py`, `mcp_server.py`, `evals.py`, and `tests/test_tools.py`. **Excluded by D-01 and D-03.** Revisit only on a Postgres migration. |
| Shared connection + lock | Connection-per-thread (`threading.local`) | Genuinely eliminates cursor sharing, and WAL makes it work on a file DB. **Fatal here:** each thread-local connection to `":memory:"` is a *separate empty database*, and `evals.py:148` (`connect(":memory:")`, untouchable per D-03) plus the `conn` test fixture both depend on `:memory:`. Also requires `build_registry` to take a factory rather than a connection, breaking every existing call site. |
| Shared connection + lock | One dedicated `ThreadPoolExecutor(max_workers=1)` for all DB work | Correct (this is what `aiosqlite` does internally) and needs no lock, but it is `loop.run_in_executor`, not `asyncio.to_thread` (D-01 names the latter), and it serialises `search_docs` — which Phase 3 turns into a ~200 ms Voyage call — behind DB work. |
| Hand-rolled `RunRegistry` | `sse-starlette` `EventSourceResponse(shutdown_grace_period=...)` | **Excluded by D-04.** Note it is *already installed* as a transitive dependency of `mcp`, but is not declared in `pyproject.toml`, so using it would mean promoting an undeclared transitive dep and handing over ownership of the response type. |

**Installation:**
```bash
# none — this phase adds no dependencies
```

**Version verification:** confirmed against the project venv on 2026-08-09:
`uvicorn 0.52.0`, `fastapi 0.141.1`, `starlette 1.3.1`, `limits 5.8.0`, `pydantic 2.13.4`, `anthropic 0.120.2`, `pytest 9.1.1`, `pytest-asyncio 1.4.0`, `sqlite3` bundled SQLite `3.50.4`, local CPython `3.14.6` (CI and Docker: `3.12`; declared floor: `3.11`). [VERIFIED: `pip list`, `sqlite3.sqlite_version`]

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** Every mechanism is stdlib or an already-declared dependency. No `slopcheck` run was required.

The one package this phase deliberately does *not* adopt, `sse-starlette 3.4.6`, is already present in the venv as a transitive dependency of `mcp` and is **not** declared in `pyproject.toml`. If a future phase wants it, it must be promoted to a first-class dependency rather than relied on transitively. [VERIFIED: `pip show mcp` → `Requires: ... sse-starlette ...`; `pyproject.toml` read]

## Architecture Patterns

### System Architecture Diagram

```
                  SIGTERM (Fly, kill_timeout=30s)
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ uvicorn Server.shutdown()                                    │
   │  1. stop accepting connections                               │
   │  2. connection.shutdown()  → in-flight SSE: keep_alive=False │
   │                              (stream is NOT interrupted)     │
   │  3. wait_for(connections idle, --timeout-graceful-shutdown)  │
   │  4. on timeout: task.cancel() ── NOT awaited ────┐           │
   │  5. lifespan.shutdown()                          │           │
   └──────────────┬───────────────────────────────────┼───────────┘
                  │                                   │ CancelledError
                  ▼                                   │ still in flight
        ┌──────────────────────┐                      ▼
        │ lifespan teardown    │        ┌──────────────────────────┐
        │  await runs.drain(t) │◀───────│ event_stream() finally:  │
        │  conn.close()        │ waits  │  record_run(...)         │
        └──────────────────────┘        │  release_run(token)      │
                                        │  runs.deregister(tok)    │
                                        └──────────────────────────┘

 REQUEST PATH (steady state)

  POST /tickets/{id}/process
        │
        ├─ _gate: anon meter → auth → daily ceiling → tier window
        │                              │ (reads runs.cost_usd)
        ▼                              ▼
  await _get_ticket(id) ──to_thread──▶ Database.execute (locked, materialised)
        │
        ├─ reserve_run()  ← Phase 1 spend reservation
        ▼
  StreamingResponse(event_stream())
        │
        ├─ runs.register(ticket_id)   ◀── INSIDE the generator body
        │
        ▼
  agent.run_ticket()  ── per step ──▶ Claude API (await, already non-blocking)
        │                   │
        │                   └─ tool_use ─▶ await asyncio.to_thread(
        │                                       _execute_guarded, …)
        │                                   │  (contextvars copied → OTel span
        │                                   │   parenting preserved)
        │                                   ▼
        │                        spec.execute(**validated)   [SYNC — D-01/D-02]
        │                                   │
        │                                   ▼
        │                        Database.transaction():
        │                          INSERT reply / UPDATE ticket   [one unit]
        │                                   │
        ▼                                   ▼
   SSE frame to client            relay.db (WAL, busy_timeout=5000)
        │
        └─ finally: record_run → release_run → runs.deregister
```

### Recommended Project Structure

```
src/relay/
├── db.py       # CHANGED  Database (RLock + materialised Result + transaction()),
│               #          connect() applies WAL / busy_timeout / foreign_keys
├── runs.py     # NEW      RunRegistry — in-flight run tracking + drain
├── agent.py    # CHANGED  one line: await asyncio.to_thread(_execute_guarded, …)
├── main.py     # CHANGED  async handlers, registry wiring, drain-before-close
├── tools.py    # CHANGED  three writers wrapped in db.transaction()
├── telemetry.py# CHANGED  record_run wrapped in db.transaction()
├── config.py   # CHANGED  + shutdown_drain_seconds
├── mcp_server.py  # UNTOUCHED (D-03)
└── evals.py       # UNTOUCHED (D-03)
tests/
├── conftest.py       # ADD a file-backed `db` fixture; existing fixtures untouched
├── test_db.py        # NEW  pragmas, transaction isolation, the :memory: trap
└── test_lifecycle.py # NEW  offload seam, registry, drain, concurrency
```

**Structure rationale:** `runs.py` is a new flat module rather than an addition to `ratelimit.py`. `ratelimit.py`'s docstring scopes it to "burst limiting and the daily spend ceiling"; run-lifecycle tracking is neither, and Phase 5's `/events` feed will import the registry from a third place. The naming matches the existing one-concern-per-module convention (`auth.py`, `ratelimit.py`, `telemetry.py`). [CITED: `.planning/codebase/CONVENTIONS.md` via CLAUDE.md — "no `utils.py`/`helpers.py` grab-bag; shared code lives in the module that owns the concept"]

---

### Pattern 1: `Database` — one connection, one lock, materialised results

**What:** `connect()` returns a `Database` that owns the `sqlite3.Connection` privately. Every query runs *and is fully fetched* while the lock is held. `transaction()` holds the lock across a statement group and commits or rolls back.

**When to use:** any process that will touch the connection from more than one thread. That is the HTTP process only — but `connect()` returning `Database` unconditionally is what keeps `evals.py` and `mcp_server.py` working unedited, since both then get an object that answers `execute`/`commit`/`close`/`executescript`/`executemany` identically.

**Why the result must be materialised — the part that is easy to get wrong:**

`Database.execute()` returning the real `sqlite3.Cursor` looks correct and passes every single-threaded test. It is not correct. The caller steps the cursor (`.fetchone()`, `.fetchall()`) *after* `execute()` released the lock, so another thread's statement can interleave with an in-flight cursor on the same connection. Measured on this codebase's own concurrency test (6 overlapping `/process` runs), 5 runs:

| Wrapper variant | Result |
|---|---|
| Lock per statement, returns live `sqlite3.Cursor` | **4 of 5 runs failed.** Failures: `HTTPException 404: ticket not found` (×3), `ValidationError: customer_email Input should be a valid string, input_value=None`, `ValidationError: status Input should be 'open'\|'resolved'\|'escalated', input_value=''` |
| Lock per statement, returns materialised `Result` | **5 of 5 passed** |

[VERIFIED: executed in this repo against a shadow copy of `src/relay/`, 2026-08-09]

Note what the failure mode actually is. It is **not** `sqlite3.OperationalError: database is locked` — the error every piece of prior research and every StackOverflow answer tells you to expect. It is silently malformed rows. A test that only asserts "no `OperationalError`" would pass while the app hands the model a customer record with a `None` email.

**API surface that must be covered.** A full audit of `src/` and `tests/` found exactly six connection methods in use — `execute` (28 sites), `commit` (10), `close` (3), `executescript` (1), `executemany` (1), plus `row_factory` set once in `connect()`. There is no `with conn:`, no `cursor()`, no `isolation_level`. Cursor attributes used: `.fetchone()`, `.fetchall()`, `.lastrowid`, and iteration. That is the complete contract the wrapper must satisfy. [VERIFIED: `grep -rnoE "(conn|db)\.[a-z_]+\(" src tests | sort | uniq -c`]

**Example:**
```python
# db.py — verified against the full 110-test suite plus 12 new tests
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Result:
    """A materialised query result covering the slice of sqlite3.Cursor this
    codebase uses.

    Rows are fetched while Database's lock is still held. Returning the real
    cursor instead lets the caller step it *after* the lock is released, so a
    concurrent statement on the same connection interleaves with an in-flight
    read — which surfaces not as "database is locked" but as rows with null and
    empty-string columns.
    """

    __slots__ = ("_i", "_rows", "description", "lastrowid", "rowcount")

    def __init__(self, rows, lastrowid, rowcount, description):
        self._rows, self._i = rows, 0
        self.lastrowid, self.rowcount, self.description = lastrowid, rowcount, description

    def fetchone(self) -> sqlite3.Row | None:
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[sqlite3.Row]:
        rows, self._i = self._rows[self._i:], len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[sqlite3.Row]:
        rows = self._rows[self._i:self._i + size]
        self._i += len(rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Database:
    """The single owner of the process's SQLite connection.

    The connection is private: nothing outside this class can issue a statement
    without holding the lock, which is what makes commit() this object's
    business rather than five call sites' business. Re-entrant so transaction()
    can call execute().
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def _run(self, method: str, *args) -> Result:
        with self._lock:
            cur = getattr(self._conn, method)(*args)
            return Result(cur.fetchall(), cur.lastrowid, cur.rowcount, cur.description)

    def execute(self, sql: str, params=()) -> Result:
        return self._run("execute", sql, params)

    def executemany(self, sql: str, seq) -> Result:
        return self._run("executemany", sql, seq)

    def executescript(self, script: str) -> Result:
        return self._run("executescript", script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """Hold the connection for one unit of work.

        commit() is connection-scoped, so an INSERT+UPDATE pair that releases the
        lock between statements can be committed halfway through by another
        request — and its own rollback then undoes nothing.
        """
        with self._lock:
            try:
                yield self
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise


def connect(db_path: str | Path) -> Database:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Silently a no-op on :memory: — see tests/test_db.py, which asserts that.
    conn.execute("PRAGMA journal_mode = WAL")
    # Python's sqlite3 already sets this from connect(timeout=5.0); stated
    # explicitly so a future timeout= change cannot silently zero it.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return Database(conn)
```

**Python-floor note:** nothing above uses `sqlite3.Connection.autocommit`, which is 3.12+ and would break the declared `requires-python = ">=3.11"`. Legacy transaction control (implicit `BEGIN` before DML) is what `transaction()` relies on. [VERIFIED: `hasattr(sqlite3.Connection, "autocommit")` is True on 3.14 but the attribute was added in 3.12; `pyproject.toml` floor is 3.11]

---

### Pattern 2: One offload seam, sync executors below it

**What:** `_execute_guarded` stays a plain sync function. `agent.py` calls it through `asyncio.to_thread`. `mcp_server.call_mcp_tool` keeps calling it directly — unchanged.

**Why this preserves OTel span parenting:** `asyncio.to_thread` runs the target inside `contextvars.copy_context()`, so the `tracer.start_as_current_span(f"tool.{name}", context=run_ctx)` that wraps the call is still current inside the worker thread. Verified: a `ContextVar` set on the loop read back correctly from the worker thread. No span-parenting change is needed and `grep -c 'async with' src/relay/agent.py` stays 0. [VERIFIED: executed]

**Cancellation semantics you must know:** cancelling the task while it awaits `to_thread` returns *immediately* from the `await`, but **the worker thread runs to completion**. A tool's DB write therefore lands even though the run was abandoned mid-call. This is safe (the write is inside a transaction and either commits or rolls back), but it means "client disconnected" does not mean "no side effect". [VERIFIED: executed — the thread's side effect appeared after the `CancelledError`]

**Concurrency ceiling:** `asyncio.to_thread` uses the loop's default executor, `max_workers = min(32, os.cpu_count() + 4)`. Measured 12 on this laptop; on Fly's `shared-cpu-1x` that is likely 5. So real thread concurrency against the connection is 5-12 — small, but more than 1, which is all the hazard needs. [VERIFIED: `loop._default_executor._max_workers`]

**Example:**
```python
# agent.py — the only new await in the loop
result, is_error = await asyncio.to_thread(
    _execute_guarded,
    spec, block.name, block.input, policy,
    bound_ticket_id=ticket["id"],
)
```

---

### Pattern 3: `RunRegistry` — in-flight runs as explicit state

**What:** a small object holding `{token: ActiveRun}` plus an `asyncio.Event` that is *set* exactly when the map is empty. Registration returns a token; deregistration is idempotent.

**Where it lives:** created in `lifespan`, stored on `app.state.runs`. Not a module-level singleton — an `asyncio.Event` caches the loop it is first awaited on, and a module-level instance shared across a pytest session that creates a fresh loop per `TestClient` context is a latent `RuntimeError: ... bound to a different event loop`. Scoping to `app.state` gives one registry per app startup, which is one per `TestClient` context. [VERIFIED: 12 candidate tests green, including three that construct their own registry]

**Where registration goes — the non-obvious part:** *inside* `event_stream`'s body, not next to `reserve_run()` in the handler. This codebase already learned why: `ratelimit.py`'s reservation TTL exists because "Starlette can cancel a streaming response before its generator is ever started, and a `finally` in an async generator that never began does not run." Register outside the generator and every aborted-before-start request leaks a registry entry, which (a) stalls every future shutdown for the full grace period and (b) violates D-06's "an idle server holds nothing". Register inside the body and registration/deregistration are exactly balanced with no TTL needed. [VERIFIED: `test_a_stream_that_never_starts_registers_nothing` passes with in-body registration; the existing `test_a_stream_that_never_starts_leaks_only_until_the_ttl` documents the same asymmetry for reservations]

**Why Phase 5 gets this for free:** `snapshot()` returns the live `ActiveRun` records, which is exactly the "what is running right now" projection DASH-01's feed needs on first connect.

---

### Pattern 4: Three nested timeouts, and only one lives in Python

**What:** the shutdown window is a budget split three ways. They must nest, and the outermost is Fly's.

| Layer | Setting | Recommended | What it bounds |
|-------|---------|-------------|----------------|
| Platform | `fly.toml` top-level `kill_timeout` | `30` | `kill_signal` → SIGKILL. Default **5**, max **300**. At 5s the drain is decorative. |
| Server | uvicorn `--timeout-graceful-shutdown` | `20` | How long uvicorn waits for in-flight connections before cancelling their tasks. Default is **`None` = wait forever**. |
| App | `settings.shutdown_drain_seconds` | `5` | Safety net in the lifespan: wait for `finally` blocks of tasks uvicorn cancelled but did not await, before `conn.close()`. |
| — | headroom | `~5` | Fly's own guidance: "if you wait the full 30s, Fly's SIGKILL arrives before your exit(0) runs." |

Fly docs recommend exactly this shape: "set `kill_timeout` to at least 30 seconds and implement application-level shutdown logic that refuses new jobs upon receiving SIGTERM, then waits for active work to complete before exiting." [CITED: https://fly.io/docs/blueprints/long-running-tasks/]

**Refusing new work during drain.** `RunRegistry.drain()` sets `draining = True` first. `process_ticket` should check it before `reserve_run()` and return 503 with `Retry-After`. Without it, a request arriving mid-drain extends the window and can outlive `conn.close()`. Note this is best-effort only — by the time the lifespan runs, uvicorn has already stopped accepting connections.

---

### Anti-Patterns to Avoid

- **Wrapping the existing shared connection in `to_thread` without changing topology.** PITFALLS.md calls this "a regression disguised as a fix" and rates it *never acceptable*. Confirmed empirically above.
- **A `Database.execute()` that returns the live cursor.** Looks right, passes single-threaded tests, corrupts rows under load. This is the trap *beyond* the one the prior research documented.
- **`async with` around the agent loop.** `agent.py`'s module comment and CONTEXT.md both forbid it (`grep -c 'async with' src/relay/agent.py` must stay 0). Nothing in this design needs one — `to_thread` is an `await`, not an `async with`, and it sits inside a block containing no `yield`.
- **A module-level `RunRegistry` singleton.** Cross-event-loop `asyncio.Event` reuse; scope it to `app.state`.
- **Registering the run in `process_ticket` rather than in `event_stream`.** Leaks on never-started streams; breaks D-06.
- **Trusting `--timeout-graceful-shutdown` alone.** It cancels tasks and moves on without awaiting them; the `record_run` in the `finally` can still be racing `conn.close()`.
- **Asserting `PRAGMA journal_mode == 'wal'` against a `:memory:` fixture.** It can never be true; see Pitfall 2.
- **Testing concurrency only for absence of `OperationalError`.** The real failure mode here is malformed rows.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Blocking-call offload | A custom thread pool + future plumbing | `asyncio.to_thread` | Handles contextvar propagation (needed for OTel), executor reuse, and exception marshalling. Rolling it loses span parenting silently. |
| Waiting for concurrent writers | A hand-rolled retry/backoff loop around `OperationalError` | `PRAGMA busy_timeout` | SQLite's own writer wait, in C, with no busy-spin. |
| Reader/writer contention | A reader-writer lock | `PRAGMA journal_mode = WAL` | WAL lets readers proceed during a write; a Python RW-lock cannot, because the contention is inside SQLite. |
| Transaction atomicity | Manual `BEGIN`/`COMMIT` strings | `Database.transaction()` over sqlite3's implicit transactions | A raw `BEGIN` string on a connection whose Python-level transaction state disagrees produces `cannot start a transaction within a transaction`. |
| Drain timeout | `while active: await sleep(0.05)` with a monotonic deadline | `asyncio.wait_for(event.wait(), timeout)` | Wakes on the event rather than polling; the poll version adds up to 50 ms to every clean shutdown for no benefit. |
| Signal handling | A `signal.signal(SIGTERM, ...)` handler in `main.py` | uvicorn's own handler + FastAPI `lifespan` | uvicorn already installs handlers for SIGINT *and* SIGTERM and sequences them against connection draining. A second handler fights it. [VERIFIED: `uvicorn/server.py:36` `HANDLED_SIGNALS = (SIGINT, SIGTERM)`] |

**Key insight:** every "custom" thing this phase is tempted to build already exists one layer down — in SQLite's C code, in asyncio, or in uvicorn. The only thing genuinely missing is the *ordering* glue between them, which is precisely what the registry is.

## Runtime State Inventory

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | `PRAGMA journal_mode = WAL` is written into the **database file header** and persists — the live `/data/relay.db` on Fly converts to WAL on the first boot after deploy and stays WAL. Two sidecar files (`relay.db-wal`, `relay.db-shm`) appear on the volume. No schema or row migration is needed; no existing row changes shape. | None — one-way, self-applying, backward compatible. Confirm the `relay_data` volume has headroom (it does; the DB is ~32 KB). Verified: journal mode read back as `wal` from a second, independent connection. |
| **Live service config** | `fly.toml` `kill_timeout` (currently **absent** → Fly's 5 s default) must be set to `30`. It is a **top-level** key, so it must be placed before the first `[table]` — i.e. after `primary_region`, not inside `[http_service]`. | Edit `fly.toml`, redeploy. A `fly deploy` is required for it to take effect; the setting is not hot-reloadable. |
| **OS-registered state** | None. No systemd units, no cron, no Task Scheduler, no pm2 — the container's PID 1 is uvicorn (via `sh -c`). | None — verified by reading `Dockerfile` and `fly.toml`; there is no process manager. |
| **Secrets / env vars** | One new setting, `RELAY_SHUTDOWN_DRAIN_SECONDS` (defaulted in `config.py`, so unset is fine). No secret changes. No `fly secrets set` needed. `.env.example` should gain the key for discoverability. | Add to `config.py` with a default; optional `.env.example` line. |
| **Build artifacts** | The `Dockerfile` `CMD` string must gain `--timeout-graceful-shutdown 20`; it is baked into the image, so the change ships only on the next `docker build`/`fly deploy`. **Also flag:** the CMD is `["sh","-c","uvicorn …"]`. If `sh` does not `exec` the child, `sh` is PID 1 and never forwards SIGTERM, and graceful shutdown silently never happens. Both `bash` and `dash` do exec a single simple command, and a local test confirmed the process was replaced — but making it `exec uvicorn …` removes all doubt at zero cost and zero risk. | Edit `Dockerfile` CMD (add the flag and an explicit `exec`); rebuild. The CI `docker` job already builds and health-checks the image, so a broken CMD fails CI. |

## Common Pitfalls

### Pitfall 1: The lock-per-statement wrapper that still corrupts rows

**What goes wrong:** `Database.execute()` acquires a lock, calls `conn.execute()`, releases the lock, and returns the live `sqlite3.Cursor`. The caller then steps that cursor outside the lock while another thread issues statements on the same connection.

**Why it happens:** every reference implementation of "thread-safe SQLite wrapper" you will find guards the *statement*, because the documented failure is `database is locked` and that failure is statement-scoped. Cursor sharing is a different bug with a different signature.

**How to avoid:** materialise inside the lock (`cur.fetchall()` before releasing) and return a `Result` object carrying rows plus `lastrowid`/`rowcount`. Every call site in this codebase already does `.fetchone()`/`.fetchall()`/`.lastrowid`, so nothing changes for callers.

**Warning signs:** intermittent `404 ticket not found` on a ticket that provably exists; `pydantic ValidationError` where a `NOT NULL TEXT` column arrives as `None` or `''`; a concurrency test that passes on some runs and not others. In the measured A/B, the naive wrapper failed 4 of 5 identical runs — flaky, not deterministic, which is the worst possible way to find this.

---

### Pitfall 2: WAL is a silent no-op on `:memory:` — but the fixture picture is better than CONTEXT.md says

**What goes wrong:** `PRAGMA journal_mode = WAL` on an in-memory database returns `memory` and raises nothing. Any assertion written against a `:memory:` fixture is vacuous.

**Verified:**
```
:memory:  PRAGMA journal_mode=WAL  ->  'memory'
file      PRAGMA journal_mode=WAL  ->  'wal'   (persists to a new connection)
```

**Correction to CONTEXT.md:** "Every current fixture uses `:memory:`" is **not accurate**. `conftest.py`'s `client` fixture already points `settings.db_path` at `tmp_path / "test.db"` — a real file. So every API-level test (the majority of the 110, including all of `test_auth.py`, `test_observability.py`, and the API half of `test_ratelimit.py`) already runs against a file-backed, WAL-eligible database. Only the `conn` fixture — used by `test_tools.py`, `test_mcp.py`, and the unit half of `test_guardrails.py` — is `:memory:`. [VERIFIED: `tests/conftest.py:40`]

**How to avoid:** add a *new* `db` fixture (`connect(tmp_path / "t.db")`) used only by the new `test_db.py`; leave the `conn` fixture alone. This satisfies D-03 completely — zero edits to existing fixtures — and gives real WAL coverage. Then write the trap down as an assertion so nobody "fixes" it later:

```python
def test_wal_is_a_silent_no_op_on_memory_databases():
    assert connect(":memory:").execute("PRAGMA journal_mode").fetchone()[0] == "memory"
```

**Bonus correction:** PITFALLS.md says "`PRAGMA busy_timeout` — the default is 0, which is why 'database is locked' fires instantly". That is true of the raw SQLite C API but **false for Python's `sqlite3`**, which maps `connect(timeout=5.0)` (its default) onto `sqlite3_busy_timeout(5000)`. Measured: `timeout=0 → 0`, default `→ 5000`, `timeout=30 → 30000`. Setting the pragma explicitly is still worth doing — it documents the value and survives a future `timeout=` change — but it is not the load-bearing fix the prior research implies. [VERIFIED: executed]

---

### Pitfall 3: `commit()` is connection-scoped, and a rollback then undoes nothing

**What goes wrong:** `send_reply` does INSERT → UPDATE → `commit()`. If another request commits between the INSERT and the UPDATE, the reply row becomes durable. If the first request then fails and calls `rollback()`, there is nothing left to roll back.

**Demonstrated on a real SQLite file:**

```
thread A: INSERT INTO replies (ticket_id=1)      # half of send_reply
thread B: INSERT INTO replies (ticket_id=2); commit()   # <- commits A's row too
thread A: raise; rollback()                      # undoes nothing
result:   replies = [(1,), (2,)]   tickets = [(1,'open'), (2,'open')]
          -> ticket 1 has a reply row and is still 'open'
```
With `Database.transaction()` in place, the same script yields `replies = [(2,)]`. [VERIFIED: both scripts executed]

**Where it applies:** five multi-statement writers — `tools.create_escalation`, `tools.send_reply`, `tools.set_category`, `main.create_ticket`, `telemetry.record_run`. All five are outside D-03's protected set. `db.init_db` also groups statements but runs single-threaded at startup.

**Warning signs:** `tickets.status = 'resolved'` with no matching `replies` row, or `'escalated'` with no `escalations` row. Worth a one-off query against the live Fly volume after this ships.

---

### Pitfall 4: Registering the run outside the generator leaks on never-started streams

**What goes wrong:** Starlette can cancel a `StreamingResponse` before the generator's first `__anext__`. A `finally` in a generator whose body never ran does not execute. Register in the handler and that entry never comes back, so the registry is non-empty on an idle server (D-06 violated) and every subsequent shutdown burns the full grace period.

**How to avoid:** first statement of `event_stream`'s body is `run_token = app.state.runs.register(ticket_id=ticket.id)`; deregistration goes alongside `release_run(token)` in the existing `finally`. Registration and deregistration then either both run or neither does.

**Warning signs:** `app.state.runs.active > 0` on an idle server; shutdown logs always showing `shutdown.drain_timeout`; Fly machine never reaching `stopped`.

---

### Pitfall 5: uvicorn cancels in-flight tasks but does not await them before running lifespan shutdown

**What goes wrong:** `conn.close()` in the lifespan teardown races the cancelled `event_stream`'s `finally`, which calls `record_run(app.state.conn, ...)`. Losing that race produces `sqlite3.ProgrammingError: Cannot operate on a closed database` inside the very code path Phase 1's CR-01 fix added, and the run's real Claude spend never reaches `runs.cost_usd` — which is what SEC-03's daily ceiling reads.

**Verified from uvicorn 0.52.0 source** (`uvicorn/server.py`, `Server.shutdown()`):

```python
# 3. wait for connections, bounded by timeout_graceful_shutdown (default None = forever)
try:
    await asyncio.wait_for(self._wait_tasks_to_complete(),
                           timeout=self.config.timeout_graceful_shutdown)
except asyncio.TimeoutError:
    for t in self.server_state.tasks:
        t.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")
        # <- cancel() only *schedules* CancelledError; nothing awaits it

# 4. lifespan shutdown runs next, immediately
if not self.force_exit:
    await self.lifespan.shutdown()
```

Also note `connection.shutdown()` in step 2 for an in-flight response merely sets `keep_alive = False` — the SSE stream is **not** interrupted. So without `--timeout-graceful-shutdown`, uvicorn waits for the agent run indefinitely, and Fly's 5 s `kill_timeout` SIGKILLs the process before the lifespan ever runs. [VERIFIED: source read at `.venv/lib/python3.14/site-packages/uvicorn/{server,config,protocols/http/h11_impl}.py`]

**How to avoid:** set `--timeout-graceful-shutdown`, and await the app-level registry in the lifespan *before* `conn.close()`. The registry drain is the only thing that observes the cancelled tasks' `finally` blocks completing.

**Warning signs:** `Cannot operate on a closed database` in Fly shutdown logs; `/metrics` run count lower than the number of streams actually started; Anthropic spend outrunning `runs.cost_usd`.

---

### Pitfall 6: Awaiting inside the `finally` of a cancelled async generator

**What goes wrong:** offloading `record_run` (`await asyncio.to_thread(record_run, ...)`) puts a new `await` into the exact path CR-01 just fixed. Awaiting in a `finally` after a `GeneratorExit` or `CancelledError` is a documented source of `RuntimeError: async generator ignored GeneratorExit`.

**Measured:** it works. Both `aclose()` (the path `test_mid_stream_disconnect_still_records_the_spend` exercises) and task cancellation ran the awaited offload to completion, on CPython 3.14. [VERIFIED: executed, four cases]

**Recommendation anyway: keep `record_run` synchronous in the `finally`.** It is one INSERT + commit — tens of microseconds — and the loop is about to go idle regardless. The cost of offloading is a new await in the most fragile fifteen lines in the app; the benefit is unmeasurable. If the planner offloads it anyway, `test_mid_stream_disconnect_still_records_the_spend` is the regression gate and must be run on CPython 3.12 (CI/Docker), not only locally.

---

### Pitfall 7: `enforce_daily_budget` still runs on the event loop

**What goes wrong:** `_gate`'s dependency calls `enforce_daily_budget(app.state.conn)` → `spent_today` → `SELECT COALESCE(SUM(cost_usd), 0.0) FROM runs WHERE created_at >= …`. That is an unindexed scan of the whole `runs` table (already flagged as WR-02 in `01-DEFERRED.md`), executed on the loop, and it will now also contend for `Database`'s lock.

**Options, in increasing order of scope:**
1. **Leave it.** Contention is microseconds today; the table has tens of rows.
2. **Add `CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at)` to `SCHEMA`.** One line, `IF NOT EXISTS`, applies on the next `init_db`. Directly addresses the growth risk.
3. **Offload it** — `await asyncio.to_thread(enforce_daily_budget, app.state.conn)`. `_gate._dependency` is already `async def` and `HTTPException` propagates through `to_thread` cleanly. Caveat: `spent_today` also reads `ratelimit._reservations`, a module-level dict mutated from the loop by `reserve_run`/`release_run`; moving the read to a worker thread makes that a genuine (if benign, GIL-atomic) cross-thread access.

**Recommendation:** (1) + (2). Offloading buys little and introduces cross-thread access to Phase 1's reservation state, which is code the phase is explicitly told not to disturb.

## Code Examples

Every snippet below was executed against a shadow copy of `src/relay/` and the real test suite.

### 1. `tools.py` writers become one unit of work

```python
def send_reply(db: Database, ticket_id: int, body: str) -> str:
    # Email delivery is mocked: the reply is persisted, nothing leaves the system.
    with db.transaction():
        cur = db.execute(
            "INSERT INTO replies (ticket_id, body) VALUES (?, ?)", (ticket_id, body)
        )
        db.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        reply_id = cur.lastrowid
    return json.dumps({"reply_id": reply_id, "status": "resolved"})
```

`lastrowid` must be read *inside* the block: after the lock drops, another thread's insert has already moved it. The executor stays sync — D-01/D-02 hold.

### 2. `main.py` handlers offloaded

```python
async def _get_ticket(ticket_id: int) -> Ticket:
    row = await asyncio.to_thread(
        lambda: app.state.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    )
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))


@app.post("/tickets", response_model=Ticket, status_code=201,
          dependencies=[Depends(create_gate)])
async def create_ticket(payload: TicketCreate) -> Ticket:
    def _insert() -> int:
        with app.state.conn.transaction() as db:
            cur = db.execute(
                "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
                (payload.customer_email, payload.subject, payload.body),
            )
            return cur.lastrowid

    return await _get_ticket(await asyncio.to_thread(_insert))


@app.get("/metrics")
async def metrics() -> dict:
    # run_metrics does SELECT * FROM runs; it is the one read that grows unbounded.
    return await asyncio.to_thread(run_metrics, app.state.conn)
```

Callers change from `_get_ticket(x)` to `await _get_ticket(x)` in three places (`create_ticket`, `get_ticket`, `process_ticket`). `test_mid_stream_disconnect_still_records_the_spend` already `await`s `process_ticket`, so it needs no change.

### 3. `runs.py`

```python
"""In-flight agent-run tracking.

Phase 2 uses it to drain SSE streams before the database closes; phase 5's live
feed reads the same records to render what is running right now. Deliberately
not part of ratelimit.py: that module is scoped to burst limiting and the spend
ceiling, and this is neither.
"""

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("relay.runs")


@dataclass(frozen=True)
class ActiveRun:
    ticket_id: int
    started_at: float


class RunRegistry:
    """Holds only runs that are actually streaming.

    An idle server holds nothing, which is what keeps Fly's autostop working —
    see tests/test_lifecycle.py::test_registry_is_empty_after_a_run_completes.
    Instantiated per app startup rather than at module level: an asyncio.Event
    binds to the loop it is first awaited on, and a module-level instance would
    outlive a TestClient's loop.
    """

    def __init__(self) -> None:
        self._active: dict[int, ActiveRun] = {}
        self._tokens = itertools.count()
        self._idle = asyncio.Event()
        self._idle.set()
        self.draining = False

    def register(self, *, ticket_id: int) -> int:
        token = next(self._tokens)
        self._active[token] = ActiveRun(ticket_id=ticket_id, started_at=time.monotonic())
        self._idle.clear()
        return token

    def deregister(self, token: int) -> None:
        self._active.pop(token, None)
        if not self._active:
            self._idle.set()

    @property
    def active(self) -> int:
        return len(self._active)

    def snapshot(self) -> list[ActiveRun]:
        return list(self._active.values())

    async def drain(self, *, timeout: float) -> bool:
        """Stop admitting runs, then wait for the in-flight ones. True if drained."""
        self.draining = True
        if not self._active:
            return True
        logger.info("shutdown.drain_started", extra={"ctx": {"active": len(self._active)}})
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            logger.info("shutdown.drain_complete", extra={"ctx": {}})
            return True
        except TimeoutError:
            logger.warning(
                "shutdown.drain_timeout",
                extra={"ctx": {"active": len(self._active),
                               "tickets": [r.ticket_id for r in self._active.values()]}},
            )
            return False
```

### 4. Lifespan teardown ordering

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    setup_tracing()
    conn = connect(settings.db_path)
    init_db(conn)
    app.state.conn = conn
    app.state.registry = build_registry(conn, settings.kb_dir)
    app.state.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    app.state.runs = RunRegistry()
    yield
    # uvicorn cancels in-flight request tasks on graceful-shutdown timeout but does
    # not await the cancellations, so a stream's finally — which writes the run's
    # cost that the daily ceiling reads back — can still be pending here. Closing
    # the connection first turns that into "Cannot operate on a closed database"
    # and loses the row.
    await app.state.runs.drain(timeout=settings.shutdown_drain_seconds)
    conn.close()
```

### 5. Registration inside the generator

```python
    async def event_stream():
        started = time.perf_counter()
        # Registered here, not beside reserve_run() above: Starlette can cancel a
        # StreamingResponse before its generator ever starts, and a finally in a
        # generator that never began does not run. Registering outside the body
        # would leak an entry on every aborted request, stalling every future
        # drain and leaving an idle server non-empty.
        run_token = app.state.runs.register(ticket_id=ticket.id)
        usage: dict = {}
        outcome = "incomplete"
        recorded = False
        try:
            ...
        finally:
            if not recorded:
                recorded = True
                record_run(app.state.conn, ...)
            release_run(token)
            app.state.runs.deregister(run_token)
```

### 6. Platform configuration

```toml
# fly.toml — top-level, therefore BEFORE the first [table]
app = 'relay-agent'
primary_region = 'syd'
# Fly's default is 5s, which SIGKILLs the machine mid-run and skips the drain
# entirely. 30s covers a typical ~20s run plus the lifespan teardown, and stays
# under Fly's 300s maximum.
kill_timeout = 30

[build]
...
```

```dockerfile
# Dockerfile — explicit exec so uvicorn is PID 1 and receives the signal itself.
# --timeout-graceful-shutdown: uvicorn's default is None, i.e. wait forever for
# an in-flight SSE stream — which Fly then SIGKILLs. 20s leaves ~10s of the
# 30s kill_timeout for the lifespan drain and teardown.
CMD ["sh", "-c", "exec uvicorn relay.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 20"]
```

### 7. A test double for genuine write concurrency

`FakeClient` in `tests/helpers.py` plays a fixed script, so overlapping runs cannot share one. This variant answers whichever ticket the prompt names, which is what makes concurrent *writes* happen:

```python
class TicketAwareFakeClient:
    """One client for overlapping runs: replies to whichever ticket the prompt
    names, so concurrent runs issue genuinely concurrent writes."""

    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, *, messages, **kw):
        ticket_id = int(re.search(r"[Tt]icket #?(\d+)", messages[0]["content"]).group(1))
        if len(messages) == 1:
            return response([tool_use_block("send_reply",
                                            {"ticket_id": ticket_id, "body": "z" * 40})])
        return response([text_block("done")], stop_reason="end_turn")
```

Whether this lives in `tests/helpers.py` (which is a test double module, so it fits) or in the new test file is the planner's call — `helpers.py` is not in D-03's protected set, but adding to it is lower-churn than editing it.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `sqlite3.connect(timeout=…)` assumed unrelated to `busy_timeout` | Python's `sqlite3` sets `busy_timeout` from `timeout`, defaulting to 5000 ms | Long-standing CPython behaviour, widely mis-stated | The pragma is documentation, not a fix — correct the mental model |
| uvicorn waits for connections then exits | `--timeout-graceful-shutdown` added, defaulting to `None` (wait forever) | uvicorn PR #1824 | Must be set explicitly on any platform with a kill timeout |
| Fly `kill_signal` documented as `SIGINT` | Fly blueprint documents `SIGTERM` as the default | Docs disagree across pages (see Open Questions) | uvicorn handles both identically, so it does not change the design |
| SQLite `journal_mode=DELETE` default | WAL for any concurrent-reader workload | SQLite 3.7 (2010) | Table stakes; the notable part is that it is inert on `:memory:` |

**Deprecated / outdated in the existing planning docs:**
- STACK.md's `aiosqlite` + `sse-starlette` recommendation — superseded by D-01 and D-04.
- PITFALLS.md's "`busy_timeout` default is 0" — true for the C API, false for Python's `sqlite3`.
- CONTEXT.md's "Every current fixture uses `:memory:`" — the `client` fixture is already file-backed.
- ROADMAP.md's "existing 37-test suite" — the suite is **110**. Treat the criterion as "no regression".

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `sh -c "uvicorn …"` in the Dockerfile execs, so uvicorn is PID 1 and receives Fly's signal | Runtime State Inventory | Graceful shutdown never fires in production while every test passes. Locally verified on macOS `sh`; the container uses Debian `dash`. **Mitigation is free: write `exec uvicorn …` and the assumption disappears.** |
| A2 | A typical run is ~20 s, so a 20 s uvicorn graceful window covers most runs | Pattern 4 | Longer runs get cancelled rather than drained. Sourced from CONTEXT.md, not measured. Bounded anyway by `max_agent_steps=10` and `max_run_cost_usd=$0.50`, and the `finally` records the partial run either way. |
| A3 | Fly's proxy autostop is driven by connection state, so an in-process registry cannot affect it | D-06 / Architectural Map | If autostop also considered something else, D-06's test would be measuring the wrong thing. Fly's docs describe autostop in terms of proxy-observed concurrency and never mention process internals, but do not state the negative explicitly. The test is still worth having as a registry-leak detector. |
| A4 | Awaiting inside a cancelled async generator's `finally` behaves the same on CPython 3.12 (CI/Docker) as on 3.14 (verified locally) | Pitfall 6 | Only matters if the planner offloads `record_run`; the recommendation is not to. CI runs 3.12 and would catch it. |
| A5 | `os.cpu_count()` on Fly `shared-cpu-1x` yields a default executor of ~5 threads | Pattern 2 | Only affects the *degree* of concurrency, not whether the hazard exists. Any value > 1 is sufficient. |

## Open Questions

1. **Does Fly send SIGINT or SIGTERM first?**
   - What we know: `fly.io/docs/reference/configuration/` says "Fly.io sends a `SIGINT` signal to the running process by default"; `fly.io/docs/blueprints/long-running-tasks/` says "Fly sends `kill_signal` (default: `SIGTERM`) to PID 1."
   - What's unclear: which page is current.
   - Recommendation: **ignore it.** uvicorn's `HANDLED_SIGNALS` is `(SIGINT, SIGTERM)` and both take the identical graceful path. Do **not** set `kill_signal` explicitly — pinning the wrong one is the only way to get this wrong.

2. **Should `process_ticket` return 503 while draining?**
   - What we know: `RunRegistry.draining` makes it a one-line check, and it prevents an arriving request from extending the drain past `conn.close()`.
   - What's unclear: whether it is reachable in practice — uvicorn stops accepting connections before the lifespan runs, so the window is narrow.
   - Recommendation: include it. One line, one test, and it makes the shutdown contract explicit rather than accidental. Reuse `ratelimit.py`'s friendly-JSON-detail convention, not a bare string.

3. **Index `runs(created_at)` in this phase?**
   - What we know: `spent_today` scans the table on every gated request, on the event loop (WR-02).
   - What's unclear: whether a schema addition is in scope for a phase whose stated boundary is "how existing writes execute, not what new data is persisted".
   - Recommendation: include it. `CREATE INDEX IF NOT EXISTS` adds no table and no column, and this phase is the one that makes loop-blocking reads a first-class concern.

4. **Where does `TicketAwareFakeClient` live?**
   - `tests/helpers.py` is the natural home and is not in D-03's protected set, but "existing tool tests untouched" is easier to demonstrate if `helpers.py` is only appended to, never modified. Planner's call.

5. **Do the stale `sqlite3.Connection` type hints in `mcp_server.py` and `evals.py` get fixed?**
   - `connect()` now returns `Database`, so those annotations become inaccurate. D-03 says both files stay untouched. Ruff does not type-check and there is no mypy, so nothing breaks.
   - Recommendation: leave them, and record it in the phase SUMMARY as a known, deliberate inaccuracy so a later reader does not treat it as a bug.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| CPython | everything | ✓ | 3.14.6 local / 3.12 CI+Docker / floor 3.11 | — |
| `sqlite3` with WAL support | DATA-01 | ✓ | SQLite 3.50.4 | — |
| `uvicorn` `--timeout-graceful-shutdown` | DATA-02 | ✓ | 0.52.0 (flag present, verified via `--help`) | — |
| `pytest` + `pytest-asyncio` (`asyncio_mode=auto`) | all new tests | ✓ | 9.1.1 / 1.4.0 | — |
| `ruff` | CI lint gate | ✓ | in venv, clean on current tree | — |
| Docker | CI `docker` job; container shutdown check | not verified locally | — | The CI `docker` job builds and health-checks the image on every push, so a broken CMD fails there |
| `flyctl` / a live Fly machine | verifying `kill_timeout` end to end | ✗ (deploy-time only) | — | Not closable in CI. Record as a deploy-time check alongside Phase 1's two open deploy-time items in `01-DEFERRED.md` |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** Docker locally (CI covers it); live Fly verification (deploy-time checklist item).

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `pytest 9.1.1` + `pytest-asyncio 1.4.0`, `asyncio_mode = "auto"` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `pytest -q tests/test_db.py tests/test_lifecycle.py` |
| Full suite command | `pytest -q` |
| Baseline | **110 passed in 0.93s**; `ruff check src tests` → All checks passed! (measured 2026-08-09 on the current HEAD, `8842c87`) |

### Phase Requirements → Test Map

All twelve rows below were **written and executed green** against a shadow implementation (`122 passed`). Names and shapes are handed over as-is.

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DATA-01-a | WAL, `busy_timeout=5000`, `foreign_keys=ON` on a file DB | unit | `pytest tests/test_db.py::test_wal_is_enabled_on_a_file_database -x` | ❌ Wave 0 |
| DATA-01-b | WAL is a silent no-op on `:memory:` (the trap, written down) | unit | `pytest tests/test_db.py::test_wal_is_a_silent_no_op_on_memory_databases -x` | ❌ Wave 0 |
| DATA-01-c | FK enforcement survives the wrapper | unit | `pytest tests/test_db.py::test_foreign_keys_still_enforced -x` | ❌ Wave 0 |
| DATA-01-d | A failed write leaves no partial row when another thread commits concurrently (deterministic, barrier-driven) | unit | `pytest tests/test_db.py::test_a_failed_write_does_not_leave_a_partial_row_when_another_thread_commits -x` | ❌ Wave 0 |
| DATA-01-e | **D-02:** no registered executor — agent registry *or* MCP registry — is a coroutine function | unit | `pytest tests/test_lifecycle.py::test_no_registered_executor_is_a_coroutine_function -x` | ❌ Wave 0 |
| DATA-01-f | Tool execution runs off the main thread (proves the offload, not just that it still works) | unit | `pytest tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop -x` | ❌ Wave 0 |
| DATA-01-g | 6 overlapping `/process` runs → 6 `runs` rows, 6 `replies`, 6 resolved tickets, no error | integration | `pytest tests/test_lifecycle.py::test_overlapping_runs_all_record_without_locking_errors -x` | ❌ Wave 0 |
| DATA-02-a | **D-06:** registry is empty after a run completes | integration | `pytest tests/test_lifecycle.py::test_registry_is_empty_after_a_run_completes -x` | ❌ Wave 0 |
| DATA-02-b | Drain returns immediately (<50 ms) when idle | unit | `pytest tests/test_lifecycle.py::test_drain_returns_immediately_when_idle -x` | ❌ Wave 0 |
| DATA-02-c | Drain waits for an in-flight run, then returns True | unit | `pytest tests/test_lifecycle.py::test_drain_waits_for_an_in_flight_run_then_returns -x` | ❌ Wave 0 |
| DATA-02-d | Drain returns False on timeout rather than hanging shutdown | unit | `pytest tests/test_lifecycle.py::test_drain_times_out_rather_than_hanging_shutdown -x` | ❌ Wave 0 |
| DATA-02-e | A stream cancelled before its generator starts registers nothing | integration | `pytest tests/test_lifecycle.py::test_a_stream_that_never_starts_registers_nothing -x` | ❌ Wave 0 |
| DATA-02-f | **(D-07 regression, already exists — must stay green)** mid-stream disconnect still records the spend | integration | `pytest tests/test_observability.py::test_mid_stream_disconnect_still_records_the_spend -x` | ✅ exists |

**Not automatable in CI — deploy-time checklist:**
- `kill_timeout = 30` observed on the live machine (`fly config show`), and a `fly deploy` during an active run shows the drain log line rather than a truncated stream.
- Machine still reaches `stopped` when idle (`fly machine list`) after this phase — D-06's real-world counterpart.
- Optional and cheap, worth adding to the CI `docker` job: `docker stop --time=35 relay` then assert exit code 0 and `shutdown.drain_complete` in `docker logs`. This is the only automatable end-to-end check of the signal path (and it would catch assumption A1).

### Sampling Rate

- **Per task commit:** `pytest -q tests/test_db.py tests/test_lifecycle.py` (< 2 s)
- **Per wave merge:** `pytest -q && ruff check src tests` — must show ≥ 110 passing and zero lint errors
- **Phase gate:** full suite green + the concurrency test run at least 5 consecutive times (it is the one test whose failure mode is flaky rather than deterministic — the naive wrapper failed 4 of 5)

### Wave 0 Gaps

- [ ] `tests/test_db.py` — covers DATA-01-a..d
- [ ] `tests/test_lifecycle.py` — covers DATA-01-e..g, DATA-02-a..e
- [ ] `tests/conftest.py` — **append only**: a file-backed `db` fixture (`connect(tmp_path / "relay.db")`). Existing `conn`, `registry`, `client`, `_reset_limits` fixtures must not change (D-03).
- [ ] `tests/helpers.py` — `TicketAwareFakeClient` (append only), or inline it in `test_lifecycle.py`
- [ ] Framework install: none — pytest and pytest-asyncio are already configured

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Unchanged by this phase; Phase 1's `auth.py` perimeter is untouched |
| V3 Session Management | no | No sessions; static API keys |
| V4 Access Control | indirect | `ToolPolicy` and the `bound_ticket_id` guard both sit *inside* `_execute_guarded`, which now runs in a worker thread. Both are pure-function checks on their arguments with no shared mutable state, so thread placement does not weaken them — but `test_concurrent_runs_do_not_cross_bind` (existing) is the proof and must stay green |
| V5 Input Validation | yes | `validate_tool_input` (Pydantic) is unchanged and still runs before execution |
| V6 Cryptography | no | None introduced |
| V7 Error Handling & Logging | yes | New `shutdown.*` log events carry `ticket_id` only — never bodies, emails, or keys, per `JsonFormatter`'s `ctx` passthrough hazard |
| V12 Secure File Upload / V13 API | partial | The 503-while-draining response must use `ratelimit.py`'s structured-detail convention and leak no internal state |

### Known Threat Patterns for Python/asyncio + SQLite

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Cross-request transaction commit on a shared connection | Tampering | `Database.transaction()` — the lock is the transaction boundary, not the statement boundary |
| Cross-request row leakage via a shared, unlocked cursor | Information Disclosure | Materialise results inside the lock (`Result`). The observed symptom is malformed rows; a subtler variant would return *another request's* rows |
| Lost audit/spend record on shutdown | Repudiation | Drain before `conn.close()`. `runs.cost_usd` is not decoration — SEC-03's daily ceiling reads it, so a lost row is a spend-control bypass |
| Unbounded shutdown wait blocking a deploy | DoS | `asyncio.wait_for` timeout + Fly `kill_timeout` as the outer bound |
| Registry entry leaked by an aborted stream | DoS (self-inflicted) | Register inside the generator body — the same failure class as Phase 1's CR-02 leaked reservation, which was "a remotely triggerable outage" |
| SQL injection | Tampering | Unchanged — every statement is parameterised; the wrapper passes `params` through untouched and adds no string formatting |

**Explicit non-goal:** WR-01 (TOCTOU between the budget check and the reservation) lives in code this phase touches and is deferred by `01-DEFERRED.md`. Do not fix it here. Nothing in this design makes it worse — the reservation path is untouched.

## Sources

### Primary (HIGH confidence — executed or read in this repo, 2026-08-09)

- `src/relay/{main,agent,db,tools,telemetry,ratelimit,mcp_server,evals,config}.py`, `tests/*`, `Dockerfile`, `fly.toml`, `.github/workflows/ci.yml`, `pyproject.toml` — read in full
- Baseline measurement: `pytest -q` → 110 passed; `ruff check src tests` → clean
- Shadow-implementation runs: full change set → **110 passed, zero test edits**; with 12 new tests → **122 passed**
- A/B measurement of the cursor-materialisation hazard: naive wrapper 1/5 pass, materialised wrapper 5/5 pass
- Executed SQLite probes: WAL on `:memory:` → `memory`; WAL on file → `wal` (persistent); `busy_timeout` default `5000`, `timeout=0` → `0`, `timeout=30` → `30000`; `sqlite3.threadsafety == 3`; SQLite `3.50.4`
- Executed asyncio probes: `to_thread` contextvar propagation; cancellation leaves the worker thread running; default executor `max_workers`; `await` inside a cancelled async generator's `finally` under both `aclose()` and `task.cancel()`
- Executed partial-commit demonstration on a real file DB (with and without `transaction()`)
- `uvicorn 0.52.0` source: `server.py` (`Server.shutdown`, `_wait_tasks_to_complete`, `HANDLED_SIGNALS`), `config.py` (`timeout_graceful_shutdown: int | None = None`), `protocols/http/h11_impl.py` (`shutdown()` sets `keep_alive = False` on an in-flight cycle)
- `pip show mcp` → `sse-starlette` is a transitive dependency, undeclared in `pyproject.toml`

### Secondary (MEDIUM-HIGH — official docs)

- https://fly.io/docs/blueprints/long-running-tasks/ — `kill_signal` default `SIGTERM`, `kill_timeout` → SIGKILL, "at least 30 seconds", the safety-margin warning
- https://fly.io/docs/reference/configuration/ — `kill_timeout` is a top-level integer, default 5, max 300; `kill_signal` value list (states SIGINT as the default — see Open Question 1)
- https://fly.io/docs/reference/fly-proxy-autostop-autostart/ — autostop is a proxy-side capacity decision; `kill_signal`/`kill_timeout` are honoured by the proxy when it stops a machine
- https://uvicorn.dev/settings/ and https://github.com/Kludex/uvicorn/pull/1824 — `--timeout-graceful-shutdown` semantics

### Prior planning research (context, partially superseded)

- `.planning/research/ARCHITECTURE.md` Pattern 3 / Anti-Pattern 4 — the `to_thread`-over-`aiosqlite` argument; the `Database` sketch there lacks result materialisation
- `.planning/research/PITFALLS.md` #9, #12 — shared-connection threading and the `:memory:` WAL trap; two factual corrections noted above
- `.planning/research/STACK.md` — `aiosqlite`/`sse-starlette` recommendations, superseded by D-01/D-04
- `.planning/codebase/CONCERNS.md`, `.planning/phases/01-security-perimeter/{01-05-SUMMARY,01-DEFERRED}.md`

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — no new packages; every version confirmed from the live venv
- Connection ownership / `Database` design: **HIGH** — designed, implemented, and A/B-measured against this repo's own suite
- The `to_thread` seam and its side effects: **HIGH** — contextvar propagation, cancellation, and executor sizing all executed
- Drain registry design: **HIGH** — implemented and covered by five executed tests
- uvicorn shutdown ordering: **HIGH** — read from the installed 0.52.0 source, not from docs
- Fly platform behaviour: **MEDIUM-HIGH** — official docs, with one documented internal contradiction (Open Question 1) that does not affect the design
- Container signal delivery (`sh -c` exec): **MEDIUM** — verified on macOS `sh`, not in the Debian container; mitigated to a non-issue by writing `exec`
- "Which existing tests break": **HIGH** — measured as *none*, by running the real suite against a shadow implementation

**Research date:** 2026-08-09
**Valid until:** 2026-09-08 (30 days — stdlib and platform behaviour, all stable; re-check only if `uvicorn` or `fly.toml` semantics change)

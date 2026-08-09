# Phase 2: Async-Safe Data Layer & Graceful Shutdown - Context

**Gathered:** 2026-08-09
**Status:** Ready for planning

<domain>
## Phase Boundary

Make every SQLite access async-safe without breaking the sync tool contract, and drain in-flight SSE runs on shutdown instead of severing them. Requirements: DATA-01, DATA-02.

This is the widest-blast-radius phase in the milestone — it must land before Phase 3 (retrieval) and Phases 5/6 (dashboard) add new call sites, or those get refactored twice.

**Explicitly NOT in this phase:** the `run_events` table (DATA-03) belongs to Phase 5. This phase touches how existing writes execute, not what new data is persisted.

</domain>

<decisions>
## Implementation Decisions

### Async DB seam
- **D-01:** Async-safety comes from a **single `asyncio.to_thread` offload seam**, not an `aiosqlite` rewrite. Offload at the `_execute_guarded` call site in `agent.py`, plus the direct DB calls in the HTTP handlers. `ToolSpec.execute` stays a sync `Callable[..., str]`.
- **D-02:** The sync-executor contract is enforced **mechanically, by test** — assert no registered `ToolSpec.execute` is a coroutine function. Rationale: the failure mode is silent (a coroutine object is returned as a tool result — no exception, garbage into the model's context) and there is no mypy in CI to catch it. This test converts a silent corruption into a loud CI failure.
- **D-03:** `mcp_server.py`, `evals.py`, and all existing tool tests must remain untouched by the async change. If a proposed approach requires editing them, it is the wrong approach.

### Shutdown drain
- **D-04:** Drain via a **hand-rolled in-flight task registry**, not `sse-starlette`'s built-in SIGTERM hook. Rationale: Phase 5's live feed needs the same registry, so building it here avoids building it twice; it also avoids a new dependency that owns the response type. (This deliberately overrides STACK.md's `sse-starlette` recommendation — ARCHITECTURE.md's registry approach wins on reuse.)
- **D-05:** Lifespan shutdown awaits in-flight runs with a **~30s grace period** before closing the DB. A typical run is ~20s and is bounded by the existing step/budget caps. `fly.toml` needs a matching `kill_timeout` — Fly's default SIGTERM→SIGKILL window is 5s, which would defeat the drain.
- **D-06:** The registry holds **only active agent runs**. An idle server holds nothing, so Fly's autostop still suspends the machine. This must be covered by an explicit test asserting the registry is empty after a run completes — scale-to-zero is a core-value constraint ("cheap to keep running"), not a nice-to-have.

### Scope boundary
- **D-07:** DATA-02's "record on interruption" half is **already done** — Phase 1's CR-01 fix (`b6da97e`) moved `record_run` into a `finally` in `event_stream`, with a `recorded` guard. This phase must **preserve** that behaviour (a regression test already exists: `test_mid_stream_disconnect_still_records_the_spend`) and add only the shutdown-drain half.

### Claude's Discretion
- Connection ownership specifics: whether to keep one shared connection behind a lock, adopt connection-per-thread, or introduce a thread-safe `Database` wrapper. Derive from D-01. **Hard constraint:** today's single shared connection is only accidentally correct because access is single-threaded — handing that same connection to worker threads makes `commit()` cross-request, committing another request's partial transaction. Whatever is chosen must make connection ownership explicit.
- WAL and `busy_timeout` pragma placement, and how tests stay representative given the trap below.
- Whether the MCP server's independently-opened connection needs the same treatment.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Research (milestone-level)
- `.planning/research/ARCHITECTURE.md` — the `to_thread`-over-`aiosqlite` argument (blast radius through `ToolSpec.execute`), shutdown/teardown ordering
- `.planning/research/PITFALLS.md` — the two traps that make naive fixes regressions: shared-connection `commit()` semantics under threads, and WAL being a silent no-op on `:memory:`
- `.planning/research/STACK.md` — recommends `sse-starlette` for shutdown; **superseded by D-04**, kept for the SIGTERM-hook details

### Codebase map
- `.planning/codebase/CONCERNS.md` — the blocking-SQLite and shared-connection concerns this phase closes
- `.planning/codebase/ARCHITECTURE.md` — current layering and `app.state` singletons
- `.planning/codebase/CONVENTIONS.md` — naming, error handling, logging patterns

### Phase 1 (immediately prior — read before touching `main.py`)
- `.planning/phases/01-security-perimeter/01-05-SUMMARY.md` — final state of the perimeter
- `.planning/phases/01-security-perimeter/01-DEFERRED.md` — deferred warnings; **WR-01 (TOCTOU between budget check and reservation) lives in the code this phase touches.** Do not fix it here unless it falls out for free; it is scheduled for gap closure.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_execute_guarded` in `src/relay/agent.py` — the single tool-execution choke point; already gained `bound_ticket_id` in Phase 1. The one seam D-01 offloads.
- `lifespan()` in `src/relay/main.py` — owns `app.state.conn` creation/teardown; the drain hooks in here, before `conn.close()`.
- `event_stream()`'s `finally` in `src/relay/main.py` — already holds `record_run` + `release_run` (Phase 1 CR-01/CR-02). The registry's deregistration belongs alongside them.
- `reserve_run`/`release_run` in `src/relay/ratelimit.py` — an existing token-identified, TTL-expiring in-flight tracker. The drain registry is a similar shape and may share the pattern.

### Established Patterns
- Guardrails and lifecycle state are explicit and testable; no context managers span the agent loop's `yield`s (`grep -c 'async with' src/relay/agent.py` must stay 0).
- Structured logging via `logger.x("dotted.event", extra={"ctx": {...}})`.
- Tests use `:memory:` SQLite via fixtures in `tests/conftest.py`, plus a shared authed `client` fixture and an autouse limiter-reset fixture (Phase 1).

### Integration Points
- `src/relay/db.py:62` — `sqlite3.connect(db_path, check_same_thread=False)`; connection factory and pragma home.
- `src/relay/main.py` — lifespan (drain + teardown ordering), handlers (offload), `event_stream` finally (deregister).
- `src/relay/agent.py` — the `_execute_guarded` await seam.
- `fly.toml` — `kill_timeout` must match D-05's grace period.

</code_context>

<specifics>
## Specific Ideas

- Two traps from research that a naive implementation hits, both worth explicit test coverage:
  1. **Shared connection across threads:** `commit()` is connection-scoped, so offloading the *existing* shared connection to worker threads makes one request commit another's partial transaction. Today's blocking behaviour is accidentally correct; a careless `to_thread` is a regression, not a fix.
  2. **WAL on `:memory:` is a silent no-op** — SQLite leaves the journal mode unchanged. Every current fixture uses `:memory:`, so a WAL pragma would appear to work while being inert across all 110 tests. Any WAL assertion needs a file-backed database.
- Success criterion 2 in ROADMAP.md references "the existing 37-test suite" — that count is stale (the suite is 110 after Phase 1). Treat the criterion as "the suite does not regress", not as a literal count.

</specifics>

<deferred>
## Deferred Ideas

- `run_events` persistence (DATA-03) — Phase 5, per the roadmap's split at the persistence seam
- WR-01 TOCTOU overshoot between budget check and reservation — deferred to gap closure (`01-DEFERRED.md`), even though it lives in code this phase touches
- Adding mypy to CI — would catch the coroutine-contract bug class generally, but it is a new CI gate and broader than this phase; D-02's targeted test covers the specific risk

</deferred>

---

*Phase: 02-async-safe-data-layer-graceful-shutdown*
*Context gathered: 2026-08-09*

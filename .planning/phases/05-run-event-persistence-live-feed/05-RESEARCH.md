# Phase 5: Run Event Persistence & Live Feed - Research

**Researched:** 2026-08-11
**Domain:** Durable per-step event persistence inside nest-safe SQLite transactions; in-process pub/sub fan-out over SSE; allowlist redaction; scale-to-zero-safe long-lived connections
**Confidence:** HIGH on mechanics (the transaction nesting, the SSE seam, the sensitive-data map, and Fly autostop behaviour were each verified by executing code in this repo or against official docs); MEDIUM on the exact persistence-injection seam (a real design choice the planner must finalise — the *invariant* is locked, the *placement* has two viable shapes)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Two separate paths that never block each other — the **DB is the source of truth** (durable `run_events`, SC-1), the **broker is a lossy live mirror** (in-memory fan-out, SC-2), the **public projection is allowlisted** (SC-3). Durability and liveness are decoupled by construction.
- **D-02:** In-process **`RunEventBroker`** holding a set of per-subscriber `asyncio.Queue`s. When a run persists an event it also publishes the *redacted projection* to live subscribers. Chosen over DB-tailing (single-process, single-machine — no cross-process consumer; immediate latency, not poll-interval).
- **D-03:** On restart, in-flight subscribers reconnect and see new runs; history is **not** replayed to the live feed (durable history lives in `run_events` for Phase 6). No `Last-Event-ID` resume.
- **D-04:** Each event row is persisted **during the stream**, inside the **SAME `transaction()`** as that step's own writes. First real exercise of Phase 2's nest-safe `transaction()`: a `send_reply` opens a transaction and the event write nests inside it — savepoints make that correct. **Do NOT open a second top-level transaction per event.**
- **D-05:** The DB write goes through the existing `asyncio.to_thread` seam (Phase 2), so a slow disk never stalls the paid run's event loop.
- **D-06:** **Publish to the broker only AFTER the DB write commits.** The live feed must never show an event that wasn't durably recorded.
- **D-07:** **Allowlist, not denylist.** Public projection = event type, tool *name* (never inputs), outcome/resolution, cost, retrieval doc *ids* + scores, guardrail *denials* (that a guard fired + which guard, never the denied payload). Everything else excluded by construction.
- **D-08:** A dedicated test asserts known-sensitive strings (seeded customer email, ticket body, fake key) never appear in any projection. This is the SC-3 guard, mutation-checked (adding a raw field to the projection makes it fail).
- **D-09:** `/events` streams send a periodic comment **heartbeat** and **close after an idle ceiling** (~5 min with no runs) so a forgotten tab cannot hold the Fly machine awake. `EventSource` auto-reconnects.
- **D-10:** The broker uses **bounded queues with drop-oldest** on a slow subscriber. A stalled watcher backpressures nothing — the paid run's publish is fire-and-forget.
- **D-11:** `/events` is **public and projection-only** (no key), consistent with the Phase 1 public-surface posture.

### Claude's Discretion

- `run_events` schema columns (at least: run/ticket ref, seq, event type, timestamp, a JSON payload column); indexing for Phase 6 drill-down
- Broker queue size and heartbeat/idle interval exact values (defaults per D-09/D-10)
- Whether the broker lives on `app.state` beside the registry, or is folded into `RunRegistry`
- The minimal `/events`-consuming smoke needed to prove SC-2 (not the Phase 6 UI)

### Deferred Ideas (OUT OF SCOPE)

- The polished dashboard (cards, per-run drill-down, SVG charts, budget gauge, Try-it form) — Phase 6 (DASH-02..05)
- `Last-Event-ID` / SSE resume — Out of Scope (milestone)
- Rejected-action counter, cost-per-stage attribution — v2, ride on `run_events` but not this phase
- Persisting the real-model recovery-probe artifact from Phase 4 — a loose end, not this phase's scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **DATA-03** | A `run_events` table persists per-run step events (tool calls, results, retrieval, denials, usage) written during the stream, enabling per-run drill-down | Pattern 1 (`run_events` schema + `run_uid` correlation), Pattern 2 (the recorder injected into `run_ticket`, write-tool event nested in the tool's transaction — verified atomicity), Pitfall 1 (the cross-thread nesting trap), Code Examples §1-3, Validation rows DATA-03-a..e |
| **DASH-01** | The dashboard receives a live run feed over a public, projection-only SSE `/events` endpoint (no polling; no sensitive data) | Pattern 3 (`RunEventBroker` fan-out, drop-oldest), Pattern 4 (`/events` route, heartbeat + idle-close), Pattern 5 (allowlist projection), the sensitive-data map, Pitfall 2/3/4, Validation rows DASH-01-a..h |

### Success Criteria (from ROADMAP)
1. **SC-1** After a run completes, its full step sequence is queryable from `run_events`.
2. **SC-2** An open dashboard tab shows runs appearing in real time over `/events` with no polling.
3. **SC-3** The public feed contains no ticket bodies, customer data, or API keys — only redacted projections.
4. **SC-4** A slow/abandoned tab never stalls or delays a paid run, and streams are capped so the machine can still scale to zero.
</phase_requirements>

## Summary

Every subtlety in this phase is a **concurrency/ordering** problem, not a feature problem. The three that will bite an unwary planner:

1. **D-04's atomicity requires the event write to run on the *same worker thread*, inside the *same open transaction*, as the tool's own writes.** I verified against the real `db.py` that `Database.transaction()` holds its `RLock` for the entire transaction body and that a nested `transaction()` becomes a `SAVEPOINT` — so nesting only works *re-entrantly on one thread*. `event_stream` in `main.py` runs on the event loop; the tool write runs in a `to_thread` worker and its transaction has **already committed** by the time the `tool_result` event surfaces back on the loop. Persisting from `event_stream` would therefore be *a second top-level transaction* — exactly what D-04 forbids, and it would make the reply row and its event row non-atomic. **The persistence seam must live inside the tool-execution offload (`agent.py`), not in `event_stream`.** This is the single most important finding.

2. **Publish-after-commit (D-06) falls out for free once persistence is in `agent.py`.** `agent.py` persists+commits an event *before* it `yield`s it; `main.py`'s `event_stream` publishes the projection *when it receives* the yielded event — strictly after commit. Persistence in `agent.py`, projection+publish in `main.py`: the same clean split ARCHITECTURE.md already mandates ("publish from `main.py`, not `agent.py`").

3. **A held-open `/events` SSE connection defeats Fly scale-to-zero.** Confirmed against Fly docs: the proxy autostops a machine based on *active inbound connections*, not container activity. An SSE stream is an active connection for as long as it's open, so a forgotten tab pins the machine awake and burns money. The idle-close (D-09) is not a nicety — it is the only thing that lets the machine reach `stopped`. This is separate from the Phase 2 `RunRegistry` drain: `/events` subscribers are **not** agent runs and must **not** enter the registry.

**Primary recommendation:** Add a `run_events` table and a `run_uid TEXT` column to `runs`. Mint a `run_uid` (`uuid4().hex`) at `event_stream` start; pass it plus `app.state.conn` into a per-run **`RunRecorder`** injected into `run_ticket` (optional arg, defaults `None` so `evals.py`/`mcp_server.py` are untouched). The recorder persists each event; for write-tier tool steps it persists the `tool_result` row **inside the tool's transaction** by wrapping the offloaded call in one outer `transaction()` (the tool's own `transaction()` nests as a savepoint — verified atomic). A new **`RunEventBroker`** on `app.state.broker` fans redacted projections out over bounded drop-oldest `asyncio.Queue`s; `event_stream` publishes `project(event)` after each event surfaces (post-commit). A new public `GET /events` subscribes, heartbeats every ~15s, and idle-closes after ~5 min of no run activity. No new dependencies.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `run_events` schema + `run_uid` correlation | Storage (`db.py`) | Telemetry (`record_run` writes `run_uid` on the `runs` row) | Schema ownership belongs to the module that owns the connection and DDL. |
| Persisting each event durably | Agent loop offload (`agent.py` via injected `RunRecorder`) | Storage (`transaction()` primitive) | Only the offloaded worker call is on the same thread + inside the same open transaction as the tool write; that is the *only* place D-04's atomic nesting is achievable. |
| Deciding which fields are public | New module (`events.py` — `project()`) | — | The redaction allowlist is a security boundary; it gets its own tested module, mirroring how the citation allowlist (Phase 3) was isolated. |
| Live fan-out to subscribers | New module (`events.py` — `RunEventBroker`) on `app.state.broker` | HTTP edge (publishes) | In-process pub/sub is a coordinator like `RunRegistry`; per-app-startup instance on `app.state`, not module-level (asyncio-loop-binding hazard). |
| Publishing projections | HTTP edge (`main.py` `event_stream`) | Broker (`publish`) | Keeps `agent.py` ignorant of the dashboard (ARCHITECTURE Pattern 7); publish is post-commit by construction. |
| Serving the public live feed | HTTP edge (`main.py` `GET /events`) | Broker (subscribe/unsubscribe) | A route, not middleware — SSE status locks at 200 on first yield (same reason auth is a route dep). |
| Not holding the machine awake | HTTP edge (`/events` idle-close) | Config (idle ceiling) | Fly autostops on *connection* idle; the app must close its own long-lived streams. |

## Standard Stack

**No new packages.** Every mechanism is Python stdlib or already installed and in use.

### Core
| Module | Version | Purpose | Why standard here |
|--------|---------|---------|-------------------|
| `asyncio.Queue` | stdlib | Per-subscriber broker queue with `maxsize` + `put_nowait`/`get_nowait` for drop-oldest | Bounded, non-blocking publish is exactly the D-10 primitive; no library needed |
| `Database.transaction()` | in-repo (`db.py`, Phase 2) | Nest-safe savepoint transactions | D-04's whole premise; nesting semantics verified below |
| `asyncio.to_thread` | stdlib 3.9+ | Off-loop DB write (D-05) | Already the Phase 2 seam; copies contextvars so OTel spans survive |
| `StreamingResponse` + hand-rolled SSE framing | `fastapi`/`starlette` (installed) | `/events` transport | `event_stream` already frames SSE by hand (`f"event: {type}\ndata: {json}\n\n"`); reuse it, add heartbeat comments |
| `uuid.uuid4().hex` | stdlib | `run_uid` correlation key minted at stream start | `runs.id` doesn't exist until end-of-stream; a uuid joins `run_events`→`runs` without reordering the `record_run` insert |
| `json.dumps(..., default=str)` | stdlib | Serialise raw `event.data` into the `run_events` payload column | Same pattern `JsonFormatter` already uses |

### Alternatives Considered
| Instead of | Could use | Why rejected here |
|------------|-----------|-------------------|
| Hand-rolled SSE for `/events` | `sse-starlette` `EventSourceResponse` | Phase 2 (D-04) already declined it; it's an *undeclared transitive* dep of `mcp`, not in `pyproject.toml`. `event_stream` already frames SSE by hand — reuse that, don't adopt a dependency that owns the response type. |
| In-process broker (D-02) | DB-tailing / polling `run_events` | Single machine, single process — no cross-process consumer to justify polling. Immediate latency vs poll-interval. Locked by D-02. |
| `run_uid` uuid | Insert `runs` row at stream start, update at end | Reorders `record_run` and perturbs the `recorded`-idempotency + reservation reasoning that Phase 1/2 carefully built. A nullable `run_uid` column written once at `record_run` is the minimal, non-disruptive join key. |

**Installation:** none.

**Version note:** confirmed no new imports are required — `asyncio`, `uuid`, `json` are stdlib; `StreamingResponse` and `asyncio.to_thread` are already imported in `main.py`. [VERIFIED: read `src/relay/main.py:1-22`, `src/relay/db.py`]

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** Every mechanism is stdlib or an already-declared/already-imported dependency. No `slopcheck` run was required.

## Runtime State Inventory

This phase is additive (new table, new column, new module, new route), not a rename/refactor — but there is real runtime/stored state to account for.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | New `run_events` table + new `run_uid TEXT` column on `runs`. `SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, so the table is additive and self-applying on the next `init_db`. **The `runs.run_uid` column is the trap:** `CREATE TABLE IF NOT EXISTS` will **not** add a column to the *existing* `runs` table on the live Fly volume — a bare `ALTER TABLE runs ADD COLUMN run_uid TEXT` is required, and it must be idempotent (guard with a `PRAGMA table_info(runs)` check, or catch the "duplicate column" `OperationalError`). | Add table DDL to `SCHEMA`; add a guarded `ALTER TABLE` migration in `init_db`. Verify against the live `/data/relay.db` after deploy. |
| **Live service config** | None. No external service holds run-event state; the broker is in-memory and dies with the process (D-03 accepts this). `fly.toml` `auto_stop_machines='stop'` / `min_machines_running=0` are **read-only inputs** to the idle-close design, not things this phase changes. | None — but the idle-close must respect them (see Pattern 4). |
| **OS-registered state** | None. | None. |
| **Secrets / env vars** | Optional new settings only (`run_events` retention? broker queue size? idle/heartbeat intervals?) — all defaulted in `config.py`, so unset is fine. No secret changes; `/events` is public (D-11). | Add defaulted settings to `config.py`; optional `.env.example` lines. |
| **Build artifacts** | None. No new package, no compiled asset. The Dockerfile `COPY src` already ships new modules. | None. |

**The one migration hazard, restated:** `run_events` is a new table (safe under `IF NOT EXISTS`); `runs.run_uid` is a **column added to an existing table** and needs an explicit idempotent `ALTER TABLE`. On the live volume the `runs` table already has rows and no `run_uid`; those legacy rows will have `run_uid = NULL`, which is correct (they predate event capture) and Phase 6 drill-down must tolerate it.

## Architecture Patterns

### System Architecture Diagram

```
  POST /tickets/{id}/process (gated)                         GET /events (PUBLIC, D-11)
        │                                                          │ subscribe()
        ▼                                                          ▼
  event_stream()  ── run_uid = uuid4().hex ──┐            RunEventBroker (app.state.broker)
        │  recorder = RunRecorder(conn, run_uid)          set[ asyncio.Queue(maxsize=N) ]
        ▼                                     │                    ▲            │
  run_ticket(..., recorder=recorder)         │      publish(project(event))    │ q.get()  (+ heartbeat
        │  per event:                        │       (DROP-OLDEST on full,     │  every ~15s, idle-close
        │                                    │        fire-and-forget) ────────┘  after ~5min no runs)
        │  ┌─ tool_use / text / usage / ─────┼─────────────► yield SSE to caller
        │  │  resolution / guardrail / notice│                    │
        │  │     recorder.record(event)      │                    └─► project(event) ─► broker.publish
        │  │     → to_thread: own txn (1 INSERT into run_events)
        │  │
        │  └─ WRITE-TOOL step (send_reply / create_escalation / set_category):
        │        to_thread( recorder.execute_and_record(execute_bound, ...) ):
        │           with conn.transaction():            # OUTER, top-level
        │               result = execute_bound(...)     # tool's own transaction() → SAVEPOINT (nested)
        │               conn.execute("INSERT INTO run_events ...")   # SAME outer txn
        │           # commit here → reply row + event row ATOMIC (D-04, verified)
        │        ── returns to loop ── yield tool_result ── project ── broker.publish   (POST-commit, D-06)
        ▼
  finally: record_run(conn, ..., run_uid)  →  runs table (run_uid joins run_events for Phase 6)
```

Data flow to trace: a `send_reply` step's reply row and its `run_events` row commit together in one worker-thread transaction; only *after* that commit does the projection reach a subscriber's queue; a slow subscriber's full queue drops its oldest frame rather than blocking the paid run.

### Recommended Project Structure
```
src/relay/
├── db.py         # CHANGED  + run_events DDL in SCHEMA; guarded ALTER TABLE runs ADD run_uid in init_db
├── events.py     # NEW      RunEventBroker (fan-out) + project() (allowlist redaction)
├── agent.py      # CHANGED  run_ticket accepts optional recorder; write-tool offload wraps tool+event in one txn
├── main.py       # CHANGED  event_stream mints run_uid, builds recorder, publishes projections;
│                 #          new public GET /events; lifespan builds + closes the broker
├── telemetry.py  # CHANGED  record_run gains run_uid param, writes it on the runs row
├── config.py     # CHANGED  + broker queue size, heartbeat interval, idle ceiling (all defaulted)
├── mcp_server.py # UNTOUCHED  (calls run_ticket/_execute_guarded without a recorder → no persistence)
└── evals.py      # UNTOUCHED  (same — recorder defaults None)
tests/
├── test_run_events.py  # NEW  persistence, atomic nesting, redaction leak test, broker fan-out/drop-oldest, /events smoke
└── conftest.py         # ADD  a broker fixture + a helper to capture published projections
```

**Structure rationale:** `events.py` is a new flat module (matches the one-concern-per-module convention; ARCHITECTURE.md already names `events.RunBroadcaster`). Keep the broker **separate from `RunRegistry`**, not folded in (CONTEXT allows either): the registry's contract is "runs actually streaming, drained at shutdown, empty when idle" (D-06 of Phase 2); broker subscribers are *dashboard viewers* with the opposite lifecycle (they persist across many runs and must be idle-closed, not drained). Folding them together would entangle two teardown semantics and risk a `/events` viewer being counted as an in-flight run — which would stall the Phase 2 drain and break scale-to-zero.

### Pattern 1: `run_events` schema + `run_uid` correlation

**What:** a table that stores the **raw** event, full-fidelity, for Phase 6 drill-down. Redaction happens only at the *public* boundary (`project()`), never in the DB — the DB is private and is the source of truth (D-01).

```sql
CREATE TABLE IF NOT EXISTS run_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uid    TEXT NOT NULL,          -- uuid4().hex minted at stream start; joins to runs.run_uid
    ticket_id  INTEGER NOT NULL,
    seq        INTEGER NOT NULL,        -- monotonic per run, so drill-down renders in order
    type       TEXT NOT NULL,           -- text | tool_use | tool_result | usage | guardrail | notice | error | resolution
    payload    TEXT NOT NULL,           -- json.dumps(event.data, default=str) — RAW, not projected
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_run_events_run_uid ON run_events(run_uid);
```

- Add `run_uid TEXT` to `runs` (nullable — legacy rows predate capture). **Existing table → needs `ALTER TABLE`, not `IF NOT EXISTS` (see Runtime State Inventory).**
- `seq` is a per-run counter held by the `RunRecorder`, incremented per persisted event. `(run_uid, seq)` gives a stable render order for Phase 6 without trusting `created_at` tie-breaks.
- **Store raw `event.data`.** DASH-03 (Phase 6) drill-down needs tool inputs/outputs and retrieval text; the public `/events` feed gets the redacted `project()` instead. Persist raw, project at publish — one table serves both.

**Why not FK `run_events.run_uid → runs.run_uid`:** the `runs` row is inserted at *end* of stream (`record_run` in the `finally`), long after the first `run_events` row is written mid-stream. A real FK would fail. The uuid is a soft join key; that's sufficient and avoids reordering the carefully-built end-of-stream insert.

### Pattern 2: `RunRecorder` injected into `run_ticket` — the only correct seam for D-04

**What:** a per-run object holding `(conn, run_uid, ticket_id)` and a `seq` counter, passed into `run_ticket` as an optional argument (default `None`). `agent.py` calls it as it yields events.

**Why it must be here and not in `event_stream` (the load-bearing finding):** `Database.transaction()` holds its `RLock` for the *entire* transaction body and a nested `transaction()` opens a `SAVEPOINT` — verified below. Re-entrancy is per-thread. The tool write runs in a `to_thread` worker and its transaction commits *inside* that worker call; by the time the `tool_result` event reaches `event_stream` on the loop, the tool's transaction is closed. To write the event row **in the same transaction** (D-04), it must run **on the same worker thread while the transaction is still open** — i.e. inside the offloaded tool call.

**Verified nesting semantics** [VERIFIED: executed against `src/relay/db.py`, 2026-08-11]:

```
case1  outer txn { INSERT reply; nested txn { INSERT event } }        → reply=1  event=1   (both durable)
case2  outer txn { INSERT reply; nested txn { INSERT event }; RAISE } → reply-  event-     (BOTH rolled back → atomic)
case3  outer txn { INSERT reply; nested txn { INSERT event; RAISE } } → reply=1  event=0   (inner savepoint discarded, outer commits)
```

Case 2 is the D-04 guarantee: the reply and its event row commit or roll back **together**. Case 3 shows a failed event write can't corrupt the reply. This only holds because the event INSERT shares the tool's transaction on one thread.

**The offloaded unit of work for a write-tier tool:**
```python
# events.py / recorder — runs inside asyncio.to_thread (one worker thread)
def execute_and_record(self, execute_bound, spec, name, raw_input, policy, *, event_type):
    with self.conn.transaction():                    # OUTER top-level transaction
        result, is_error = execute_bound(spec, name, raw_input, policy)   # tool's own transaction() → SAVEPOINT
        self._insert_event(event_type, {"tool": name, "result": json.loads(result), "is_error": is_error})
    return result, is_error                           # commit at `with` exit → reply + event atomic
```
`agent.py`'s write-tool branch calls `to_thread(recorder.execute_and_record, execute_bound, spec, name, input, policy, event_type="tool_result")` instead of `to_thread(execute_bound, ...)`. For **read** tools and non-tool events, `recorder.record(event)` opens its own single-INSERT top-level transaction (there is no sibling write to nest into — D-04's "no second top-level transaction" is specifically about the *write step*, where nesting is mandatory).

**Why this keeps `evals.py`/`mcp_server.py` working:** they call `run_ticket(...)` / `_execute_guarded(...)` with no recorder; `recorder is None` → the offload stays exactly today's `to_thread(execute_bound, ...)` and nothing is persisted. This is the same optional-collaborator pattern `run_ticket` already uses for `policy`/`budget`. [VERIFIED: read `src/relay/agent.py:179-197`, `src/relay/evals.py` call site referenced in CLAUDE.md]

**Confidence note (MEDIUM on placement):** the *invariant* — write-tool event shares the tool's transaction; publish strictly after commit — is LOCKED and mechanically verified. The exact number of `agent.py` call sites the recorder touches (one wrapped offload + N plain `record` calls at the yield points) is an implementation detail the planner finalises. An acceptable alternative is a single `record()` call placed right after each existing `yield AgentEvent(...)`, with only the write-tool branch using `execute_and_record`. Either shape satisfies D-04/D-06; the planner should pick the one with the fewest agent.py edits and add the atomicity test (Validation DATA-03-b) as the gate.

### Pattern 3: `RunEventBroker` — bounded, drop-oldest, fire-and-forget

```python
# events.py
class RunEventBroker:
    def __init__(self, *, maxsize: int = 256):
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize
        self.closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)                      # idempotent, like RunRegistry.deregister

    def publish(self, frame: dict) -> None:
        # Synchronous, non-blocking, fire-and-forget (D-10). A stalled subscriber
        # drops its OLDEST frame rather than backpressuring the paid run's publish.
        for q in self._subs:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()                 # drop oldest
                    q.put_nowait(frame)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass                           # never raise into the run
```

- **`publish` is a plain `def`, not `async`** — it never awaits, so it cannot suspend or backpressure the run. This is the mechanical proof of "a stalled watcher backpressures nothing" (D-10).
- **On shutdown/drain:** `close()` sets `self.closed = True` and pushes a sentinel (or empty-set + wake) to every subscriber so their `/events` generators terminate. `lifespan` must call `broker.close()` so open `/events` connections end and don't keep the machine awake or delay uvicorn's connection drain. **`/events` subscribers are NOT registered in `RunRegistry`** — the Phase 2 drain waits on *agent runs*, and a viewer is not one.

### Pattern 4: `GET /events` — public, heartbeat, idle-close

```python
@app.get("/events")                                # NO gate — public (D-11)
async def events() -> StreamingResponse:
    async def stream():
        q = app.state.broker.subscribe()
        idle_deadline = time.monotonic() + settings.events_idle_seconds
        try:
            # Optional first frame: a projection of RunRegistry.snapshot() so a new tab
            # immediately sees what's running now (D-03: no HISTORY replay; live snapshot is fine).
            while True:
                timeout = settings.events_heartbeat_seconds
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=timeout)
                except TimeoutError:
                    if time.monotonic() >= idle_deadline:
                        return                      # idle-close → EventSource reconnects (D-09)
                    yield ": keep-alive\n\n"        # SSE comment heartbeat; not an event
                    continue
                if frame is _CLOSE_SENTINEL:        # broker.close() on shutdown
                    return
                idle_deadline = time.monotonic() + settings.events_idle_seconds
                yield f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n"
        finally:
            app.state.broker.unsubscribe(q)         # ALWAYS runs — the CR-01/CR-02 lesson
    return StreamingResponse(stream(), media_type="text/event-stream")
```

- **Idle ceiling measures *run* activity, not heartbeats** — the deadline resets only on a real frame, so a feed with only heartbeats still closes after ~5 min. This is the SC-4 machine-side guarantee: **Fly autostops on active *connections*, not container activity** [CITED: fly.io/docs/reference/fly-proxy-autostop-autostart/], so a held-open SSE stream pins the machine awake until the app closes it.
- **Heartbeat interval ~15s** keeps proxies/`EventSource` from treating the stream as dead during a quiet-but-recent period; **idle ceiling ~5 min** per D-09. Both configurable, defaulted in `config.py`.
- **`unsubscribe` in `finally`** — same discipline as `event_stream`'s deregister: a subscriber that disconnects mid-stream (or whose generator never starts) must not leak a queue into the broker set, or the machine's publish loop grows unbounded and, worse, the broker never empties.
- **Interaction with uvicorn graceful shutdown:** an open `/events` connection is an in-flight connection uvicorn waits on (up to `--timeout-graceful-shutdown 20`). `broker.close()` in `lifespan` ends these promptly so shutdown doesn't burn the full window. This is why the broker close belongs in lifespan **alongside** the registry drain, not inside it.

### Pattern 5: `project()` — the allowlist redaction (SC-3 security boundary)

**What:** a pure function `project(event: AgentEvent) -> dict | None` that builds the public frame field-by-field from a **named allowlist**, never by spreading `event.data`. Return `None` to drop an event from the feed entirely.

```python
# events.py — build every field explicitly; NEVER do {**event.data}
def project(event) -> dict | None:
    t, d = event.type, event.data
    if t == "usage":
        return {"type": t, "steps": d.get("steps"), "input_tokens": d.get("input_tokens"),
                "output_tokens": d.get("output_tokens"), "cost_usd": d.get("cost_usd")}
    if t == "resolution":
        return {"type": t, "via": d.get("via"), "cost_usd": d.get("cost_usd"), "steps": d.get("steps")}
    if t == "error":
        return {"type": t, "reason": d.get("reason")}          # reason is an enum, safe
    if t == "tool_use":
        return {"type": t, "tool": d.get("tool")}              # NAME only — never d["input"]
    if t == "tool_result":
        return _project_tool_result(d)                          # per-tool safe fields, below
    if t == "guardrail":
        return {"type": t, "guard": d.get("guard"), "tool": d.get("tool"), "action": d.get("action")}
    if t == "notice":
        return {"type": t, "kind": d.get("kind"), "tool": d.get("tool"),
                "retrieval_mode": d.get("retrieval_mode"), "cause": d.get("cause"), "results": d.get("results")}
    if t == "text":
        return {"type": t}                                      # DROP the prose — may restate customer data
    return None
```

Per-tool `tool_result` allowlist (this is where the leak risk concentrates):

| Tool | Raw result contains | Public projection |
|------|---------------------|-------------------|
| `lookup_customer` | customer name, email, plan, recent ticket subjects | `{type, tool, is_error}` — **drop the customer object entirely** |
| `search_docs` | doc text, headings, ids, scores | `{type, tool, results:[{doc, id, score}]}` — ids + scores only (D-07: "retrieval doc ids + scores"), **never `text`** |
| `send_reply` | `{reply_id, status}` | `{type, tool, reply_id, status}` — no body (body isn't in the result, but assert it) |
| `create_escalation` | `{escalation_id, status}` | `{type, tool, escalation_id, status}` |
| `set_category` | `{ticket_id, category}` | `{type, tool, category}` |

**Mutation-check discipline (D-08):** the leak test seeds a customer email + ticket body + fake key into a run, captures every `/events` frame, and asserts none of the three strings appears anywhere. It must **fail** when a raw field is added to `project()` (e.g. spreading `**d` into a `tool_use` frame). Without the mutation half this is an "unfalsifiable check" — the exact trap this codebase keeps catching (Phase 3 citation allowlist).

### Sensitive-Data Map (which yielded events carry what)

This is the audit the planner needs to know *why* each allowlist entry exists. [VERIFIED: read `src/relay/agent.py` yield sites + `src/relay/tools.py` executors]

| Event `type` | `data` field | Sensitive? | Notes |
|--------------|--------------|-----------|-------|
| `tool_use` | `input` | **YES** | `lookup_customer`→email, `search_docs`→query, `send_reply`→body+citations, `create_escalation`→reason. Project to `tool` name only. |
| `tool_result` (`lookup_customer`) | `result.customer` | **YES** | name, email, plan, recent ticket subjects. Drop entirely. |
| `tool_result` (`search_docs`) | `result.results[].text` | KB is public docs but still **exclude** | Project ids + scores; text is not in the allowlist. |
| `tool_result` (write tools) | `result` | No | `{reply_id/escalation_id, status, category}` — ids and enums. |
| `text` | `text` | **YES-ish** | Model prose; may restate customer specifics. Drop the body. |
| `usage`/`resolution` | tokens, cost, steps, via | No | Allowlisted (cost). |
| `error` | `reason`, `status`, `type` | No | Enumerated reasons. |
| `guardrail` | `guard`, `tool`, `action`, `expected/supplied_ticket_id`, `missing_citations` | ids only, but **be strict** | D-07: "that a guard fired + which guard." Ticket ids are ints and doc ids are public filenames, but the minimal projection (`guard, tool, action`) fails closed. |
| `notice` | `kind`, `retrieval_mode`, `cause`, `results` | No | Degradation info. |

### Anti-Patterns to Avoid
- **Persisting the event from `event_stream` in `main.py`.** By the time the event surfaces, the tool's transaction has committed on another thread — a `run_events` write there is *a second top-level transaction*, violating D-04 and making reply+event non-atomic. Persist in the `agent.py` offload.
- **`async def publish(...)`** anywhere in the broker. An awaiting publish can suspend the paid run behind a slow subscriber — the exact backpressure D-10 forbids. Keep it synchronous and non-blocking.
- **`{**event.data}` in `project()`.** A denylist by another name; leaks the first field someone adds later. Build every field explicitly.
- **Redacting inside `run_events`.** The DB is private and full-fidelity for Phase 6; redaction is a *publish-time* transform only.
- **Counting `/events` viewers in `RunRegistry`.** They aren't agent runs; doing so stalls the Phase 2 drain and pins the machine awake — breaking scale-to-zero.
- **Publishing before the commit returns.** Ordering must be persist→commit→publish (D-06); with persistence in `agent.py` this is automatic because the event is yielded after the offloaded commit.
- **`CREATE TABLE IF NOT EXISTS runs (... run_uid ...)` to add the column.** No-op on the existing table; the column silently never appears on the live volume. Use a guarded `ALTER TABLE`.

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Atomic reply+event write | Manual `BEGIN`/`SAVEPOINT`/`RELEASE` strings | `Database.transaction()` nesting (Phase 2) | Already correct and verified; a raw `BEGIN` on a connection already in a transaction raises "cannot start a transaction within a transaction". |
| Bounded fan-out with drop | A ring buffer + condition variables | `asyncio.Queue(maxsize=N)` + `put_nowait`/`get_nowait` | Drop-oldest is two nowait calls; no locking, no library. |
| SSE transport + shutdown | `sse-starlette` | Hand-rolled framing (already in `event_stream`) + `broker.close()` in lifespan | Phase 2 declined the dep; the framing is three lines and already exists. |
| Off-loop DB write | A bespoke thread pool | `asyncio.to_thread` | The Phase 2 seam; carries contextvars for OTel. |
| Client reconnect after idle-close | Custom retry JS | `EventSource` native auto-reconnect | Built into the browser; D-09 relies on it. |

**Key insight:** every primitive this phase needs already exists one layer down (savepoints in `db.py`, `asyncio.Queue`, `to_thread`, `EventSource`). The only new code is the *ordering glue*: the recorder that puts the event write inside the tool's transaction, and the broker that fans projections out without blocking.

## Common Pitfalls

### Pitfall 1: The cross-thread nesting trap (the reason D-04 needs the `agent.py` seam)
**What goes wrong:** the event write is placed in `event_stream` (loop thread) "inside a transaction," expecting it to nest with the tool's write.
**Why it happens:** `transaction()` *looks* re-entrant, so it seems you can open one anywhere. But it holds the `RLock` for the whole body and re-entrancy is per-thread; the tool ran (and committed) on a different `to_thread` worker. The two transactions never overlap in time or thread.
**How to avoid:** persist the write-tool event *inside the offloaded worker call*, wrapping tool-exec + event-insert in one `transaction()` (Pattern 2). Verified atomic (case 2 above).
**Warning signs:** an atomicity test that forces the event insert to raise but finds the reply row still present; `run_events` rows with `created_at` strictly *after* the `runs`/reply commit.

### Pitfall 2: A held-open `/events` stream keeps the Fly machine awake
**What goes wrong:** scale-to-zero stops working; the machine bills 24/7 because one forgotten tab holds an SSE connection.
**Why it happens:** Fly's proxy autostops on *active inbound connections*, not on container CPU/idle. An SSE stream is active until closed.
**How to avoid:** idle-close after ~5 min of no run activity (D-09), measured on real frames not heartbeats. `EventSource` reconnects when the user returns.
**Warning signs:** `fly status` never shows `stopped`; the machine's uptime tracks the oldest open dashboard tab.

### Pitfall 3: A leaked broker subscriber
**What goes wrong:** a `/events` client disconnects but its queue stays in the broker's set; `publish` iterates an ever-growing set of dead queues, and the broker never empties.
**Why it happens:** the same async-generator asymmetry Phase 2 documented — a `finally` that isn't guaranteed to run, or unsubscribe placed outside the generator body.
**How to avoid:** `subscribe()` as the first statement of the `/events` generator body; `unsubscribe()` in its `finally` (Pattern 4). Test that a never-started / disconnected stream leaves the broker with zero subscribers.
**Warning signs:** `len(broker._subs)` grows across a test session; publish latency rises with uptime.

### Pitfall 4: The unfalsifiable redaction test (SC-3's classic failure)
**What goes wrong:** the leak test passes because the run under test happened not to place the secret where the (buggy) projection copies raw data.
**Why it happens:** the test asserts absence without proving the projection *would* catch a leak.
**How to avoid:** mutation-check (D-08) — add a raw field to `project()` and assert the test flips to red. Seed the secret into a field the projection actually touches (a `tool_use.input` and a `lookup_customer` result).
**Warning signs:** the leak test is green even after you deliberately spread `**event.data` into a frame.

## Code Examples

### 1. `record_run` gains `run_uid` (telemetry.py)
```python
def record_run(conn, *, ticket_id, model, duration_ms, steps, input_tokens,
               output_tokens, cost_usd, outcome, run_uid=None):   # run_uid optional → old callers safe
    with conn.transaction():
        conn.execute(
            "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
            " output_tokens, cost_usd, outcome, run_uid) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticket_id, model, duration_ms, steps, input_tokens, output_tokens, cost_usd, outcome, run_uid),
        )
```

### 2. Guarded column migration (db.py `init_db`)
```python
def init_db(conn) -> None:
    conn.executescript(SCHEMA)                       # run_events created here (IF NOT EXISTS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_uid" not in cols:                        # ALTER is not idempotent; guard it
        conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")
    ...  # existing seed logic
    conn.commit()
```

### 3. `event_stream` wiring (main.py, inside the existing generator)
```python
run_uid = uuid.uuid4().hex
recorder = RunRecorder(app.state.conn, run_uid=run_uid, ticket_id=ticket.id)
...
async for event in run_ticket(app.state.client, app.state.registry, ticket.model_dump(),
                              policy=ToolPolicy(allow_writes=not dry_run), recorder=recorder):
    ...existing usage/outcome bookkeeping...
    frame = project(event)                           # allowlist redaction
    if frame is not None:
        app.state.broker.publish(frame)              # POST-commit: event already persisted in agent.py
    yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
...
record_run(app.state.conn, ..., run_uid=run_uid)     # in the existing finally
```

## State of the Art
| Old approach | Current approach | Impact |
|--------------|------------------|--------|
| No per-step persistence; only the end-of-run `runs` summary survives | `run_events` written during the stream | Enables Phase 6 drill-down (DASH-03); this is FEATURES.md's "hidden critical path". |
| 5-second `/metrics` polling for the dashboard | Push over `/events` SSE | Live feed replaces the poll (don't stack both — two sources of truth drift). |
| Raw events forwarded verbatim (the naive live-feed) | Allowlist projection at the public boundary | The difference between a demo and a data leak on an unauthenticated endpoint. |

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | Storing **raw** `event.data` in `run_events` (redaction only at the public boundary) is what Phase 6 drill-down wants. D-01 says "DB is the source of truth" and DASH-03 needs tool inputs/outputs, which implies full fidelity. | Pattern 1 | If Phase 6 expects `run_events` to be pre-redacted, drill-down loses inputs/outputs. Low risk — DASH-03 explicitly needs them; confirm at Phase 6 planning. |
| A2 | A nullable `runs.run_uid` soft join key is preferred over inserting the `runs` row at stream start. | Standard Stack / Pattern 1 | If a hard FK is later required, a follow-up migration is needed. Low — reordering `record_run` was judged riskier (perturbs Phase 1/2 reservation logic). |
| A3 | Default intervals: broker queue `maxsize≈256`, heartbeat `≈15s`, idle ceiling `≈5min`. | Pattern 3/4 | Wrong values are a tuning issue, not a correctness one; all configurable. CONTEXT leaves exact values to discretion. |
| A4 | Touching `agent.py` (optional `recorder` arg) is acceptable in Phase 5. Phase 2's "don't touch agent.py/evals/mcp" (D-03) was Phase-2-scoped; the recorder defaults `None` so evals/mcp behaviour is unchanged. | Pattern 2 | If a reviewer treats Phase 2 D-03 as global, the seam must move — but then D-04 atomicity is unachievable (Pitfall 1). Surface at plan-check. |

## Open Questions (RESOLVED)

> All three closed on 2026-08-11: Q1 and Q2 by CONTEXT.md's D-04-correction / D-14 addendum and enforced in the plans; Q3 deferred to v2.


1. **Exact `agent.py` recorder call-site count.**
   - Known: the write-tool offload must wrap tool-exec + event-insert in one transaction; publish must be post-commit.
   - Unclear: whether non-write events are persisted from within `run_ticket` (one `record` per yield) or the planner prefers a thinner touch.
   - **RESOLVED (D-04 correction):** persist all events via the recorder in `agent.py` (write-tool one inside its txn); publish projections from `main.py`. Gated by the atomicity test (05-02 Task 3).

2. **Does `/events` send an initial snapshot of currently-running runs?**
   - D-03 forbids *history* replay but a live snapshot of `RunRegistry.snapshot()` (projected) helps a fresh tab. **RESOLVED (D-14, now MANDATORY):** send one projected snapshot frame on connect; live state, not history. Enforced by `test_events_sends_initial_snapshot_on_connect` (05-04 Task 2).

3. **`run_events` retention.** Unbounded growth on the Fly volume over months. **RESOLVED (deferred to v2):** not in scope this phase (corpus is tiny, runs are ~$0.02 and rate-limited); v2 filler is a `DELETE WHERE created_at < ...` on startup.

## Environment Availability

Skip — no external dependencies. This phase is code + schema only (SQLite already present; no new package, service, or CLI). All mechanisms are stdlib or already installed. [VERIFIED: read `pyproject.toml` deps via CLAUDE.md; no new imports required]

## Validation Architecture

Nyquist enabled (`workflow.nyquist_validation` absent-or-true in `.planning/config.json` → enabled). [VERIFIED: read config.json]

### Test Framework
| Property | Value |
|----------|-------|
| Framework | `pytest>=8` + `pytest-asyncio` (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/test_run_events.py -x -q` |
| Full suite command | `pytest -q` |
| CI path | `.github/workflows/ci.yml` `test` job: `pip install -e ".[dev]"` → `ruff check src tests` → `pytest -q`. Free (no API keys); `conftest.py`'s `_no_outbound_http` autouse fixture blocks accidental Voyage/Anthropic calls. [VERIFIED: read ci.yml, conftest.py] |

### Phase Requirements → Test Map
| Req / SC | Behavior | Type | Automated command | File exists? |
|----------|----------|------|-------------------|-------------|
| DATA-03 / SC-1 (a) | A completed run persists one `run_events` row per yielded event, in `seq` order, joinable to `runs` via `run_uid` | integration | `pytest tests/test_run_events.py::test_a_run_persists_its_full_event_sequence -x` | ❌ Wave 0 |
| DATA-03 / SC-1 (b) | **Atomicity:** a `send_reply` run commits reply row + its `run_events` row together; forcing the event insert to raise rolls back the reply too (D-04, the load-bearing test) | integration | `pytest tests/test_run_events.py::test_send_reply_and_its_event_row_commit_atomically -x` | ❌ Wave 0 |
| DATA-03 (c) | Guarded `ALTER TABLE runs ADD run_uid` is idempotent (second `init_db` on a populated DB doesn't raise; legacy rows keep `run_uid=NULL`) | unit | `pytest tests/test_run_events.py::test_run_uid_migration_is_idempotent -x` | ❌ Wave 0 |
| DASH-01 / SC-2 (d) | **`/events` smoke:** subscribe to `/events`, run a ticket, assert projected frames arrive live (proves SC-2 without the Phase 6 UI) | integration | `pytest tests/test_run_events.py::test_events_delivers_a_live_run -x` | ❌ Wave 0 |
| DASH-01 / SC-3 (e) | **Redaction leak test:** seed customer email + ticket body + fake key; capture every `/events` frame; assert none appear. **Mutation-checked** (spreading a raw field flips it red) | integration | `pytest tests/test_run_events.py::test_no_projection_leaks_sensitive_data -x` | ❌ Wave 0 |
| DASH-01 / SC-4 (f) | **Fire-and-forget:** a full (stalled) subscriber queue drops its oldest frame; `publish` never blocks or raises; the paid run completes normally | unit | `pytest tests/test_run_events.py::test_publish_drops_oldest_and_never_blocks -x` | ❌ Wave 0 |
| DASH-01 (g) | **No leaked subscriber:** a `/events` stream that disconnects (or never starts) leaves the broker with zero subscribers | integration | `pytest tests/test_run_events.py::test_events_disconnect_unsubscribes -x` | ❌ Wave 0 |
| D-06 (h) | Publish happens strictly after commit — a frame reaches a subscriber only after its `run_events` row is durable | integration | `pytest tests/test_run_events.py::test_broker_never_leads_the_database -x` | ❌ Wave 0 |
| D-09 / SC-4 (i) | `/events` emits a heartbeat comment during a quiet period and idle-closes after the ceiling (use a short ceiling in test) | integration | `pytest tests/test_run_events.py::test_events_heartbeats_then_idle_closes -x` | ❌ Wave 0 |
| SC-4 (j) | Scale-to-zero preserved: an open `/events` subscriber does **not** register an agent run (`RunRegistry.active` unaffected); `lifespan` `broker.close()` ends open streams | integration | `pytest tests/test_run_events.py::test_events_viewer_is_not_a_registered_run -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_run_events.py -x -q`
- **Per wave merge:** `pytest -q` (the full suite — the existing 122+ tests must not regress; `test_lifecycle.py`'s drain/registry tests are the guard that the broker didn't reintroduce a CR-01/CR-02 leak)
- **Phase gate:** full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_run_events.py` — new file covering DATA-03 + DASH-01 + SC-1..4 (rows a–j above)
- [ ] `tests/conftest.py` — add a `broker` fixture and a helper that drives `event_stream`/`/events` and captures published frames (reuse `helpers.FakeClient`/`TicketAwareFakeClient` for the agent side)
- [ ] Atomicity harness — a way to force the `run_events` insert to raise mid-transaction (monkeypatch `RunRecorder._insert_event`) to prove case-2 rollback; mirrors `test_lifecycle.py`'s `record_run_against_a_closed_database` pattern
- [ ] Mutation-check note in the leak test docstring (the "do not weaken this" convention `test_overlapping_runs_all_record...` already models)
- [ ] No framework install needed — pytest/pytest-asyncio already present

## Security Domain

`security_enforcement` not disabled in config → included.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard control |
|---------------|---------|------------------|
| V5 Input Validation | yes (indirect) | `/events` takes no input (public GET, no params); the persisted payloads are already Pydantic-validated tool I/O upstream. |
| V6 Cryptography | no | No secrets handled by this phase; `/events` is keyless by design (D-11). |
| V7 Error/Logging & Data Protection | **yes — primary** | The allowlist projection (D-07) is the data-protection control: sensitive fields never cross the public boundary. Structured logs already pass ids/counts only (Phase 1 convention). |
| V4 Access Control | yes (by design) | `/events` is intentionally public and content-free (D-11); access control is replaced by *content* control (projection). The threat model: an unauthenticated reader learns run cadence/cost/outcomes, never customer data. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard mitigation |
|---------|--------|---------------------|
| Sensitive data exposure on an unauthenticated SSE feed | Information Disclosure | Allowlist `project()`, mutation-checked leak test (D-07/D-08) |
| Slow-reader backpressure stalls a paid run (indirect DoS on cost/latency) | Denial of Service | Bounded drop-oldest queues, synchronous fire-and-forget publish (D-10) |
| Forgotten tab defeats scale-to-zero (cost abuse) | Denial of Service (cost) | Idle-close + heartbeat (D-09); Fly autostops on connection idle |
| Live feed shows an event never durably recorded (integrity) | Tampering/Repudiation | Publish strictly after commit (D-06); DB is source of truth (D-01) |

## Sources

### Primary (HIGH)
- Codebase, read directly: `src/relay/{main,agent,db,tools,telemetry,models,runs,config}.py`, `tests/{conftest,helpers,test_lifecycle}.py`, `Dockerfile`, `fly.toml`, `.github/workflows/ci.yml`, `.planning/config.json` — the integration seams, the SSE framing, the transaction primitive, the test conventions. **HIGH**
- Executed against this repo's `src/relay/db.py` (2026-08-11): the three-case nesting proof (atomic commit, atomic rollback, inner-savepoint discard). **HIGH**
- `.planning/phases/02-async-safe-data-layer-graceful-shutdown/02-RESEARCH.md` — the `to_thread` seam, the `RunRegistry` + drain, uvicorn cancellation semantics (the CR-01/CR-02 class the broker must not reintroduce). **HIGH**
- `.planning/research/{ARCHITECTURE,FEATURES,STACK}.md` — projection-only public `/events`, run_events as the hidden critical path, why the live feed threatens scale-to-zero, the publish-from-main boundary. **HIGH**
- [Fly Proxy autostop/autostart](https://fly.io/docs/reference/fly-proxy-autostop-autostart/) and [Autostop/autostart Machines](https://fly.io/docs/launch/autostop-autostart/) — autostop keys on active inbound connections, checked every few minutes; a held-open connection prevents stop. **HIGH (CITED)**

### Secondary (MEDIUM)
- [Fly — Long-running tasks and machine lifecycle](https://fly.io/docs/blueprints/long-running-tasks/) — the proxy can't see inside the container; connection close is what triggers stop. **MEDIUM-HIGH**
- [Fly community — idle time before auto-stop](https://community.fly.io/t/how-to-change-the-idle-time-before-machine-is-auto-stopped/15423) — corroborates the "few minutes" idle window. **MEDIUM**

## Metadata

**Confidence breakdown:**
- `run_events` schema + persistence mechanics: HIGH — verified nesting/atomicity against the real `db.py`; schema is a small additive change.
- The persistence *seam* (recorder in `agent.py`): MEDIUM — the invariant is locked and proven; the exact call-site placement is a planner decision (two viable shapes, both satisfy D-04/D-06).
- Broker + `/events` + redaction: HIGH on mechanics (stdlib primitives, allowlist pattern, Fly autostop behaviour all verified/cited); MEDIUM only on default interval values (tuning, not correctness).
- Migration hazard (`ALTER TABLE runs`): HIGH — `IF NOT EXISTS` does not add columns; guarded `ALTER` is standard.

**Research date:** 2026-08-11
**Valid until:** ~2026-09-10 (stable — stdlib + in-repo primitives; only the Fly autostop docs could drift, and slowly)

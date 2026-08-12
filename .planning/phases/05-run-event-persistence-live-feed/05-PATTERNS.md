# Phase 5: Run Event Persistence & Live Feed - Pattern Map

**Mapped:** 2026-08-11
**Files analyzed:** 9 (4 new, 5 modified, 0 asserted-untouched)
**Analogs found:** 9 / 9 (every new/modified file has an in-repo analog except the `ALTER TABLE` migration, flagged below)

This project has a *very* high bar for pattern fidelity: near-identical concerns already exist one layer down (savepoint nesting, an on-`app.state` coordinator with a drain, an allowlist accept-set, a public projection-only route, the `to_thread` offload seam). The new code is almost entirely *ordering glue* over existing primitives. Copy the analogs closely — the comments in them encode the exact hazards this phase re-enters (CR-01 leaked-finally, CR-02 leaked-subscriber, the cross-thread nesting trap, the unfalsifiable-redaction trap).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/relay/events.py` (NEW) — `RunEventBroker` | provider / coordinator | pub-sub | `src/relay/runs.py` `RunRegistry` | exact (per-app-startup on `app.state`, idempotent add/remove, close-on-drain) |
| `src/relay/events.py` (NEW) — `project()` | utility (security boundary) | transform | `agent.py` citation allowlist (`_execute_guarded` L150-172); Phase 3 accept-set | role-match (allowlist, fails-closed) |
| `src/relay/events.py` (NEW) — `RunRecorder` | service | file-I/O (DB write) | `telemetry.py` `record_run` L57-78 + `db.py` `transaction()` | role-match (per-run writer through `transaction()`) |
| `src/relay/db.py` (MOD) — `run_events` DDL | model / migration | CRUD | existing `SCHEMA` string L19-68 (`runs`, `replies`, index) | exact |
| `src/relay/db.py` (MOD) — guarded `ALTER TABLE runs` | migration | batch (DDL) | **NO ANALOG** — `init_db` L220-228 only ever ran `CREATE TABLE IF NOT EXISTS` | none — flagged below |
| `src/relay/agent.py` (MOD) — optional `recorder` param + worker seam | controller | event-driven | `bind_to_ticket` L57-91 (optional constructor injection); `to_thread` offload L305-317 | exact |
| `src/relay/main.py` (MOD) — `event_stream` persist/publish hooks | controller | streaming (SSE) | `event_stream` L193-279 (the generator itself; hooks attach here) | exact (same file, additive) |
| `src/relay/main.py` (MOD) — public `GET /events` route | route | streaming (SSE) | `/metrics` L282-286 (public), `process_ticket` StreamingResponse L159-279 | role-match (public + SSE, no gate) |
| `src/relay/main.py` (MOD) — broker create/close in `lifespan` | config | event-driven | `lifespan` L27-45 (registry create + drain-before-close) | exact |
| `src/relay/telemetry.py` (MOD) — `record_run` gains `run_uid` | service | CRUD | `record_run` L57-78 | exact (same function) |
| `src/relay/models.py` (MOD, if needed) — projection type | model | — | `AgentEvent` L38-46 | exact |
| `tests/test_run_events.py` (NEW) | test | — | `tests/test_lifecycle.py` (drain/registry/atomicity harness) | exact |
| `tests/conftest.py` (MOD) — broker fixture + capture helper | test | — | `conftest.py` fixtures L24-61; `helpers.py` `TicketAwareFakeClient` | exact |

**Asserted untouched (a test must prove it):** `src/relay/mcp_server.py`, `src/relay/evals.py` — both call `run_ticket`/`_execute_guarded` with no `recorder`, so `recorder is None` keeps today's `to_thread(execute_bound, ...)` path. This mirrors how `policy`/`budget` default in `run_ticket` (agent.py L183-184, L197-198). `.github/workflows/ci.yml` also untouched (no new deps).

---

## Pattern Assignments

### `src/relay/events.py` → `RunEventBroker` (provider, pub-sub)

**Analog:** `src/relay/runs.py` `RunRegistry` (the load-bearing model — read its module docstring L1-19 first).

**Why this analog:** `RunRegistry` is the project's template for a per-app-startup, in-memory coordinator held on `app.state`, created in `lifespan`, with idempotent removal and a shutdown hook. The broker is its sibling with the *opposite* teardown lifecycle (viewers persist across many runs and are idle-closed, not drained). RESEARCH is explicit: keep it separate, do NOT fold into `RunRegistry`.

**On-`app.state`, not module-level** (runs.py L8-12, L53-56):
```python
# The registry is an instance created per app startup and stored on app.state,
# never module-level state like ratelimit.py's reservations.
```
The broker holds `asyncio.Queue`s. An `asyncio.Queue` binds to the loop it is first used on, exactly like the `asyncio.Event` hazard runs.py documents at L60-64 and L121-124 — so the broker (like the registry) must be built in `lifespan`, never at import. `tests/test_lifecycle.py` L1-6 and L110-134 show why: the suite runs more than one event loop.

**Idempotent removal** (runs.py L98-102) — copy for `unsubscribe`:
```python
def deregister(self, token: int) -> None:
    """Retire one run. Idempotent, and never retires another run's."""
    self._active.pop(token, None)
```
Broker `unsubscribe(q)` → `self._subs.discard(q)` (discard, not remove — idempotent by construction, same discipline).

**`snapshot()` for the D-14 initial frame** (runs.py L108-110) — already written *for this phase*:
```python
def snapshot(self) -> list[ActiveRun]:
    """The live records, for phase 5's "what is running right now" projection."""
    return list(self._active.values())
```
The `/events` initial-snapshot frame (D-14) projects `app.state.runs.snapshot()`.

**Drop-oldest publish must be a plain `def`, non-blocking, fire-and-forget** (RESEARCH Pattern 3; D-10). There is no exact analog for the `put_nowait`/`get_nowait` drop-oldest — it is new. The invariant to preserve: `publish` never `await`s, never raises into the run. RESEARCH Code Example Pattern 3 gives the reference body.

**close-on-drain lives in `lifespan`, beside the drain, not inside it** — see the `lifespan` assignment below.

---

### `src/relay/events.py` → `project()` (utility, transform — SC-3 security boundary)

**Analog:** the citation allowlist in `agent.py` `_execute_guarded` L146-172 (Phase 3, RAG-04) — an accept-set that fails closed.

**Why this analog:** CONTEXT L78 and RESEARCH Pattern 5 both state the redaction "mirrors how the citation guard was built (allowlist accept-set)." The citation guard builds an explicit `allowed` set and denies anything not in it, rather than stripping known-bad — the same allowlist-not-denylist posture `project()` needs.

**The accept-set shape to mirror** (agent.py L150-157):
```python
if name == "send_reply" and retrieved_ids is not None:
    allowed = {i.strip().lower() for i in retrieved_ids}
    missing = [
        c for c in (validated.get("citations") or []) if c.strip().lower() not in allowed
    ]
    if missing:
        ...  # denied — anything not explicitly allowed is rejected
```

**`project()` build-every-field-explicitly rule** (RESEARCH Pattern 5, Anti-Patterns): build the public dict field-by-field from a named allowlist; **NEVER** `{**event.data}`. Return `None` to drop an event. The per-`type` and per-tool allowlist table is in RESEARCH Pattern 5 + the Sensitive-Data Map — that map is the authoritative field list, derived from the actual yield sites.

**The event shapes `project()` consumes** come straight from `agent.py`'s yield sites — copy the field names exactly:
- `tool_use` L301-303: `{"tool": block.name, "input": block.input}` → project to `tool` name only, DROP `input`
- `tool_result` L450-457: `{"tool", "result": payload, "is_error"}` → per-tool allowlist on `result`
- `guardrail` L392-401 / L432-441: `{"guard", "tool", "action", ...}` → `{guard, tool, action}` only
- `notice` L414-425: `{"kind", "tool", "retrieval_mode", "cause", "results"}` → safe as-is (counts/enums)
- `usage` L290 / `resolution` L478-481 / `error` L277,283-286: cost/steps/reason — allowlisted
- `text` L299: `{"text": block.text}` → DROP the prose

**Per-tool `tool_result` leak concentration** (from `tools.py` executor returns):
- `lookup_customer` (tools.py L34-45) returns `{"found", "customer": dict(row), "recent_tickets": [...]}` — **drop `customer` and `recent_tickets` entirely** (name, email, plan, subjects)
- `search_docs` (tools.py L48-70 / retrieval) returns `results[].{doc, heading, id, text, score}` — project `{doc, id, score}` only, **never `text`**
- `send_reply` L98 → `{reply_id, status}`; `create_escalation` L82 → `{escalation_id, status}`; `set_category` L104 → `{ticket_id, category}` — ids/enums, safe

**Mutation-check discipline** (the load-bearing test): the citation guard's own regression test convention (`test_overlapping_runs_all_record...` L369-376, "Do not weaken these to an exception check") is the model. The leak test must FAIL when a raw field is spread into a frame — see the test assignment below.

---

### `src/relay/events.py` → `RunRecorder` (service, file-I/O)

**Analog:** `telemetry.py` `record_run` L57-78 (a per-run writer that goes through `conn.transaction()`), plus the nesting contract in `db.py` `transaction()` L156-204.

**Why this analog:** `record_run` is the existing "write one run's row through `transaction()`" function; `RunRecorder._insert_event` is its per-step sibling. Its comment L69-72 explains why a bare `commit()` is wrong (connection-scoped) — the same reason the recorder must use `transaction()`, never raw `execute` + `commit`.

**`record_run`'s single-INSERT-in-a-transaction shape** (telemetry.py L73-78) — copy for the read-tool / non-tool `record()` path:
```python
with conn.transaction():
    conn.execute(
        "INSERT INTO runs (ticket_id, model, ...) VALUES (?, ?, ...)",
        (ticket_id, model, ...),
    )
```

**The write-tool atomic-nesting seam** — `db.py` `transaction()` L156-204 is the primitive, and its docstring L170-173 explicitly names *this phase* as its reason to exist:
```
# Phase 5 writes run_events from inside a run that already holds a
# transaction; that call site is why this is not left as a documented trap.
```
The three-case proof (RESEARCH Pattern 2, verified against this `db.py`): the write-tool `execute_and_record` opens ONE outer `transaction()`, calls the tool's `execute` (whose own `transaction()` — e.g. `send_reply` tools.py L92-98 — nests as a SAVEPOINT), then inserts the event row inside the same outer block. Reply row + event row commit or roll back together (case 2). RESEARCH Pattern 2 gives the exact `execute_and_record` body.

---

### `src/relay/db.py` → `run_events` DDL + guarded migration (model / migration)

**Analog (table DDL):** the existing `SCHEMA` string, db.py L19-68 — specifically the `runs` table L45-56 and the `idx_runs_created_at` index L67.

**Copy the DDL conventions exactly** (L45-67): `INTEGER PRIMARY KEY AUTOINCREMENT`, `TEXT NOT NULL`, `created_at TEXT NOT NULL DEFAULT (datetime('now'))`, and a `CREATE INDEX IF NOT EXISTS` after the table. The `run_events` schema + `idx_run_events_run_uid` index are in RESEARCH Pattern 1. The comment style — an inline note on *why* an index exists (L65-66) — should be matched for the `run_uid` index (Phase 6 drill-down join).

**Analog (migration): NONE — flag this.** `init_db` L220-228 has only ever run `executescript(SCHEMA)` (all `IF NOT EXISTS`) + a seed count. There is **no existing guarded `ALTER TABLE`** anywhere in the repo. This is the phase's one genuinely new DB idiom and its documented trap (RESEARCH Runtime State Inventory + Anti-Patterns + D-13): `CREATE TABLE IF NOT EXISTS runs (... run_uid ...)` is a silent no-op on the *existing* `runs` table on the live Fly volume — the column never appears. The guarded pattern (RESEARCH Code Example 2):
```python
def init_db(conn) -> None:
    conn.executescript(SCHEMA)                       # run_events created here
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_uid" not in cols:                        # ALTER is not idempotent; guard it
        conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")
    ...  # existing seed logic L222-227
    conn.commit()
```
Note `init_db` receives a `Database`, not a raw `sqlite3.Connection` — use `conn.execute(...).fetchall()` (Result API, db.py L95-108), not `conn.cursor()`. Idempotency is asserted by `test_run_uid_migration_is_idempotent` (a second `init_db` on a populated DB must not raise).

---

### `src/relay/agent.py` → optional `recorder` param + worker seam (controller, event-driven)

**Analog:** `bind_to_ticket` L57-91 (optional constructor-injection of a per-run collaborator) and the `to_thread` offload L305-317.

**Optional-collaborator injection** — `run_ticket`'s existing `policy`/`budget` defaults L183-184, L197-198 are the exact pattern the `recorder` reuses:
```python
policy: ToolPolicy | None = None,
budget: RunBudget | None = None,
...
policy = policy or ToolPolicy()
```
Add `recorder=None` the same way. `recorder is None` → today's behaviour unchanged (this is what keeps `evals.py`/`mcp_server.py` untouched, RESEARCH Pattern 2 + Assumption A4).

**The write-tool offload seam** — L305-317, where the event-insert must nest:
```python
result, is_error = await asyncio.to_thread(
    execute_bound, spec, block.name, block.input, policy
)
```
For a write-tier tool with a recorder, this becomes `await asyncio.to_thread(recorder.execute_and_record, execute_bound, spec, block.name, block.input, policy, event_type="tool_result")`. The offload comment L308-314 already documents the "the write is inside a transaction and either commits or rolls back, never lands halfway" contract — the recorder extends that transaction to cover the event row.

**Loop-suspension hazard** (agent.py L232-233, and mirrored in main.py L237-239): this is a generator; nothing loop-bound may be held across a `yield`. The recorder holds only a `seq` counter + conn/run_uid (plain state, like `retrieved_ids` L216-217 held by reference) — no context managers spanning a yield.

**Constructor-injection-over-forgettable-keyword rationale** (bind_to_ticket L59-65): the same philosophy applies — but note RESEARCH's MEDIUM-confidence flag: the exact number of `record()` call sites vs. one `execute_and_record` is a planner decision. Both shapes satisfy D-04/D-06; the atomicity test is the gate.

---

### `src/relay/main.py` → `event_stream` persist/publish hooks (controller, streaming)

**Analog:** `event_stream` itself, L193-279 — the hooks attach to the existing generator (same file, additive).

**Where `run_uid` + recorder are minted** (before the `run_ticket` call at L222-227), and where projection+publish attach (inside the `async for`, L228-234). RESEARCH Code Example 3 gives the exact wiring:
```python
run_uid = uuid.uuid4().hex
recorder = RunRecorder(app.state.conn, run_uid=run_uid, ticket_id=ticket.id)
async for event in run_ticket(..., policy=..., recorder=recorder):
    ...existing usage/outcome bookkeeping (L228-233)...
    frame = project(event)                           # allowlist redaction
    if frame is not None:
        app.state.broker.publish(frame)              # POST-commit (persisted in agent.py already)
    yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"   # unchanged L234
```

**Publish-after-commit falls out for free** (RESEARCH Summary point 2): agent.py persists+commits *before* it yields; `event_stream` publishes *when it receives* the yield — strictly post-commit by construction. Keep `agent.py` ignorant of the broker.

**`record_run` in the finally gains `run_uid`** — the existing call L260-270 adds `run_uid=run_uid`. The finally's leak-safe structure L236-277 (record wrapped in try/except, `release_run` + `deregister` in an inner finally) is CR-01 — **do not disturb it**; the `run_uid` is a pure addition to the existing `record_run(...)` kwargs.

---

### `src/relay/main.py` → public `GET /events` route (route, streaming)

**Analog:** `/metrics` L282-286 (a public, ungated route) for the *public* posture; `process_ticket` L159-279 for the `StreamingResponse(generator, media_type="text/event-stream")` SSE shape.

**Public-by-design, no gate** — `/metrics` L282-286 and `/dashboard` L328-341 are public; the gate comment L116-117 lists what stays public. `/events` joins them (D-11). The `_gate` docstring L74-76 explains why: an SSE StreamingResponse locks status at 200 on first yield, so a route dependency (not middleware) is the only place a rejection could land — and `/events` carries only allowlisted projections, so it needs none.

**SSE framing to reuse** — the exact hand-rolled frame from `event_stream` L234:
```python
yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
```
`/events` reuses this plus SSE comment heartbeats (`": keep-alive\n\n"`) and an idle-close. RESEARCH Pattern 4 gives the full generator (heartbeat via `asyncio.wait_for(q.get(), timeout=...)`, idle-deadline resets only on real frames, close-sentinel).

**`subscribe()` first in the body, `unsubscribe()` in `finally`** — this is the CR-02 lesson, and `event_stream` L193-217 is the canonical example: registration happens *inside* the generator body (L203-204) precisely because Starlette can cancel a StreamingResponse before its generator starts, and a `finally` in a body that never ran does not execute (L196-202 comment). `test_a_stream_that_never_starts_registers_nothing` (test_lifecycle.py L444-456) is the exact regression to mirror for `test_events_disconnect_unsubscribes`.

**`/events` viewers must NOT enter `RunRegistry`** (D-12, RESEARCH Anti-Patterns): the registry's drain waits on `active == 0` (runs.py L112-159); a counted viewer would stall it and pin the machine awake. The broker's subscriber set is entirely separate.

---

### `src/relay/main.py` → broker create/close in `lifespan` (config)

**Analog:** `lifespan` L27-45 — registry created L36, drained-before-close L44-45.

**Create beside the registry** (L33-36):
```python
app.state.conn = conn
app.state.registry = build_registry(conn, settings.kb_dir)
app.state.client = AsyncAnthropic(...)
app.state.runs = RunRegistry()
# + app.state.broker = RunEventBroker()
```

**Close on shutdown, beside the drain, not inside it** (L37-45). The drain L44 waits for agent runs; `broker.close()` ends viewer streams. RESEARCH Pattern 4 (interaction-with-uvicorn note): `broker.close()` belongs in `lifespan` alongside the drain so open `/events` connections end promptly and don't burn uvicorn's graceful window. Order: drain runs (waits on real runs), then `broker.close()`, then `conn.close()` — closing viewers must not race the connection close the same way the drain guards it (L38-43 comment explains the close-last discipline).

---

### `src/relay/telemetry.py` → `record_run` gains `run_uid` (service, CRUD)

**Analog:** `record_run` L57-78 itself.

**Additive keyword-only param, defaulted** — matches the codebase's keyword-only + optional-default convention (CLAUDE.md Function Design). RESEARCH Code Example 1:
```python
def record_run(conn, *, ticket_id, ..., outcome, run_uid=None):   # optional → old callers safe
    with conn.transaction():
        conn.execute(
            "INSERT INTO runs (..., run_uid) VALUES (?,?,...,?)",
            (..., run_uid),
        )
```
`run_uid=None` default keeps `test_lifecycle.py`'s direct `record_run` callers (L203-213, L200-213) and any other caller working unchanged. The INSERT column list must gain `run_uid` in step with the DDL.

---

### `src/relay/models.py` → projection type (model, if needed)

**Analog:** `AgentEvent` L38-46 (the plain `type` + `data: dict` envelope).

Likely no new model is required — `project()` returns a plain `dict` (the SSE wire format is a JSON dict, same as `AgentEvent.data`). If a typed projection is wanted, mirror `AgentEvent`'s minimal shape. Prefer not adding one unless a plan needs it; the codebase keeps models lean (CLAUDE.md: no grab-bag models).

---

### `tests/test_run_events.py` (NEW test)

**Analog:** `tests/test_lifecycle.py` in full — it is the template for every test type this phase needs.

**Atomicity harness (DATA-03-b, the load-bearing D-04 test)** — mirror `test_a_failed_record_run_still_releases...` L496-538, which monkeypatches `record_run` to `raise sqlite3.ProgrammingError("Cannot operate on a closed database")` (L513-516) and asserts consequences. For atomicity: monkeypatch `RunRecorder._insert_event` to raise mid-transaction and assert the `send_reply` row rolled back too (case-2 rollback). `test_lifecycle.py`'s "re-open from disk to prove durability" idiom L227-234 is the model for verifying commit/rollback.

**Live `/events` smoke (DASH-01/SC-2)** — drive a run and capture frames the way `test_overlapping_runs_all_record...` L380-399 drives `process_ticket` and reads `stream.body_iterator` (`"".join([chunk async for chunk in stream.body_iterator])`). The `/events` side subscribes a broker queue in parallel.

**Redaction leak test (SC-3, the load-bearing one)** — the mutation-check convention is `test_overlapping_runs...` L369-376 and L417-419 ("Do not weaken these to an exception check", asserted per-row so one corrupt read fails it). Seed a customer email + ticket body + fake key; capture every `/events` frame; assert none of the three strings appears anywhere; document that spreading a raw field into `project()` must flip it red (RESEARCH Pitfall 4 / D-08). Seed the secret into a field `project()` actually touches (a `tool_use.input` and a `lookup_customer` result).

**Leaked-subscriber test (Pitfall 3 / CR-02)** — mirror `test_a_stream_that_never_starts_registers_nothing` L444-456: a stream that never starts / disconnects leaves `len(broker._subs) == 0`.

**Drop-oldest / fire-and-forget (SC-4)** — unit test on the broker directly: fill a subscriber queue to `maxsize`, publish, assert oldest dropped, `publish` never blocked or raised, the paid run completed. No HTTP needed (like the pure-`RunRegistry` drain tests L44-107).

**Viewer-is-not-a-run (SC-4/D-12)** — assert an open `/events` subscriber leaves `RunRegistry.active` unaffected; mirror `test_registry_is_empty_after_a_run_completes` L422-441.

**Heartbeat + idle-close (D-09)** — use a short idle ceiling in the test; assert a heartbeat comment during quiet, then close. Config values come from `settings` (see Shared Patterns).

**Registry constructed per-test, not off `app.state`** — test_lifecycle.py header comment L1-6 and its bodies build fresh `RunRegistry()` because `asyncio.Event`/`Queue` bind per-loop. Build a fresh `RunEventBroker()` per test the same way.

---

### `tests/conftest.py` (MOD test) + `tests/helpers.py`

**Analog:** existing fixtures `conn`/`db`/`registry`/`client` L24-61; test doubles in `helpers.py`.

**Add a `broker` fixture** shaped like the `conn`/`registry` fixtures L24-45 (construct, `yield`, teardown). **Add a capture helper** that drives `event_stream`/`/events` and records published frames — reuse `helpers.FakeClient` L26-34 (scripted responses) and `TicketAwareFakeClient` L37-52 (concurrent runs) for the agent side; no new Claude wiring needed.

**Keep the autouse guards** — `_no_outbound_http` L63-79 and `_reset_limits` L15-22 already protect the suite; the broker is in-memory so it needs no reset hook (same reasoning as `RunRegistry`, runs.py L53-56: per-app state, nothing for the autouse fixture to clear).

---

## Shared Patterns

### On-`app.state` per-startup coordinator (never module-level)
**Source:** `runs.py` L8-12, L53-64; wired in `main.py` `lifespan` L36.
**Apply to:** `RunEventBroker`.
An `asyncio` primitive (Queue/Event) binds to the loop it is first used on. Build the broker in `lifespan`, store on `app.state.broker`, never at import. The test suite runs multiple loops (test_lifecycle.py L110-134) and will expose a module-level instance immediately.

### Leak-safe generator teardown (CR-01 record-finally, CR-02 subscribe-in-body)
**Source:** `main.py` `event_stream` L193-217 (subscribe-inside-body) and L236-277 (wrapped-record finally); `runs.py` L67-96 register/deregister contract.
**Apply to:** `/events` (`subscribe` first statement, `unsubscribe` in `finally`) and the `event_stream` publish hooks (must not perturb the existing record/release/deregister finally).
The two mutations that stayed green across the whole suite in Phase 2 were "delete the drain" and "move it after close" (test_lifecycle.py L137-142) — the broker close must be covered the same way (a test that fails if `broker.close()` is removed from `lifespan`).

### Nest-safe `transaction()` — the D-04 atomicity primitive
**Source:** `db.py` `transaction()` L156-204 (its docstring L170-173 names this phase); `telemetry.py` `record_run` L73-78 as the single-INSERT usage.
**Apply to:** `RunRecorder` — write-tool event nests inside the tool's own `transaction()` as a SAVEPOINT (one outer block in the worker thread); read-tool/non-tool events open their own single-INSERT `transaction()`.
Never raw `BEGIN`/`SAVEPOINT` strings (RESEARCH Don't-Hand-Roll); never a bare `commit()` (record_run L69-72 explains why it's connection-scoped and wrong).

### Allowlist that fails closed (security boundary)
**Source:** `agent.py` citation guard L146-172 (Phase 3 accept-set).
**Apply to:** `project()` — build each public field explicitly from a named allowlist; never `{**event.data}`. A denylist leaks the first field someone forgets (D-07).

### `to_thread` offload for all DB writes (no loop stall)
**Source:** `agent.py` L305-317 (tool exec); `main.py` L102, L151, L286, L348-352 (every DB touch offloaded); its rationale in `_gate` L91-99 and test_lifecycle.py `test_the_daily_budget_read_runs_off_the_event_loop` L682-723.
**Apply to:** `RunRecorder` writes run inside the existing `to_thread` seam (D-05); no new thread machinery.

### Public route = route dependency, never middleware; SSE locks 200 on first yield
**Source:** `main.py` `_gate` docstring L70-76; `/metrics` L282-286, `/dashboard` L328-341 public surfaces.
**Apply to:** `GET /events` — public, no gate, projection-only (D-11).

### Additive, defaulted, keyword-only parameters keep frozen callers working
**Source:** `run_ticket` `policy`/`budget` L183-184; `bind_to_ticket` retrieved_ids L57; `record_run` keyword-only `*` L59.
**Apply to:** `recorder=None` on `run_ticket`; `run_uid=None` on `record_run`. This is what keeps `mcp_server.py`/`evals.py` untouched (assert via a test, mirroring test_lifecycle.py L240-261 which covers the MCP registry precisely because D-03 freezes that file).

### New settings default-and-optional in `config.py`
**Source:** `config.py` — every phase adds a commented block (e.g. `shutdown_drain_seconds` L67-71, retrieval settings L73-106).
**Apply to:** broker `maxsize` (~256), heartbeat (~15s), idle ceiling (~5min) — all defaulted so unset is fine (RESEARCH A3). Match the inline-comment-explaining-why convention.

---

## No Analog Found

| File / concern | Role | Data Flow | Reason |
|----------------|------|-----------|--------|
| `db.py` guarded `ALTER TABLE runs ADD COLUMN run_uid` in `init_db` | migration | DDL | `init_db` L220-228 has only ever run `CREATE TABLE IF NOT EXISTS` + a seed; there is no prior `ALTER TABLE`, no `PRAGMA table_info` guard, no idempotent-migration idiom anywhere in the repo. This is the phase's one genuinely new DB pattern and its documented trap (D-13, RESEARCH Runtime State Inventory). Use RESEARCH Code Example 2; gate with `test_run_uid_migration_is_idempotent`. |
| `RunEventBroker.publish` drop-oldest (`put_nowait`/`get_nowait`) | provider | pub-sub | No existing bounded-queue-with-drop primitive. `RunRegistry` models the *lifecycle* (on-`app.state`, idempotent, close-on-drain) but not the drop-oldest fan-out. Use RESEARCH Pattern 3 verbatim; the invariant (plain `def`, never awaits, never raises) is the load-bearing bit. |
| `/events` heartbeat + idle-close loop | route | streaming | `event_stream` models the SSE framing and finally-discipline but has no heartbeat/idle-timeout; the `asyncio.wait_for(q.get(), timeout=...)` idle-deadline logic is new. Use RESEARCH Pattern 4. |

## Metadata

**Analog search scope:** `src/relay/` (all modules read: runs, db, agent, main, models, telemetry, tools, config), `tests/` (conftest, helpers, test_lifecycle).
**Files scanned:** 12 read in full.
**Pattern extraction date:** 2026-08-11

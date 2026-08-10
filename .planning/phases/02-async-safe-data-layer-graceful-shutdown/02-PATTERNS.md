# Phase 2: Async-Safe Data Layer & Graceful Shutdown - Pattern Map

**Mapped:** 2026-08-09
**Files analyzed:** 14 (3 new, 11 modified)
**Analogs found:** 14 / 14 (2 partial — see "No Analog Found")

This codebase is small and internally consistent, so most "analogs" are the file
being modified itself: the pattern to copy is the one already in that file, and
the risk is drifting from it. The two genuinely new artefacts (`src/relay/runs.py`,
the `Database` wrapper in `db.py`) have strong analogs in `ratelimit.py` and
`guardrails.py` respectively.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/relay/runs.py` **(NEW)** | service (in-memory lifecycle registry) | event-driven | `src/relay/ratelimit.py` (`_reservations` / `reserve_run` / `release_run`) | role-match (strong) |
| `src/relay/db.py` | model / storage | CRUD | itself (`connect`, `init_db`) + `src/relay/guardrails.py` (`ToolPolicy` class shape) | exact (self) |
| `src/relay/main.py` | controller | request-response + streaming | itself (`lifespan`, `event_stream`, `_get_ticket`) | exact (self) |
| `src/relay/agent.py` | service | streaming / event-driven | itself (`_execute_guarded` call site, `run_ticket` span block) | exact (self) |
| `src/relay/tools.py` | service | CRUD | itself (`send_reply`, `create_escalation`, `set_category`) | exact (self) |
| `src/relay/telemetry.py` | service | CRUD | itself (`record_run`) | exact (self) |
| `src/relay/config.py` | config | — | itself (phase-grouped settings blocks) | exact (self) |
| `fly.toml` | config (platform) | — | itself (`[env]` block comment style) | exact (self) |
| `Dockerfile` | config (build) | — | itself (`CMD` line 23) | exact (self) |
| `.env.example` | config (docs) | — | itself (phase-grouped comment blocks) | exact (self) |
| `tests/test_db.py` **(NEW)** | test (unit) | CRUD | `tests/test_ratelimit.py` (unit half, lines 43-330) + `tests/test_tools.py` | role-match |
| `tests/test_lifecycle.py` **(NEW)** | test (unit + integration) | streaming / event-driven | `tests/test_observability.py:58-82` + `tests/test_ratelimit.py:423-433` | role-match |
| `tests/conftest.py` | test fixture | — | itself (`conn` fixture, lines 23-28) | exact (self) |
| `tests/helpers.py` | test double | — | itself (`FakeClient`, lines 25-33) | exact (self) |

---

## Pattern Assignments

### `src/relay/runs.py` (NEW — service, event-driven)

**Analog:** `src/relay/ratelimit.py` — the existing token-identified in-flight tracker.
Secondary: `src/relay/auth.py:1-8` for the "docstring justifies the design choice" convention.

**Module docstring pattern — states scope AND what it deliberately is not** (`src/relay/ratelimit.py:1-13`):
```python
"""Phase 1 security perimeter: burst limiting and the daily spend ceiling.

Two controls with deliberately different lifetimes. The moving window bounds
burst — it lives in process memory and is expected to vanish on a cold start,
because a scale-to-zero machine that forgets who was hammering it an hour ago is
fine. The daily ceiling bounds aggregate Claude spend and is derived from the
`runs` table, so it survives those same cold starts, which is the whole point:
in-memory buckets cannot enforce a dollar budget across restarts.

Both are exposed as plain callables for FastAPI route dependencies, never
middleware — see auth.py for why streaming responses make middleware unusable
for rejections.
"""
```
`runs.py`'s docstring must do the same job: say why it is not part of `ratelimit.py`
(that module is scoped to burst limiting and the spend ceiling; run-lifecycle
tracking is neither) and why the registry is per-app-startup, not module-level.

**Token-identified state + idempotent release** (`src/relay/ratelimit.py:37-49`, `176-193`):
```python
# Cost of runs admitted but not yet written to `runs`: token -> (expires_at, usd).
#
# Self-expiring because the release is not guaranteed to happen. Starlette can
# cancel a streaming response before its generator is ever started, and a `finally`
# in an async generator that never began does not run — so that run's claim is
# never handed back. ...
RESERVATION_TTL_S = 300.0
_reservations: dict[int, tuple[float, float]] = {}
_tokens = itertools.count()


def reserve_run() -> int:
    """Claim this run's worst-case cost before it starts, returning its token."""
    now = time.monotonic()
    _prune(now)
    token = next(_tokens)
    _reservations[token] = (now + RESERVATION_TTL_S, settings.max_run_cost_usd)
    return token


def release_run(token: int | None) -> None:
    """Hand back one claim. Idempotent, and never frees another run's."""
    if token is not None:
        _reservations.pop(token, None)
```
Copy: `itertools.count()` for tokens, `time.monotonic()` for timestamps,
`dict.pop(token, None)` for idempotent removal, one-line docstring stating the
invariant ("Idempotent, and never frees another run's").

**Deliberate divergences from this analog — call them out in comments:**
1. `RunRegistry` is a **class instantiated in `lifespan` and stored on `app.state`**,
   not module-level globals. Reason: an `asyncio.Event` binds to the loop it is
   first awaited on; a module-level instance outlives a `TestClient`'s loop.
   `ratelimit.py`'s module-level state is safe only because `MemoryStorage`
   "constructs safely outside a running event loop" (see its comment at line 32-33).
2. **No TTL.** The reservation needs one because `reserve_run()` is called in the
   handler and released in the generator. The registry registers *inside* the
   generator body, so register/deregister are exactly balanced. State that reason
   in a comment or a future reader will "fix" it by adding a TTL.
3. Because state is per-app, `runs.py` needs **no `reset_limits()` equivalent**
   (`ratelimit.py:225-229`) and must not be added to `conftest.py`'s autouse reset.

**Structured logging pattern** (`src/relay/ratelimit.py:114-117`, `src/relay/agent.py:207-209`):
```python
logger = logging.getLogger("relay.ratelimit")
...
logger.info(
    "ratelimit.exceeded",
    extra={"ctx": {"bucket": bucket, "tier": tier, "ip": ip, "retry_after": retry_after}},
)
```
New logger name: `logging.getLogger("relay.runs")`. New events:
`shutdown.drain_started`, `shutdown.drain_complete`, `shutdown.drain_timeout`.
Dotted, past/imperative, context only in `ctx`. **Security constraint from
RESEARCH.md's V7 row:** `ctx` passes straight through `JsonFormatter`
(`src/relay/telemetry.py:31`) — carry `ticket_id` and counts only, never bodies
or emails.

**Frozen dataclass for a lightweight struct** (`src/relay/tools.py:26-31`):
```python
@dataclass(frozen=True)
class ToolSpec:
    schema: dict[str, Any]
    tier: str  # "read" | "write"
    input_model: type[BaseModel]
    execute: Callable[..., str]
```
`ActiveRun` follows this exactly: `@dataclass(frozen=True)`, plain annotated fields,
no validation (Pydantic is reserved for wire/tool inputs per CONVENTIONS.md).

---

### `src/relay/db.py` (model / storage, CRUD)

**Analog:** itself, lines 61-76. Class shape from `src/relay/guardrails.py` (`ToolPolicy`).

**Current code being replaced** (`src/relay/db.py:61-76`):
```python
def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
            SEED_CUSTOMERS,
        )
    conn.commit()
```
Note the existing `PRAGMA foreign_keys = ON` line — the WAL and `busy_timeout`
pragmas go beside it, same style, one pragma per `execute` call. `init_db`'s
signature keeps its parameter name `conn` (every call site passes positionally;
renaming it to `db` is churn with no benefit and would ripple into `mcp_server.py`
and `evals.py`, which D-03 protects). Its body is the one place `executescript` and
`executemany` are used — the wrapper must expose both.

**Target shape** — verified in RESEARCH.md Pattern 1. Non-negotiable details:
- `Database.execute()` returns a **materialised `Result`**, never a live
  `sqlite3.Cursor` (Pitfall 1: the naive version failed 4 of 5 concurrency runs
  with `customer_email=None` / `status=''`, not with `database is locked`).
- `threading.RLock`, not `Lock` — `transaction()` calls `execute()` re-entrantly.
- `Result` must cover exactly the contract in use: `.fetchone()`, `.fetchall()`,
  `.lastrowid`, iteration. Nothing else appears in `src/` or `tests/`.
- No `sqlite3.Connection.autocommit` (3.12+; the declared floor is 3.11).

**Module docstring** currently reads (`src/relay/db.py:1`):
```python
"""SQLite storage. Phase 1 uses the stdlib driver; Postgres comes with deployment (phase 6)."""
```
Extend it in the CONVENTIONS.md "docstring explains *why*" style — it now owns
connection ownership and transaction boundaries, which is the whole point of the
change and must be stated where the class lives.

**Schema addition (D-10)** goes at the end of `SCHEMA` (`src/relay/db.py:6-51`),
which is a single `"""` DDL string of `CREATE TABLE IF NOT EXISTS` statements
separated by blank lines:
```sql
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);
```
`init_db` already runs `executescript(SCHEMA)` on every startup, so it self-applies.

---

### `src/relay/agent.py` (service, streaming)

**Analog:** itself — one line changes; everything around it stays.

**The exact call site to modify** (`src/relay/agent.py:188-206`):
```python
                    spec = registry.get(block.name)
                    with tracer.start_as_current_span(
                        f"tool.{block.name}", context=run_ctx
                    ) as span:
                        # Bound at call time from this run's own ticket — never stored on
                        # the registry, which is built once and shared by every live run.
                        result, is_error = _execute_guarded(
                            spec, block.name, block.input, policy,
                            bound_ticket_id=ticket["id"],
                        )
                        payload = json.loads(result)
```
Becomes `result, is_error = await asyncio.to_thread(_execute_guarded, spec, block.name,
block.input, policy, bound_ticket_id=ticket["id"])`. The `with tracer.start_as_current_span(...)`
block stays — `to_thread` copies the current `contextvars.Context`, so span parenting
survives (RESEARCH.md Pattern 2, verified). This block contains **no `yield`**, which is
what makes an `await` here safe under the module's suspend-at-yield rule.

**Constraint to preserve** (`src/relay/agent.py:116-126`):
```python
    # The run span is parented explicitly (not made "current") because this is
    # a generator: execution suspends at every yield, and a current-span
    # context manager would leak across whatever runs in between.
    run_span = tracer.start_span(
        "agent.run",
        attributes={...},
    )
    run_ctx = trace.set_span_in_context(run_span)
```
`grep -c 'async with' src/relay/agent.py` must stay `0`. `to_thread` is an `await`,
not an `async with` — no new context manager is introduced.

**`_execute_guarded` stays sync** (`src/relay/agent.py:45-52`, `84-87`) — unchanged:
```python
    try:
        return spec.execute(**validated), False
    except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
        return json.dumps({"error": str(exc)}), True
```
This is the sanctioned broad-except and the reason `mcp_server.call_mcp_tool` can
keep calling `_execute_guarded` directly (D-03).

**Import placement:** `import asyncio` joins the stdlib group at `src/relay/agent.py:23-27`
(`json`, `logging`, `time`, then `collections.abc`, `typing`) — ruff's isort ordering.

---

### `src/relay/tools.py` (service, CRUD)

**Analog:** itself — the three multi-statement writers.

**Current pattern, repeated three times** (`src/relay/tools.py:67-90`):
```python
def create_escalation(conn: sqlite3.Connection, ticket_id: int, reason: str, priority: str) -> str:
    cur = conn.execute(
        "INSERT INTO escalations (ticket_id, reason, priority) VALUES (?, ?, ?)",
        (ticket_id, reason, priority),
    )
    conn.execute("UPDATE tickets SET status = 'escalated' WHERE id = ?", (ticket_id,))
    conn.commit()
    return json.dumps({"escalation_id": cur.lastrowid, "status": "escalated"})


def send_reply(conn: sqlite3.Connection, ticket_id: int, body: str) -> str:
    # Email delivery is mocked: the reply is persisted, nothing leaves the system.
    cur = conn.execute(
        "INSERT INTO replies (ticket_id, body) VALUES (?, ?)", (ticket_id, body)
    )
    conn.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
    conn.commit()
    return json.dumps({"reply_id": cur.lastrowid, "status": "resolved"})


def set_category(conn: sqlite3.Connection, ticket_id: int, category: str) -> str:
    conn.execute("UPDATE tickets SET category = ? WHERE id = ?", (category, ticket_id))
    conn.commit()
    return json.dumps({"ticket_id": ticket_id, "category": category})
```

**Target** (RESEARCH.md Code Example §1, verified green):
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
Three things to preserve from the analog:
- The `json.dumps(...)` return stays **outside** the transaction block; executors
  return JSON strings, not dicts (CONVENTIONS.md — it is the wire format the model reads).
- `cur.lastrowid` must be read **inside** the block. After the lock drops another
  thread's insert has moved it.
- Existing inline comments (e.g. the "Email delivery is mocked" line) stay put.
- `build_registry` (`src/relay/tools.py:93-95`) keeps its parameter name and its
  closure style: `execute=lambda ticket_id, body: send_reply(conn, ticket_id, body)`.
  The closures are what keep `ToolSpec.execute` a sync `Callable[..., str]` (D-01/D-02).

`lookup_customer` (line 34-45) and `search_docs` (line 48-64) are single-statement /
no-statement reads and need no transaction.

---

### `src/relay/telemetry.py` (service, CRUD)

**Analog:** itself.

**Current** (`src/relay/telemetry.py:56-73`) — note the keyword-only signature style
that must be preserved verbatim (every caller uses keywords):
```python
def record_run(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    model: str,
    ...
) -> None:
    conn.execute(
        "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
        " output_tokens, cost_usd, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, model, duration_ms, steps, input_tokens, output_tokens, cost_usd, outcome),
    )
    conn.commit()
```
Wrap the `execute` + `commit` in `with conn.transaction():` (single statement, but the
implicit commit is what removes the cross-request commit hazard). **Keep `record_run`
synchronous** — RESEARCH.md Pitfall 6 measured that offloading it works but adds a new
`await` to the most fragile fifteen lines in the app for no measurable benefit.

`run_metrics` (`src/relay/telemetry.py:83-84`) does `SELECT * FROM runs ORDER BY id`
and stays sync — it is offloaded at the `/metrics` handler, not here.

---

### `src/relay/main.py` (controller, request-response + streaming)

**Analog:** itself. This is the highest-risk file: it holds three Phase 1 fixes that
must survive.

**Lifespan — the ordering that changes** (`src/relay/main.py:22-32`):
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
    yield
    conn.close()
```
Add `app.state.runs = RunRegistry()` alongside the other three `app.state`
assignments, and `await app.state.runs.drain(timeout=settings.shutdown_drain_seconds)`
**before** `conn.close()`. The teardown gains a comment in this file's established
style (a *why*, at the exact line it applies to) explaining that uvicorn cancels
in-flight tasks without awaiting them, so the stream's `finally` can still be pending.

**Handler offload — the read path** (`src/relay/main.py:245-251`):
```python
def _get_ticket(ticket_id: int) -> Ticket:
    row = app.state.conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "ticket not found")
    return Ticket(**dict(row))
```
Becomes `async def` with the query in `asyncio.to_thread`; the `HTTPException(404, ...)`
short-string form stays (CONVENTIONS.md: short strings for domain errors, dict details
only for the perimeter's product-copy rejections). Three call sites gain `await`:
`create_ticket` (line 106), `get_ticket` (line 111), `process_ticket` (line 122).

**Handler offload — the write path** (`src/relay/main.py:99-106`):
```python
async def create_ticket(payload: TicketCreate) -> Ticket:
    conn = app.state.conn
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        (payload.customer_email, payload.subject, payload.body),
    )
    conn.commit()
    return _get_ticket(cur.lastrowid)
```
Per RESEARCH.md Code Example §2: an inner `def _insert()` closure holding
`with app.state.conn.transaction() as db:` and returning `cur.lastrowid`, offloaded
with `asyncio.to_thread`, then `await _get_ticket(...)`.

**`/metrics`** (`src/relay/main.py:185-187`): `return await asyncio.to_thread(run_metrics, app.state.conn)`.

**503-while-draining (D-09)** goes in `process_ticket` immediately before
`reserve_run()` (`src/relay/main.py:132`). Copy the structured-detail convention
from `src/relay/ratelimit.py:208-222`, **not** the short-string form:
```python
    raise HTTPException(
        503,
        detail={
            "error": "daily_budget_exhausted",
            "spent_usd": round(spent, 4),
            "limit_usd": settings.max_daily_cost_usd,
            "resets_at": resets_at.isoformat(),
            "note": (
                "This demo caps its Claude spend at"
                f" ${settings.max_daily_cost_usd:.2f}/day and resets at 00:00 UTC."
                " Come back after the reset — the cap is a feature, not a fault."
            ),
        },
        headers={"Retry-After": str(retry_after)},
    )
```
So: `{"error": "shutting_down", "note": "<product copy explaining the deploy>"}`
plus a `Retry-After` header. `main.py` already raises perimeter-style errors this
way via `ratelimit`, and `test_daily_budget_503` (`tests/test_ratelimit.py:392-399`)
is the test shape that asserts it.

**Registration inside the generator — the load-bearing detail**
(`src/relay/main.py:126-138`, then `154-180`):
```python
    # Claim this run's worst-case cost now that the gate has admitted it. record_run
    # only fires once the stream ends, so without a reservation a burst of concurrent
    # runs would all read the same stale SUM and all clear the daily ceiling. The
    # token is claimed here but released below, and the two can be separated by a
    # cancellation that skips the release entirely — which is why the claim expires
    # on its own rather than trusting this handoff.
    token = reserve_run()

    async def event_stream():
        started = time.perf_counter()
        usage: dict = {}
        outcome = "incomplete"
        recorded = False
        try:
```
`run_token = app.state.runs.register(ticket_id=ticket.id)` goes as the **first
statement inside `event_stream`'s body** — deliberately *not* next to `reserve_run()`.
The comment above `reserve_run()` documents exactly why the two placements differ;
mirror that reasoning in a comment at the registration line.

**The `finally` that must not regress** (`src/relay/main.py:154-180`):
```python
        finally:
            # A plain finally, never a context manager: run_ticket suspends at every
            # yield, and anything held across a yield leaks into whatever coroutine
            # runs in between (see agent.py's run-span note).
            #
            # The row is written from here rather than after the loop because a client
            # that disconnects mid-stream cancels this generator at its suspended
            # yield: every Claude call already made is real money, and the daily
            # ceiling reads it back out of `runs`. ... The flag
            # keeps a second close from writing the row twice and double-charging
            # the ledger.
            if not recorded:
                recorded = True
                record_run(
                    app.state.conn,
                    ticket_id=ticket.id,
                    ...
                )
            # Released after the row exists, so the two are never both missing.
            release_run(token)
```
`app.state.runs.deregister(run_token)` is appended **after** `release_run(token)`.
Do not convert this `finally` into a context manager, do not add an `await` to the
`record_run` branch, and keep the `recorded` guard (D-07; guarded by
`test_mid_stream_disconnect_still_records_the_spend`).

**Imports** (`src/relay/main.py:1-19`) — relative, alphabetical within the local group:
```python
from .agent import run_ticket
from .auth import Tier, api_key_header, require_tier
from .config import settings
from .db import connect, init_db
from .guardrails import ToolPolicy
from .models import Ticket, TicketCreate
from .ratelimit import enforce, enforce_daily_budget, release_run, reserve_run
```
`from .runs import RunRegistry` sorts between `.ratelimit` and `.telemetry`.
`import asyncio` joins the stdlib group at the top (line 1-4).

---

### `src/relay/config.py` (config)

**Analog:** itself — settings are grouped by phase with a comment header per block
(`src/relay/config.py:18`, `21`, `43`):
```python
    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0
```
Add a new trailing block:
```python
    # Shutdown drain (phase 2 remaster). Nests inside uvicorn's
    # --timeout-graceful-shutdown, which nests inside fly.toml's kill_timeout.
    shutdown_drain_seconds: float = 5.0
```
Env var is `RELAY_SHUTDOWN_DRAIN_SECONDS` for free via `env_prefix="RELAY_"`
(line 8). No `Field(...)` alias needed — only the two settings with non-obvious
env names use one.

---

### `.env.example` (config docs)

**Analog:** itself — phase-grouped blocks with a `#` explanation above each.
Append one line under a short comment, matching the existing style
(`RELAY_MAX_RUN_COST_USD=0.50` etc.). Optional per RESEARCH.md, cheap for discoverability.

---

### `fly.toml` (config, platform)

**Analog:** itself — every non-obvious key carries a comment saying why
(`fly.toml:12-17`):
```toml
[env]
  RELAY_DB_PATH = '/data/relay.db'
  # Must match http_service.internal_port below; the container binds $PORT.
  PORT = '8080'
  # Only here: behind the Fly proxy, Fly-Client-IP is authoritative. Off-proxy the
  # header is client-controlled, so trusting it anywhere else is a rate-limit bypass.
  RELAY_TRUST_PROXY = 'true'
```
`kill_timeout = 30` is a **top-level** key: it must go after `primary_region` on
line 7 and **before** `[build]` on line 9, or TOML parses it into the last table.
Comment it in the same voice: Fly's default is 5s, which SIGKILLs mid-run and skips
the drain entirely. **Do not add `kill_signal`** (D-08).

---

### `Dockerfile` (config, build)

**Analog:** itself — each `ENV`/`CMD` line has a `#` rationale above it
(`Dockerfile:12-23`):
```dockerfile
# Honour $PORT so the image runs unchanged on Fly, Render, Railway, Cloud Run…
ENV PORT=8000
...
CMD ["sh", "-c", "uvicorn relay.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```
Target (RESEARCH.md Code Example §6): add `exec` so uvicorn is PID 1 and receives
the signal directly, and `--timeout-graceful-shutdown 20`. Keep the `sh -c` form —
`${PORT:-8000}` needs a shell.

---

### `tests/conftest.py` (test fixture — APPEND ONLY)

**Analog:** the existing `conn` fixture (`tests/conftest.py:23-28`):
```python
@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()
```
New `db` fixture is the same shape, file-backed:
```python
@pytest.fixture()
def db(tmp_path):
    db = connect(tmp_path / "relay.db")
    init_db(db)
    yield db
    db.close()
```
`tmp_path` is already used this way by the `client` fixture (`tests/conftest.py:37-40`).

**Do not modify** `_reset_limits` (14-20), `conn` (23-28), `registry` (31-33), or
`client` (36-49). D-03 plus RESEARCH.md's correction: `client` is *already*
file-backed at `tmp_path / "test.db"`, so the API half of the suite already
exercises WAL. Do **not** add a `RunRegistry` reset to `_reset_limits` — the
registry lives on `app.state`, so `TestClient`'s context manager already gives one
per test.

---

### `tests/helpers.py` (test double — APPEND ONLY)

**Analog:** `FakeClient` (`tests/helpers.py:25-33`):
```python
class FakeClient:
    """Plays back scripted responses in place of the Claude API."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return next(self._responses)
```
`TicketAwareFakeClient` copies this shape exactly — `SimpleNamespace(create=...)`
duck-typing of `client.messages.create`, an `async def _create`, one-line docstring
stating what the double does. It reuses the module's existing `response`,
`text_block`, `tool_use_block` builders (lines 6-22) rather than constructing
`SimpleNamespace` inline.

---

### `tests/test_db.py` (NEW — test, unit/CRUD)

**Analog:** `tests/test_ratelimit.py` unit half + `tests/test_tools.py`.

**Conventions to copy:**
- No test classes; module-level `def test_...` / `async def test_...`
  (`asyncio_mode = "auto"`, so async tests need no decorator).
- Section separators as comments: `# --- reset semantics ---`,
  `# --- in-flight reservation ---` (`tests/test_ratelimit.py:43`, `232`, `309`).
- Test names state the expected outcome in words:
  `test_daily_budget_raises_503_at_the_ceiling`,
  `test_a_stream_that_never_starts_leaks_only_until_the_ttl`.
- Comments explain *why the test exists*, not what it does
  (`tests/test_ratelimit.py:424-427`, `tests/test_observability.py:59-62`).

**Trap-documenting test pattern** — the analog is `tests/test_ratelimit.py:423-433`,
a test whose whole purpose is to pin a counter-intuitive behaviour so nobody
"fixes" it later. `test_wal_is_a_silent_no_op_on_memory_databases` is the same
genre and needs the same kind of comment.

**Deterministic concurrency** — no analog exists in this suite (see below); use a
`threading.Barrier`, never `time.sleep`.

---

### `tests/test_lifecycle.py` (NEW — test, unit + integration)

**Analog:** `tests/test_observability.py:58-82` — the existing test that drives the
handler coroutine directly to get at streaming lifecycle behaviour `TestClient`
cannot reach:
```python
def test_mid_stream_disconnect_still_records_the_spend(client):
    # SEC-03's ceiling reads SUM(runs.cost_usd), so a run whose row is never written
    # is spend the ceiling cannot see. A disconnect cancels the generator at its
    # suspended yield, which skips everything placed after the loop — the handler is
    # driven directly here because TestClient always drains the body.
    ticket_id = _make_ticket(client)
    app.state.client = FakeClient([...])

    async def abort_after_the_first_event():
        stream = await process_ticket(ticket_id)
        first = await stream.body_iterator.__anext__()
        assert first.startswith("event: usage")
        await stream.body_iterator.aclose()

    asyncio.run(abort_after_the_first_event())

    rows = app.state.conn.execute("SELECT cost_usd, outcome FROM runs").fetchall()
```
This is the exact template for `test_a_stream_that_never_starts_registers_nothing`
(call `await process_ticket(id)` and never iterate — cf. `tests/test_ratelimit.py:430`,
`asyncio.run(process_ticket(ticket_id))`) and for
`test_registry_is_empty_after_a_run_completes`.

**Fake-client injection pattern** (`tests/test_observability.py:24-33`): assign
`app.state.client = FakeClient([...])` *after* the `client` fixture has started the
app, then `client.post(f"/tickets/{ticket_id}/process")`.

**Ticket-creation helper** (`tests/test_observability.py:11-20`) — each test module
defines its own small `_make_ticket(client)` / module-level `TICKET` dict rather than
sharing a fixture; follow that.

**Assertion style:** direct DB reads via `app.state.conn.execute(...).fetchall()`
after the action, plus `pytest.approx` for floats (`tests/test_ratelimit.py:432`).

---

## Shared Patterns

### Structured logging
**Source:** `src/relay/agent.py:41` + `:207-209`, `src/relay/ratelimit.py:30` + `:114-117`
**Apply to:** `runs.py` (drain events), any new log line in `main.py`/`db.py`
```python
logger = logging.getLogger("relay.agent")
...
logger.info("tool.executed", extra={"ctx": {
    "ticket_id": ticket["id"], "tool": block.name, "is_error": is_error,
}})
```
Logger name mirrors the module. Message is a short dotted event name. All context
in `extra={"ctx": {...}}`, never interpolated into the message.

### Error responses
**Source:** short-string form `src/relay/main.py:124`, `:250`; structured form `src/relay/ratelimit.py:120-139`, `:208-222`
**Apply to:** the 503-while-draining response (structured form), any 404/409 (short form)
```python
raise HTTPException(404, "ticket not found")                      # domain errors
raise HTTPException(503, detail={"error": ..., "note": ...},      # perimeter rejections
                    headers={"Retry-After": str(retry_after)})    # read as product copy
```
The dict form is the exception, and `ratelimit.py:118-119` explicitly documents why
it diverges. A drain rejection is a perimeter rejection — use the dict form.

### Comment discipline
**Source:** `src/relay/main.py:126-131`, `:154-165`; `src/relay/agent.py:116-118`; `src/relay/ratelimit.py:39-46`
**Apply to:** every non-obvious placement decision in this phase
Every counter-intuitive line in this codebase carries a comment naming the failure
it prevents. This phase has four such lines and each needs one:
1. registration inside the generator body (never-started streams),
2. drain before `conn.close()` (uvicorn cancels without awaiting),
3. materialised `Result` instead of a live cursor (malformed rows, not "db is locked"),
4. `kill_timeout` top-level placement / `exec` in the Dockerfile CMD.

### Keyword-only signatures
**Source:** `src/relay/telemetry.py:56-67`, `src/relay/agent.py:45-52`
**Apply to:** `RunRegistry.register(*, ticket_id)`, `RunRegistry.drain(*, timeout)`
```python
def record_run(conn, *, ticket_id: int, model: str, duration_ms: int, ...) -> None:
```
Anything past 2-3 args, or any bool/int whose meaning is not obvious at the call
site, is keyword-only.

### Type hints
**Source:** throughout; `src/relay/agent.py:51` (`bound_ticket_id: int | None = None`), `src/relay/db.py:61` (`db_path: str | Path`)
Mandatory on every signature; modern `X | None`, never `Optional[X]`.

### Test fixture usage
**Source:** `tests/conftest.py:14-49`
`conn` = `:memory:` unit fixture, `client` = file-backed `TestClient` with the owner
key on default headers, `_reset_limits` = autouse process-state reset. New tests pick
`db` (new, file-backed) for storage-level assertions and `client` for anything
touching the app.

---

## No Analog Found

Two mechanisms have no precedent in this codebase. The planner should follow
RESEARCH.md's verified implementations rather than searching for a local pattern.

| Construct | Role | Data Flow | Reason | Use instead |
|-----------|------|-----------|--------|-------------|
| `Database.transaction()` — a sync `@contextlib.contextmanager` | storage | CRUD | The only context manager in `src/` is `@asynccontextmanager lifespan` (`src/relay/main.py:22`). No sync `@contextmanager` exists. | RESEARCH.md Pattern 1, Code Example §1 (executed green). Note the `except BaseException: rollback; raise` clause — a bare `except Exception` would let a `CancelledError` escape with the transaction open. |
| `asyncio.Event` + `asyncio.wait_for` drain | service | event-driven | No async coordination primitive exists anywhere in `src/`. `ratelimit.py`'s only async is `await _limiter.hit(...)`. | RESEARCH.md Pattern 3 / Code Example §3 (executed green). `except TimeoutError` (3.11+ alias), not `asyncio.TimeoutError`. |
| `threading.Barrier`-driven deterministic concurrency test | test | — | No test in the suite uses threads; `tests/test_ratelimit.py` uses `asyncio.run` only. | RESEARCH.md Validation Architecture rows DATA-01-d / DATA-01-g. Assert on **row contents**, not just absence of `OperationalError` — the measured failure mode was `customer_email=None`, and it was flaky (4 of 5 runs), so the phase gate requires 5 consecutive passes. |

---

## Metadata

**Analog search scope:** `src/relay/` (all 13 modules), `tests/` (all 9 files),
`fly.toml`, `Dockerfile`, `.env.example`
**Files scanned:** 25 (all read in full except `mcp_server.py`, read at 130-158, and
`test_ratelimit.py`, read at 1-60 plus grep-targeted ranges)
**Pattern extraction date:** 2026-08-09
**HEAD at extraction:** `8842c87`

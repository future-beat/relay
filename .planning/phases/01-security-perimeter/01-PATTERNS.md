# Phase 1: Security Perimeter - Pattern Map

**Mapped:** 2026-08-06
**Files analyzed:** 17 (2 new src modules, 7 modified src/config files, 2 new tests, 4 modified tests, 2 docs/scripts)
**Analogs found:** 15 / 17

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/relay/auth.py` (NEW) | middleware/guard | request-response | `src/relay/guardrails.py` (`ToolPolicy.denial_reason`) | role-match (policy gate; FastAPI dependency shape is new) |
| `src/relay/ratelimit.py` (NEW) | middleware/guard + service | request-response + CRUD read | `src/relay/telemetry.py` (`record_run`/`run_metrics` — module-level SQL over `runs`) | partial (SQL aggregation exact; limiter wiring is new) |
| `src/relay/config.py` (MOD) | config | n/a | itself (lines 7-27) — extend in place | exact |
| `src/relay/main.py` (MOD) | route/controller | request-response + streaming | itself (`process_ticket` lines 62-104) | exact |
| `src/relay/agent.py` (MOD) | service (agent loop) | event-driven/streaming | itself (`_execute_guarded` lines 40-55, tool_use branch lines 152-185) | exact |
| `src/relay/models.py` (MOD) | model | n/a | itself (`AgentEvent` lines 38-42) — comment only | exact |
| `src/relay/mcp_server.py` (MOD) | entry point | request-response (stdio) | itself (docstring lines 1-13) — docstring only | exact |
| `tests/test_auth.py` (NEW) | test (integration) | request-response | `tests/test_api.py` | exact |
| `tests/test_ratelimit.py` (NEW) | test (integration + unit) | request-response + CRUD | `tests/test_observability.py` | exact |
| `tests/conftest.py` (MOD) | test fixture | n/a | itself + `tests/test_api.py::client` fixture (lines 7-14) | exact |
| `tests/test_api.py` (MOD) | test (integration) | request-response | itself | exact |
| `tests/test_observability.py` (MOD) | test (integration) | streaming | itself | exact |
| `tests/test_guardrails.py` (MOD — SEC-04 tests) | test (integration) | event-driven | itself (`test_dry_run_denies_write_tools_but_allows_reads` lines 83-97) | exact |
| `tests/test_mcp.py` (MOD — SEC-05 tests) | test (unit) | request-response | itself (lines 1-30) | exact |
| `.env.example` (MOD) | config | n/a | itself | exact |
| `pyproject.toml` (MOD) | config | n/a | itself (`[project] dependencies`) | exact |
| `fly.toml` / `scripts/demo.sh` / `README.md` (MOD) | config / script / docs | n/a | `scripts/demo.sh` (curl calls, lines 8, 18) | exact for demo.sh; no analog for fly.toml `[env]` addition beyond existing entries |

## Pattern Assignments

### `src/relay/auth.py` (NEW — guard module, request-response)

**Analog:** `src/relay/guardrails.py` (module shape, denial semantics) + `src/relay/config.py` (settings access)

**Module docstring pattern** — every module opens with a *why*, phase-tagged docstring (`src/relay/guardrails.py:1-5`):
```python
"""Phase 2 guardrails: validated tool I/O, cost budgets, and write-tool policy.

Everything here is enforced in the agent loop — not just requested in the
prompt — so a misbehaving model run is contained by code.
"""
```
New module should open with an equivalent: "Phase 1 perimeter: API-key tiers ... enforced as route dependencies, never middleware, because a StreamingResponse locks its status line at 200."

**Imports pattern** — stdlib, blank line, third-party, blank line, relative intra-package (`src/relay/guardrails.py:7-12`, `src/relay/main.py:1-16`):
```python
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .models import TicketCategory
```
Every intra-package import is relative (`from .config import settings`). No `utils`/`helpers` module exists — do not create one.

**Denial-decision pattern** — a pure predicate returning `None` for "allowed", a message for "denied" (`src/relay/guardrails.py:63-69`):
```python
    def denial_reason(self, tier: str) -> str | None:
        if tier == "write" and not self.allow_writes:
            return (
                "This run is in dry-run mode: write actions are disabled by policy."
                " Summarise what you would have done instead."
            )
        return None
```
`resolve_tier(presented) -> Tier | None` mirrors this: pure function, `X | None` return, no raising. Only the FastAPI dependency wrapper raises.

**Error-raising pattern** — HTTP rejections use bare `HTTPException(status, "short lowercase message")` (`src/relay/main.py:72, 151`):
```python
        raise HTTPException(409, f"ticket is already {ticket.status.value}")
...
        raise HTTPException(404, "ticket not found")
```
Follow for 401/403/503: positional status, short lowercase message. Add `headers=` for `WWW-Authenticate`. For D-08's structured 429/503 bodies use a dict `detail` — a deliberate divergence from the short-string convention, justified by the friendly-copy requirement; note it in a comment.

**Domain-exception pattern** (if a `RelayAuthError` is wanted) — `src/relay/guardrails.py:51-52`:
```python
class ToolInputError(Exception):
    pass
```
Bare subclass, no custom `__init__`. Prefer raising `HTTPException` directly at the dependency boundary; only add a domain exception if a non-HTTP caller needs it.

**Type-hint conventions:** mandatory hints on every signature, modern `X | None` unions (never `Optional[X]`), keyword-only `*` past 2-3 args (`src/relay/telemetry.py:56-67`).

**Logging pattern** (for `auth.rejected`) — `src/relay/agent.py:95-96`:
```python
logger = logging.getLogger("relay.agent")
...
    logger.info("run.start", extra={"ctx": {"ticket_id": ticket["id"], "model": settings.model,
                                            "allow_writes": policy.allow_writes}})
```
Use `logging.getLogger("relay.auth")`, dotted event names as the message, all context in `extra={"ctx": {...}}`. **Never** put the presented key in `ctx` — `JsonFormatter` dumps the whole dict to stdout (`src/relay/telemetry.py:31`).

---

### `src/relay/ratelimit.py` (NEW — guard module + SQL aggregation)

**Analog:** `src/relay/telemetry.py` (module-level SQL constants over `runs`, `sqlite3.Connection` passed as first positional arg)

**SQL-against-`runs` pattern** (`src/relay/telemetry.py:68-73, 84`):
```python
    conn.execute(
        "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
        " output_tokens, cost_usd, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, model, duration_ms, steps, input_tokens, output_tokens, cost_usd, outcome),
    )
    conn.commit()
...
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id").fetchall()]
```
Note: implicit string concatenation across lines for long SQL, `?` placeholders always (the daily-spend query has no parameters at all — keep it that way), `conn` as the first positional parameter, plain `sqlite3.Connection` type hint, no ORM.

**UTC datetime import pattern** (`src/relay/telemetry.py:14, 26`):
```python
from datetime import UTC, datetime
...
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
```
`next_utc_midnight()` must use `from datetime import UTC, datetime, timedelta` and `datetime.now(UTC)` — this exact import form already exists in the codebase.

**Private-helper naming** (`src/relay/telemetry.py:76`):
```python
def _percentile(sorted_values: list[int], pct: float) -> int:
```
Single leading underscore for module-private helpers. Module-level mutable state (`_storage`, `_limiter`, `_reserved_usd`) follows the same underscore convention. Precedent for module-level singletons: `tracer = trace.get_tracer("relay")` (`src/relay/agent.py:37`).

**Aggregation-function shape** (`src/relay/telemetry.py:83-107`) — `run_metrics(conn) -> dict[str, Any]`: takes the connection, computes, returns a plain dict. `spent_today(conn) -> float` follows the same shape.

**Logging events:** `ratelimit.exceeded`, `budget.daily_exceeded` via `logging.getLogger("relay.ratelimit")`, same `extra={"ctx": {...}}` form as above. Log the resolved tier and IP, never the key.

---

### `src/relay/config.py` (MODIFIED — config)

**Analog:** itself. Extend the existing class; do not introduce a second config mechanism.

**Full current shape** (lines 7-27) — new settings slot into the commented-group structure:
```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    # Read without the RELAY_ prefix so the same variable works for the SDK's own lookup.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-5"
    db_path: Path = Path("relay.db")
    kb_dir: Path = Path("kb")
    max_agent_steps: int = 10
    max_tokens: int = 16000

    # MCP server (phase 5)
    mcp_allow_writes: bool = True          # ← flip to False (SEC-05)

    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0


settings = Settings()
```
**Copy exactly:** the `# Group name (phase N)` comment above each block; plain typed attributes with literal defaults; `str | None = None` for optional secrets (`api_key`, `demo_key` — do **not** use `Field(...)` unless a `validation_alias` is needed, as only `anthropic_api_key` requires one). Add a `# Security perimeter (phase 1)` block. `extra="ignore"` means new env vars are harmless.

**Module-level singleton:** `settings = Settings()` at the bottom; every consumer does `from .config import settings`. Tests override with `monkeypatch.setattr(settings, "field", value)` (`tests/test_api.py:11`) — so new settings must be plain instance attributes, not properties.

---

### `src/relay/main.py` (MODIFIED — routes, request-response + streaming)

**Analog:** itself — `process_ticket` (lines 62-104) and `create_ticket` (lines 46-54).

**Route-declaration pattern** (lines 46-63):
```python
@app.post("/tickets", response_model=Ticket, status_code=201)
async def create_ticket(payload: TicketCreate) -> Ticket:
...
@app.get("/tickets/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: int) -> Ticket:
...
@app.post("/tickets/{ticket_id}/process")
async def process_ticket(ticket_id: int, dry_run: bool = False) -> StreamingResponse:
```
Add the gate either as `dependencies=[Depends(...)]` in the decorator (when the tier value is unused) or as a typed default param `tier: str = Depends(run_gate)` (when the handler needs the tier). Keep return type hints. **Do not touch** `root`, `health`, `metrics`, `dashboard` — they stay public (Docker `HEALTHCHECK` + CI `curl -sf /health`).

**Import pattern to extend** (lines 5-7):
```python
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
```
Add `Depends`, `Request` to the `fastapi` import; add `from .auth import ...` / `from .ratelimit import ...` to the relative-import block (lines 9-16), alphabetically ordered by module.

**`app.state` access pattern** (lines 25-27, 48, 79-81, 92, 109, 147):
```python
    app.state.conn = conn
    app.state.registry = build_registry(conn, settings.kb_dir)
    app.state.client = AsyncAnthropic(api_key=settings.anthropic_api_key)
...
    conn = app.state.conn
```
Handlers and helpers reference the module-level `app` directly (`_get_ticket` at line 147 does), not `request.app`. `run_gate` may follow either; using `request.app.state.conn` is safer inside a dependency but `app.state.conn` matches existing style — pick one and be consistent.

**Streaming handler + reservation seam** (lines 70-104) — the in-flight reservation must wrap around this, in the handler, and release in a `finally` *inside* `event_stream` after `record_run`, or around the whole generator. Note the existing structure the reservation has to thread through:
```python
    ticket = _get_ticket(ticket_id)
    if ticket.status != "open":
        raise HTTPException(409, f"ticket is already {ticket.status.value}")

    async def event_stream():
        started = time.perf_counter()
        ...
            yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
        record_run(
            app.state.conn,
            ticket_id=ticket.id,
            ...
        )
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```
**Critical:** the SSE formatter at line 90 is type-agnostic — the new `guardrail` event needs **zero** changes here. **Do not** add `async with` around the generator (see `src/relay/agent.py:83-85` warning). **Do not** add `@app.middleware("http")`.

**Dashboard demo-key line:** `DASHBOARD_HTML` is a module-level triple-quoted constant (lines 112-138) with inline CSS/JS. Add the published demo key as one line sourced from `settings.demo_key` (e.g. an f-string or a `.replace()` at serve time in `dashboard()`, line 141-143) rather than a hardcoded literal, so README and dashboard cannot drift.

---

### `src/relay/agent.py` (MODIFIED — guard chain + guardrail event)

**Analog:** itself. This is the highest-risk edit; copy the surrounding shape exactly.

**Current guard chain to extend** (lines 40-55) — insert the binding check after `validate_tool_input`, before `spec.execute`, and keep the `(str, bool)` return arity:
```python
def _execute_guarded(spec: ToolSpec | None, name: str, raw_input: dict[str, Any],
                     policy: ToolPolicy) -> tuple[str, bool]:
    """Run one tool call through the guardrail chain. Returns (result_json, is_error)."""
    if spec is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    denial = policy.denial_reason(spec.tier)
    if denial:
        return json.dumps({"error": denial, "denied_by": "policy"}), True
    try:
        validated = validate_tool_input(spec.input_model, raw_input)
    except ToolInputError as exc:
        return json.dumps({"error": str(exc)}), True
    try:
        return spec.execute(**validated), False
    except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
        return json.dumps({"error": str(exc)}), True
```
**Copy:** the `{"error": ..., "denied_by": "..."}` payload shape — `denied_by: "policy"` at line 47 is the exact precedent for `denied_by: "ticket_binding"`. Note the multi-line signature continuation style (params aligned under the opening paren, no trailing-comma-per-line here); adding `*, bound_ticket_id: int | None = None` will require reflowing to one-param-per-line — that's fine, `record_run` (`telemetry.py:56-67`) is the codebase's one-param-per-line + keyword-only precedent.

**Caller to modify** (lines 152-185) — the tool_use branch. Note that `json.loads(result)` already happens at line 180; parse once and reuse:
```python
                elif block.type == "tool_use":
                    yield AgentEvent(
                        type="tool_use", data={"tool": block.name, "input": block.input}
                    )
                    spec = registry.get(block.name)
                    with tracer.start_as_current_span(
                        f"tool.{block.name}", context=run_ctx
                    ) as span:
                        result, is_error = _execute_guarded(spec, block.name, block.input, policy)
                        span.set_attributes({
                            "relay.tool.tier": spec.tier if spec else "unknown",
                            "relay.tool.is_error": is_error,
                        })
                    logger.info("tool.executed", extra={"ctx": {
                        "ticket_id": ticket["id"], "tool": block.name, "is_error": is_error,
                    }})
                    tool_results.append(...)
                    yield AgentEvent(
                        type="tool_result",
                        data={
                            "tool": block.name,
                            "result": json.loads(result),
                            "is_error": is_error,
                        },
                    )
                    if not is_error and block.name in TERMINAL_TOOLS:
                        resolved_via = block.name
```
**Span-attribute pattern to copy** for `relay.tool.binding_violation`: `span.set_attributes({...})` with dotted `relay.*` keys, called *inside* the `with` block.

**Warning-log pattern** for `guardrail.ticket_id_mismatch` — copy `run.budget_exceeded` (lines 195-197):
```python
                logger.warning("run.budget_exceeded", extra={"ctx": {
                    "ticket_id": ticket["id"], **budget.snapshot(),
                }})
```

**Docstring maintenance:** the module docstring (lines 1-16) enumerates guardrails by phase. Add a phase-1 bullet: server-side `ticket_id` binding.

**Anti-pattern reminder from this file** (lines 83-85) — the explicit reason no context manager may span a `yield`:
```python
    # The run span is parented explicitly (not made "current") because this is
    # a generator: execution suspends at every yield, and a current-span
    # context manager would leak across whatever runs in between.
```

---

### `src/relay/models.py` (MODIFIED — comment only)

**Analog:** itself (lines 38-42):
```python
class AgentEvent(BaseModel):
    """One step in an agent run, streamed to the client as SSE."""

    type: str  # "text" | "tool_use" | "tool_result" | "resolution" | "error"
    data: dict[str, Any]
```
The event-type union lives in a trailing `#` comment, not a `Literal`. Add `"guardrail"` to that comment. (Note: `"usage"` is already missing from the list — fix while there, or leave; flag as planner discretion.) No schema change, no validation change.

---

### `src/relay/mcp_server.py` (MODIFIED — docstring only)

**Analog:** itself (lines 1-13). The one line to invert:
```python
Set RELAY_MCP_ALLOW_WRITES=false to serve a read-only tool surface.
```
→ "Writes are disabled by default; set `RELAY_MCP_ALLOW_WRITES=true` to enable them."

**Do not change** `create_server` (line 127) — `ToolPolicy(allow_writes=settings.mcp_allow_writes)` already reads the setting at call time. **Do not change** `call_mcp_tool` (line 119) — it unpacks exactly two values from `_execute_guarded`, which is why the arity must stay `(str, bool)`.

---

### `tests/test_auth.py` (NEW) and `tests/test_ratelimit.py` (NEW)

**Analog:** `tests/test_api.py` (integration, TestClient) and `tests/test_guardrails.py` (unit + section comments)

**Test file structure** (`tests/test_api.py:1-14`):
```python
import pytest
from fastapi.testclient import TestClient

from relay.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from relay.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    with TestClient(app) as client:
        yield client
```
Note: tests import the installed package as `relay.*` (editable install), never relative. `from relay.config import settings` is imported **inside** the fixture, deliberately, so the module-level settings singleton is patched after app import.

**Assertion style** (`tests/test_api.py:17-41`) — short, one behavior per test, direct `assert`, no helper assertion library:
```python
def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
...
def test_get_missing_ticket_404(client):
    assert client.get("/tickets/9999").status_code == 404
```

**Section-comment convention** (`tests/test_guardrails.py:56, 81, 111, 142`):
```python
# --- input validation ---
# --- write policy / dry-run ---
# --- budget ---
# --- API failure ---
```
Use for `test_ratelimit.py`: `# --- moving window ---`, `# --- client ip ---`, `# --- daily spend ---`.

**Async test style** (`tests/test_guardrails.py:70`) — bare `async def test_...`, no decorator (`asyncio_mode = "auto"` in `pyproject.toml`).

**Test naming** — full behavioral sentences stating the expected outcome: `test_dry_run_never_writes_to_db`, `test_normal_run_ending_without_action_is_error`. Follow for `test_missing_key_returns_401_with_challenge`, `test_daily_sum_is_utc_day_scoped`.

**DB-seeding for the daily-spend tests** — reuse the existing helper rather than raw INSERTs (`tests/test_observability.py:75-84`):
```python
def test_percentiles(conn):
    from relay.telemetry import record_run

    for ms in [100, 200, 300, 400, 1000]:
        record_run(conn, ticket_id=1, model="m", duration_ms=ms, steps=1,
                   input_tokens=1, output_tokens=1, cost_usd=0.01, outcome="send_reply")
```
`record_run(..., cost_usd=...)` is the clean way to seed today's spend. For the yesterday/UTC-boundary test, a raw parameterized INSERT with an explicit `created_at` is required (`record_run` has no `created_at` param).

---

### `tests/conftest.py` (MODIFIED — fixtures)

**Analog:** itself (full file) + the duplicated `client` fixture in `test_api.py:7-14` and `test_observability.py:12-19`:
```python
from pathlib import Path

import pytest

from relay.db import connect, init_db
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def registry(conn):
    return build_registry(conn, KB_DIR)
```
**Copy:** `@pytest.fixture()` with explicit empty parens (consistent throughout), yield-teardown shape, module-level `KB_DIR` constant. Move the duplicated `client` fixture here (adding key monkeypatches + `TestClient(app, headers={"X-API-Key": ...})`) and delete the two local copies. Add the autouse `_reset_limits` fixture — underscore-prefixed, matching the private-helper convention.

---

### `tests/test_api.py`, `tests/test_observability.py` (MODIFIED)

**Analog:** themselves. Delete the local `client` fixtures (`test_api.py:7-14`, `test_observability.py:12-19`) in favour of the shared conftest one; test bodies stay byte-identical because the key rides on the `TestClient` default headers. `test_observability.py::_make_ticket` (lines 22-31) needs no change for the same reason.

Per RESEARCH Pitfall 2: keep `test_get_missing_ticket_404` asserting 404 (now authed) and add a *separate* unauthenticated→401 test in `test_auth.py`.

---

### `tests/test_guardrails.py` (MODIFIED — SEC-04 tests)

**Analog:** `test_dry_run_denies_write_tools_but_allows_reads` (lines 83-97) — the exact shape a binding-denial test should copy:
```python
async def test_dry_run_denies_write_tools_but_allows_reads(registry):
    client = FakeClient([
        _response([
            _tool_use("search_docs", {"query": "rate limits"}, id="t1"),
            _tool_use("send_reply", {"ticket_id": 1, "body": "A long enough grounded reply."}, id="t2"),
        ]),
        _response([_text("Noted, dry-run.")], stop_reason="end_turn"),
    ])
    events = await collect(
        run_ticket(client, registry, TICKET, policy=ToolPolicy(allow_writes=False))
    )
    results = {e.data["tool"]: e.data for e in events if e.type == "tool_result"}
    assert results["search_docs"]["is_error"] is False
    assert results["send_reply"]["is_error"] is True
    assert results["send_reply"]["result"]["denied_by"] == "policy"
```
Reuse the file's existing scaffolding verbatim — `TICKET` dict (lines 14-19), `_text`/`_tool_use`/`_usage`/`_response` builders (lines 22-38), `FakeClient` (lines 41-49), `collect` (lines 52-53). The multi-response `FakeClient([...])` script is exactly the mechanism for the "denied then retried, run still resolves" test (Pitfall 3).

Side-effect assertion pattern (`test_dry_run_never_writes_to_db`, lines 100-108):
```python
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0
```
Use for "nothing written to ticket 99".

---

### `.env.example`, `pyproject.toml`, `scripts/demo.sh`, `fly.toml`

**`.env.example` pattern** (full current file) — commented groups, defaults shown, no real secrets:
```
# Copy to .env and fill in. Never commit .env.
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides (defaults shown)
RELAY_MODEL=claude-sonnet-5
RELAY_DB_PATH=relay.db
RELAY_MAX_AGENT_STEPS=10
RELAY_MAX_RUN_COST_USD=0.50
RELAY_MCP_ALLOW_WRITES=true
```
Add a `# Security perimeter` group with `RELAY_API_KEY`/`RELAY_DEMO_KEY` dev placeholders (needed so local setup stays one copy command under fail-closed auth), `RELAY_MAX_DAILY_COST_USD=5.0`, `RELAY_TRUST_PROXY=false`; flip `RELAY_MCP_ALLOW_WRITES=false`.

**`pyproject.toml`** — append `"limits>=5.8,<6",` to `[project] dependencies` (runtime, not the `dev` extra). Existing entries are unquoted-floor style (`"fastapi>=0.115",`) with trailing commas; the upper bound is a deliberate deviation, keep it.

**`scripts/demo.sh`** (lines 8, 18) — both curls need the header:
```bash
TICKET_ID=$(curl -s -X POST "$BASE/tickets" \
  -H "Content-Type: application/json" \
  -d '{...}' | python3 -c "...")
...
curl -N -X POST "$BASE/tickets/$TICKET_ID/process"
```
Add `-H "X-API-Key: ${RELAY_DEMO_KEY:-<published-demo-key>}"` following the existing `"${RELAY_URL:-http://127.0.0.1:8000}"` env-with-default idiom (line 6).

**`fly.toml`** — add `RELAY_TRUST_PROXY = 'true'` to the existing `[env]` block alongside `PORT`/`RELAY_DB_PATH` (single-quoted TOML strings, matching existing entries).

## Shared Patterns

### Structured logging
**Source:** `src/relay/agent.py:36, 95-96, 165-167, 195-197`; formatter at `src/relay/telemetry.py:23-34`
**Apply to:** `auth.py`, `ratelimit.py`, the `agent.py` guardrail branch
```python
logger = logging.getLogger("relay.agent")
...
logger.info("tool.executed", extra={"ctx": {
    "ticket_id": ticket["id"], "tool": block.name, "is_error": is_error,
}})
```
Dotted event name as the message; all context in `extra={"ctx": {...}}`, never f-string-interpolated. New events: `auth.rejected`, `ratelimit.exceeded`, `budget.daily_exceeded`, `guardrail.ticket_id_mismatch`. `JsonFormatter` flattens `ctx` into the top-level JSON line (`telemetry.py:31`) — so no key material may ever enter `ctx`.

### HTTP error raising
**Source:** `src/relay/main.py:71-72, 150-151`
**Apply to:** all new route dependencies
```python
    if ticket.status != "open":
        raise HTTPException(409, f"ticket is already {ticket.status.value}")
...
    if row is None:
        raise HTTPException(404, "ticket not found")
```
Positional `(status, detail)`, short lowercase message, no custom exception-handler middleware anywhere in the project. New: `headers=` kwarg for `WWW-Authenticate`/`Retry-After`/`X-RateLimit-*`, and dict `detail` for the D-08 friendly bodies.

### Model-readable denial (never raise into the loop)
**Source:** `src/relay/guardrails.py:63-69` + `src/relay/agent.py:45-47`
**Apply to:** the `ticket_id` binding check
```python
    denial = policy.denial_reason(spec.tier)
    if denial:
        return json.dumps({"error": denial, "denied_by": "policy"}), True
```
Denials are JSON-encoded strings returned as `(result, is_error=True)`, carrying a `denied_by` discriminator. Message is phrased as an instruction the model can act on ("Summarise what you would have done instead") — mirror that for the binding denial ("Retry with ticket_id=1") per Pitfall 3.

### Settings access
**Source:** `src/relay/config.py:27` + every consumer (`agent.py:28`, `main.py:11`, `mcp_server.py:26`)
**Apply to:** `auth.py`, `ratelimit.py`
```python
from .config import settings
```
Module-level singleton, read at call time (not captured at import time) so `monkeypatch.setattr(settings, ...)` in tests takes effect. **Important for `ratelimit.py`:** if `LIMITS` is built at import time from `settings.*`, monkeypatched limit values won't apply — build the `RateLimitItem` lazily or expose a rebuild hook.

### Test fixtures
**Source:** `tests/conftest.py:11-21`, `tests/test_api.py:7-14`
**Apply to:** `test_auth.py`, `test_ratelimit.py`, the migrated `client` fixture
```python
@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()
```
`@pytest.fixture()` with explicit parens; yield-teardown; `monkeypatch.setattr(settings, ...)` for config overrides; `tmp_path / "test.db"` for file-backed DBs (required for the cold-start-survival test).

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `src/relay/auth.py` — FastAPI dependency/`Security` wiring | middleware | request-response | No route dependency, `Depends`, or `fastapi.security` usage exists anywhere in the codebase today. Follow RESEARCH.md Code Example 1 for the dependency-factory shape; use the codebase analogs above only for module/naming/error/logging conventions. |
| `src/relay/ratelimit.py` — `limits` library wiring | middleware | request-response | New dependency; no async third-party state object is held at module level today (`tracer` is the closest, `agent.py:37`). Follow RESEARCH.md Code Examples 2-3 for the `MemoryStorage`/`MovingWindowRateLimiter` API surface (verified against 5.8.0). |

## Metadata

**Analog search scope:** `src/relay/`, `tests/`, `scripts/`, repo-root config (`pyproject.toml`, `.env.example`, `fly.toml`, `Dockerfile`)
**Files scanned:** 12 source modules, 8 test modules, 5 config/script files
**Pattern extraction date:** 2026-08-06

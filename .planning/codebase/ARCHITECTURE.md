<!-- refreshed: 2026-08-05 -->
# Architecture

**Analysis Date:** 2026-08-05

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                    HTTP API (FastAPI)                        │
│                  `src/relay/main.py`                         │
│   POST /tickets  GET /tickets/{id}  POST /tickets/{id}/process│
│   GET /metrics   GET /dashboard     GET /health               │
└───────────────┬─────────────────────────────┬────────────────┘
                │                              │
                ▼                              ▼
┌───────────────────────────────┐   ┌───────────────────────────┐
│   Agent Loop (hand-rolled)     │   │   MCP Server (stdio)      │
│  `src/relay/agent.py`          │   │ `src/relay/mcp_server.py` │
│  run_ticket(): Claude request  │   │  exposes same tool        │
│  -> tool execute -> append     │   │  registry to MCP clients  │
└───────┬─────────────┬──────────┘   └──────────┬─────────────────┘
        │             │                         │
        ▼             ▼                         ▼
┌────────────────┐ ┌────────────────────────────────────────────┐
│ Guardrails      │ │ Tool Registry                              │
│`guardrails.py`  │ │`src/relay/tools.py`                        │
│ Pydantic input  │ │ ToolSpec: schema + tier + input_model +    │
│ validation,     │ │ execute() closure over sqlite conn/kb_dir  │
│ ToolPolicy,     │ │ lookup_customer, search_docs, set_category,│
│ RunBudget       │ │ send_reply, create_escalation              │
└─────────────────┘ └───────────────┬────────────────────────────┘
                                     │
                     ┌───────────────┴────────────────┐
                     ▼                                 ▼
        ┌─────────────────────────┐       ┌──────────────────────────┐
        │  SQLite Store            │       │  Knowledge Base (files)  │
        │ `src/relay/db.py`        │       │  `kb/*.md`                │
        │ tickets, customers,      │       │  keyword-searched by     │
        │ escalations, replies,    │       │  search_docs()            │
        │ runs                     │       │                            │
        └─────────────────────────┘       └──────────────────────────┘

Cross-cutting: telemetry.py (structured logs + OTel spans + run metrics),
config.py (pydantic-settings), prompts.py (system prompt + ticket framing)
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| FastAPI app / lifespan | Wires DB connection, tool registry, Anthropic client into `app.state`; exposes HTTP routes | `src/relay/main.py` |
| Agent loop | Drives the Claude request/tool-execute/append cycle, yields `AgentEvent`s, enforces budget/step limits | `src/relay/agent.py` |
| Guardrails | Pydantic validation of tool inputs, write-tier policy (`ToolPolicy`), cost ceiling (`RunBudget`) | `src/relay/guardrails.py` |
| Tool registry | Defines Claude-facing tool schemas and their local executors bound to a sqlite connection + kb dir | `src/relay/tools.py` |
| Data models | Pydantic request/response/domain models (`Ticket`, `TicketCreate`, `AgentEvent`, enums) | `src/relay/models.py` |
| Storage | SQLite schema, connection factory, seed data | `src/relay/db.py` |
| Config | Environment-driven settings (`RELAY_` prefix + `.env`) | `src/relay/config.py` |
| Prompts | System prompt and per-ticket user-message framing | `src/relay/prompts.py` |
| Telemetry | JSON log formatter, OTel tracer setup, run persistence/aggregation for `/metrics` | `src/relay/telemetry.py` |
| MCP server | Exposes the same tool registry (plus ticket-lifecycle tools) over the Model Context Protocol via stdio | `src/relay/mcp_server.py` |
| Eval harness | Runs the agent against a golden dataset, grades deterministically + via LLM judge | `src/relay/evals.py` |

## Pattern Overview

**Overall:** Thin service architecture around a hand-rolled agent loop — not a layered MVC or hexagonal design. There is no separate "controller/service/repository" split; instead, a single FastAPI module wires together a small number of focused modules (agent, tools, guardrails, db, telemetry) that each own one concern.

**Key Characteristics:**
- No agent framework (LangChain/etc.) — `run_ticket()` in `src/relay/agent.py` is an explicit while/for loop against the raw Anthropic Messages API, chosen deliberately so control flow, guardrails, and the event stream are fully visible and testable (see module docstring).
- Tool definitions are colocated with their executors as `ToolSpec` dataclasses (`src/relay/tools.py`), each carrying a Claude JSON schema, a permission tier (`"read"`/`"write"`), a Pydantic input model, and an `execute` closure bound to the live `sqlite3.Connection` and `kb_dir`.
- Guardrails are enforced in code, not just prompted: input validation happens before every tool call, write-tier tools are gated by `ToolPolicy`, and runs are hard-capped by `RunBudget` on cumulative dollar cost (`src/relay/guardrails.py`).
- Everything (HTTP API and MCP server) shares one tool registry built by `build_registry()` (`src/relay/tools.py`), so behavior does not diverge between entry points; `mcp_server.py` extends it with two ticket-lifecycle-only tools.
- The system is single-process, single-connection: one `sqlite3.Connection` is created at FastAPI startup and stored in `app.state.conn`, reused by every request (see `lifespan()` in `src/relay/main.py`).

## Layers

**HTTP Layer:**
- Purpose: Accept ticket CRUD requests, stream agent runs as Server-Sent Events, serve a metrics dashboard.
- Location: `src/relay/main.py`
- Contains: FastAPI route handlers, SSE event formatting, a self-contained HTML/JS dashboard string.
- Depends on: agent loop (`agent.run_ticket`), guardrails (`ToolPolicy`), db (`connect`, `init_db`), telemetry (`record_run`, `run_metrics`), tools (`build_registry`).
- Used by: External clients (curl, browser dashboard, `scripts/demo.sh`).

**Agent Loop Layer:**
- Purpose: Own the full lifecycle of one ticket run against Claude — request, tool dispatch, budget/step enforcement, tracing, and terminal-action detection.
- Location: `src/relay/agent.py`
- Contains: `run_ticket()` async generator, `_execute_guarded()` per-tool-call guard chain, OTel span management.
- Depends on: guardrails, models (`AgentEvent`), prompts, tools (`ToolSpec`), config (`settings`).
- Used by: `main.py` (HTTP), `evals.py` (eval harness).

**Guardrails Layer:**
- Purpose: Enforce safety/cost constraints independent of the model's behavior.
- Location: `src/relay/guardrails.py`
- Contains: Pydantic input models per tool, `validate_tool_input()`, `ToolPolicy` (write-tier gating), `RunBudget` (token/cost accounting with cache-pricing multipliers).
- Depends on: models (`TicketCategory`).
- Used by: agent loop, mcp_server.

**Tool Registry Layer:**
- Purpose: Define what the agent/MCP client can do, and how each action executes against storage/kb.
- Location: `src/relay/tools.py`
- Contains: `ToolSpec` dataclass, tool executor functions (`lookup_customer`, `search_docs`, `create_escalation`, `send_reply`, `set_category`), `build_registry()` factory.
- Depends on: guardrails (input models), sqlite3, kb directory on disk.
- Used by: agent loop, mcp_server, evals.

**Storage Layer:**
- Purpose: Own the SQLite schema and connection lifecycle.
- Location: `src/relay/db.py`
- Contains: `SCHEMA` DDL string, `SEED_CUSTOMERS`, `connect()`, `init_db()`.
- Depends on: nothing internal (stdlib `sqlite3` only).
- Used by: main, tools, mcp_server, evals.

**Observability Layer:**
- Purpose: Structured logging, distributed tracing, and run-metrics aggregation.
- Location: `src/relay/telemetry.py`
- Contains: `JsonFormatter`, `configure_logging()`, `setup_tracing()`, `record_run()`, `run_metrics()`, `_percentile()`.
- Depends on: OpenTelemetry SDK, sqlite3 (`runs` table).
- Used by: main (lifespan + `/metrics`), agent (tracer only, via `opentelemetry.trace`).

**MCP Layer:**
- Purpose: Alternate entry point exposing the same tool registry (plus ticket-lifecycle tools) over the Model Context Protocol for external MCP clients (Claude Desktop, Claude Code, IDEs).
- Location: `src/relay/mcp_server.py`
- Contains: `build_mcp_registry()`, `list_mcp_tools()`, `call_mcp_tool()`, `create_server()`, stdio `amain()`/`main()` entry points.
- Depends on: agent (`_execute_guarded`), config, db, guardrails, tools.
- Used by: External MCP clients only; run standalone via `python -m relay.mcp_server`.

## Data Flow

### Primary Request Path (process a ticket via HTTP)

1. Client `POST /tickets/{id}/process` (`src/relay/main.py:62`) — loads the ticket, rejects if not `open`, builds a `ToolPolicy(allow_writes=not dry_run)`.
2. `run_ticket()` is invoked as an async generator (`src/relay/agent.py:58`); it appends the ticket framing (`prompts.ticket_prompt`) as the first user message and loops up to `settings.max_agent_steps`.
3. Each loop iteration calls `client.messages.create()` against Claude, wrapped in an OTel span (`agent.py:101`) and a structured `model.response` log line.
4. Tool-use content blocks are dispatched through `_execute_guarded()` (`agent.py:40`), which checks `ToolPolicy` denial, validates input via the tool's Pydantic model, then calls the bound executor from `tools.py` (which reads/writes SQLite or greps `kb/*.md`).
5. Every step yields an `AgentEvent` (`text`, `tool_use`, `tool_result`, `usage`, `resolution`, or `error`) back through the generator to `main.py`'s `event_stream()`, which formats each as an SSE frame (`event: <type>\ndata: <json>\n\n`).
6. On loop exit, `main.py` calls `telemetry.record_run()` to persist duration/tokens/cost/outcome into the `runs` table, then yields a final `event: done` frame.

### Metrics / Dashboard Flow

1. Browser loads `GET /dashboard` (`src/relay/main.py:141`) — returns a static HTML/JS page.
2. Page JS polls `GET /metrics` every 5s, which calls `telemetry.run_metrics()` (`src/relay/telemetry.py:83`) to aggregate the `runs` table into percentile latency, cost totals, and the last 20 runs.

**State Management:**
- All persistent state lives in one SQLite file (`relay.db` by default, configurable via `RELAY_DB_PATH`/`settings.db_path`), opened once at startup and shared across requests via `app.state.conn` (`check_same_thread=False`).
- No in-memory session/conversation state persists between HTTP requests — each `/process` call re-derives the full message history within a single `run_ticket()` invocation; nothing is cached between ticket runs.
- Agent run-in-progress state (message list, budget accumulator) is local to the `run_ticket()` generator's stack frame and is discarded when the generator completes.

## Key Abstractions

**ToolSpec (`src/relay/tools.py`):**
- Purpose: Bundle a Claude-facing JSON tool schema with its permission tier, Pydantic validator, and executor in one frozen dataclass, so the agent loop and MCP server can treat all tools uniformly.
- Examples: `build_registry()` returns `dict[str, ToolSpec]` for `lookup_customer`, `search_docs`, `set_category`, `send_reply`, `create_escalation`.
- Pattern: Executors are closures capturing the live `sqlite3.Connection`/`kb_dir` at registry-build time (not passed per-call), so tool signatures match exactly what Claude sends.

**AgentEvent (`src/relay/models.py`):**
- Purpose: Uniform envelope (`type` + `data`) for every observable step of an agent run, streamed as SSE and also consumed synchronously by the eval harness.
- Examples: `type="text"`, `"tool_use"`, `"tool_result"`, `"usage"`, `"resolution"`, `"error"`.
- Pattern: Producer (`agent.run_ticket`) is decoupled from consumers (`main.py` SSE formatter, `evals.py` `extract_outcome()`), both of which just filter on `event.type`.

**RunBudget (`src/relay/guardrails.py`):**
- Purpose: Accumulate token usage/cost across a multi-step run and enforce a hard dollar ceiling (`settings.max_run_cost_usd`), including Anthropic prompt-cache pricing multipliers.
- Examples: instantiated once per `run_ticket()` call; `.add(response.usage)` called after every model response; `.exceeded` checked before continuing the loop.
- Pattern: Mutable accumulator object passed by reference through the loop; `.snapshot()` produces the dict embedded in `usage`/`resolution`/`error` events.

**ToolPolicy (`src/relay/guardrails.py`):**
- Purpose: Single boolean gate (`allow_writes`) deciding whether write-tier tools may execute; used to implement `dry_run=true` on `/process` and `RELAY_MCP_ALLOW_WRITES` for the MCP server.
- Examples: `ToolPolicy(allow_writes=not dry_run)` in `main.py`; `ToolPolicy(allow_writes=settings.mcp_allow_writes)` in `mcp_server.py`.
- Pattern: Checked once per tool call in `_execute_guarded()`, returning a model-readable denial reason string instead of raising.

## Entry Points

**HTTP API (`uvicorn relay.main:app`):**
- Location: `src/relay/main.py`
- Triggers: `uvicorn` process (see `Dockerfile` CMD, `scripts/demo.sh` usage comment).
- Responsibilities: Ticket CRUD, streaming agent execution, metrics dashboard, health check.

**MCP stdio server (`python -m relay.mcp_server`):**
- Location: `src/relay/mcp_server.py`
- Triggers: Invoked directly by an MCP client (Claude Desktop, Claude Code, IDE integration) over stdio.
- Responsibilities: Expose the tool registry (plus `create_ticket`/`list_open_tickets`) as MCP tools; runs its own DB connection independent of the HTTP process.

**Eval harness (`python -m relay.evals`):**
- Location: `src/relay/evals.py`
- Triggers: Manual/CI invocation (`--limit`, `--concurrency`, `--threshold`, `--output` flags).
- Responsibilities: Run the agent against `evals/golden.jsonl`, grade deterministically and via an LLM judge, write a JSON report to `eval_results/`, exit non-zero below the pass-rate threshold (CI gate).

## Architectural Constraints

- **Threading:** Single-process asyncio event loop (uvicorn). The one `sqlite3.Connection` is created with `check_same_thread=False` (`src/relay/db.py:62`) specifically to allow reuse across async request handlers on the same loop; there is no connection pool.
- **Global state:** `app.state.conn`, `app.state.registry`, and `app.state.client` are effectively process-wide singletons set once in FastAPI's `lifespan()` (`src/relay/main.py:19`). The `DASHBOARD_HTML` string is a module-level constant.
- **Circular imports:** None observed — dependency direction is consistently `main.py` → `agent.py` → `guardrails.py`/`tools.py`/`prompts.py`/`models.py`, with `mcp_server.py` and `evals.py` as parallel consumers of `tools.py`/`agent.py`.
- **Two independent runtimes:** The HTTP app and the MCP server each open their own SQLite connection (`main.py` lifespan vs. `mcp_server.amain()`); running both against the same `relay.db` file simultaneously relies on SQLite's own locking, not application-level coordination.
- **No conversation persistence:** Agent message history is not stored; only the final `AgentEvent` stream summary (via `record_run`) survives a run. Replaying/debugging a past run requires the logs, not the DB.

## Anti-Patterns

### Inline HTML/JS in a Python module

**What happens:** `DASHBOARD_HTML` is a ~30-line HTML/CSS/JS string literal embedded directly in `src/relay/main.py` (lines 112-138).
**Why it's wrong:** No syntax highlighting, templating, or testability for the dashboard; any UI growth will bloat the route module and make diffs noisy.
**Do this instead:** If the dashboard grows beyond a single polling table, move it to a template file (Jinja2) or a static asset served separately; keep `main.py` focused on routing.

### Broad exception swallowing at tool-execution boundary

**What happens:** `_execute_guarded()` in `src/relay/agent.py:54` catches bare `Exception` from any tool executor and serializes it into a model-visible error string (annotated `# noqa: BLE001` as intentional).
**Why it's wrong:** This is a deliberate, documented choice (surface errors to the model rather than crash the run) — not an anti-pattern here, but it does mean genuine programming bugs in tool executors (e.g., a `TypeError` from a bad `ToolSpec.execute` lambda signature) are silently reported to Claude as a generic tool error rather than surfaced to logs/alerts as a code defect.
**Do this instead:** If tool executor bugs become a debugging pain point, log the exception with traceback at `ERROR` before returning the sanitized message to the model (currently only the sanitized error reaches `logger.info("tool.executed", ...)`, not `logger.error` with `exc_info`).

## Error Handling

**Strategy:** Fail closed and structured. Anthropic API failures (`APIConnectionError`, `APIStatusError`) end the run cleanly with an `AgentEvent(type="error", ...)` rather than propagating a stack trace to the client (`src/relay/agent.py:127-139`). Tool execution errors are caught per-call and returned to the model as a `tool_result` with `is_error=True`, giving Claude a chance to recover or escalate.

**Patterns:**
- HTTP-level validation errors use FastAPI/Pydantic's default 422 behavior; domain errors (ticket not found, already processed) raise `HTTPException` explicitly (`main.py:71`, `main.py:151`).
- Agent-loop errors are categorized by reason string (`api_connection_error`, `api_error`, `model_refusal`, `budget_exceeded`, `ended_without_action`, `step_limit_reached`) and always terminate the generator via `return` after yielding the error event.
- The eval harness wraps each case in a broad `except Exception` (`evals.py:212`) so one bad test case cannot sink the whole suite run.

## Cross-Cutting Concerns

**Logging:** JSON-structured via `configure_logging()`/`JsonFormatter` (`src/relay/telemetry.py`); every log call passes contextual fields through `extra={"ctx": {...}}`. `uvicorn.access` is silenced to `WARNING` to avoid duplicate request logs.

**Validation:** Pydantic v2 everywhere — API request/response bodies (`models.py`), tool inputs (`guardrails.py`), and settings (`config.py` via `pydantic-settings`).

**Authentication:** None implemented. No auth middleware, API keys, or session handling on the HTTP API; the only credential in play is `ANTHROPIC_API_KEY` used server-side to call Claude.

---

*Architecture analysis: 2026-08-05*

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Relay — Remaster**

Relay is an AI support-triage agent built as a production service: it receives support tickets over a REST API, works each one autonomously with a hand-written agent loop on the Claude API (customer lookup, classification, grounded doc search, reply or escalation), and streams its reasoning as SSE. This milestone remasters the existing v1 into a portfolio showpiece: production-hardened, with real semantic retrieval and a polished live dashboard.

**Core Value:** A visitor hitting the live demo sees a credible, safe, observably-real AI agent service — impressive to read and watch, cheap to keep running.

### Constraints

- **Budget**: Live demo must stay cheap — single Fly machine, min_machines_running=0, per-run cost budget retained; Voyage usage is index-once + tiny per-query cost
- **Tech stack**: Python/FastAPI/SQLite/Claude API retained; no orchestration framework — the visible hand-written loop is a feature
- **Deploy**: One container, no build step, existing Fly.io + GitHub Actions pipeline keeps working
- **Compatibility**: SSE event contract and MCP tool surface stay backward compatible where practical; evals must keep passing
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- Python 3.11+ (requires-python `>=3.11` in `pyproject.toml`) - entire application (`src/relay/`)
- HTML/CSS/JS (inline, no build step) - `src/relay/main.py` (`DASHBOARD_HTML` constant, a single-page dashboard with vanilla `fetch`/`setInterval` polling)
- Markdown - knowledge base content in `kb/*.md`, consumed by keyword search tool
- Bash - `scripts/demo.sh` (demo/smoke script)
## Runtime
- CPython 3.11+ (project floor) / 3.12 (CI, Docker) / 3.14 (local venv)
- ASGI server: `uvicorn[standard]>=0.30` (`pyproject.toml`), started via `uvicorn relay.main:app` (Dockerfile CMD, README quick start)
- pip with `pyproject.toml` (PEP 621 style), build backend `hatchling`
- No lockfile present (no `requirements.txt`, `poetry.lock`, or `uv.lock`) — dependency versions are floor-pinned only (`>=`)
## Frameworks
- FastAPI `>=0.115` (`pyproject.toml`) - REST API + SSE streaming, defined in `src/relay/main.py`
- Pydantic `[email]>=2.7` - request/response models (`src/relay/models.py`) and tool-input validation (`src/relay/guardrails.py`)
- Pydantic Settings `>=2.3` - typed environment config (`src/relay/config.py`)
- Anthropic Python SDK `>=0.60` (`anthropic`, `AsyncAnthropic`) - drives the hand-written agent loop against the Claude API (`src/relay/agent.py`)
- MCP SDK `>=1.2` (`mcp`) - exposes the tool registry over the Model Context Protocol via stdio (`src/relay/mcp_server.py`)
- pytest `>=8.0` + `pytest-asyncio>=0.23` (`asyncio_mode = "auto"` in `pyproject.toml`) - test suite in `tests/`
- httpx `>=0.27` - used for FastAPI's `TestClient`/async HTTP testing
- ruff `>=0.5` - linting, `line-length = 100`, configured for `src` and `tests` (`pyproject.toml` `[tool.ruff]`)
- hatchling - wheel build backend (`[build-system]`, packages = `["src/relay"]`)
## Key Dependencies
- `anthropic>=0.60` - all model calls (chat completion + tool use); also used directly for LLM-as-judge grading in `src/relay/evals.py`
- `fastapi>=0.115` + `uvicorn[standard]>=0.30` - HTTP service layer and streaming (`StreamingResponse`/SSE) in `src/relay/main.py`
- `mcp>=1.2` - alternate transport for the same tool registry (`src/relay/mcp_server.py`)
- `opentelemetry-sdk>=1.25` + `opentelemetry-exporter-otlp-proto-http>=1.25` - tracing setup in `src/relay/telemetry.py`; spans are a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set
- Python stdlib `sqlite3` - only persistence layer, wired in `src/relay/db.py` (no ORM/driver dependency)
- `pydantic-settings>=2.3` - `.env` file + `RELAY_`-prefixed environment variable loading
## Configuration
- Loaded via `pydantic_settings.BaseSettings` in `src/relay/config.py`, `env_prefix="RELAY_"`, `env_file=".env"`, `extra="ignore"`
- `ANTHROPIC_API_KEY` is read without the `RELAY_` prefix (`validation_alias="ANTHROPIC_API_KEY"`) so it matches the Anthropic SDK's own lookup convention
- Key settings (all overridable via env, defaults shown):
- Template: `.env.example` (copy to `.env`, never commit — enforced by `.gitignore`)
- `.env` file exists locally (contents not read; presence noted only per security policy)
- `pyproject.toml` - single source of truth for dependencies, build backend, pytest config, and ruff config
- `Dockerfile` - multi-stage-free single-stage build (`python:3.12-slim`), installs package via `pip install .`, copies `kb/`, sets `RELAY_DB_PATH=/data/relay.db` and `PORT=8000`, includes a container `HEALTHCHECK` hitting `/health`
- `fly.toml` - Fly.io app config (`app = 'relay-agent'`, region `syd`), mounts a persistent volume `relay_data` at `/data`, `PORT=8080` to match `internal_port`
## Platform Requirements
- Python 3.11+, `pip install -e ".[dev]"` (README Quick start)
- `.env` with `ANTHROPIC_API_KEY` required to run the agent loop
- SQLite file-based DB auto-created/seeded on startup (`init_db` in `src/relay/db.py`) — no external DB server needed locally
- Deployed to Fly.io (`fly.toml`, app `relay-agent`, live at `https://relay-agent.fly.dev` per `README.md`)
- Container: `shared-cpu-1x` VM, 512MB memory, auto-stop/auto-start machines, `min_machines_running = 0` (scale-to-zero)
- Persistent volume `relay_data` mounted at `/data` holds the SQLite file (`RELAY_DB_PATH=/data/relay.db`) — single-writer, single-instance deployment model (no external Postgres yet; `src/relay/db.py` docstring notes "Postgres comes with deployment (phase 6)" but SQLite is still what's wired up)
- `PORT` env var honored at runtime so the same image works unchanged on Fly/Render/Railway/Cloud Run (Dockerfile comment)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

## Naming Patterns
- Lowercase snake_case module names, one concern per module: `src/relay/agent.py`, `src/relay/guardrails.py`, `src/relay/tools.py`, `src/relay/telemetry.py`, `src/relay/mcp_server.py`
- No `utils.py`/`helpers.py` grab-bag in `src/` — shared code lives in the module that owns the concept (e.g. Pydantic input models live in `src/relay/guardrails.py` next to the validation function that uses them, not in a generic `models.py`)
- Test helper module is explicitly named `tests/helpers.py` (test doubles only, imported by name from tests)
- snake_case, verb-first for actions: `validate_tool_input`, `build_registry`, `run_ticket`, `record_run`, `run_metrics`
- Private/internal helpers prefixed with a single underscore: `_execute_guarded` (`src/relay/agent.py:40`), `_percentile` (`src/relay/telemetry.py:76`), `_get_ticket` (`src/relay/main.py:146`)
- Test functions are `test_<behavior_described_in_words>`, e.g. `test_dry_run_never_writes_to_db`, `test_normal_run_ending_without_action_is_error` — the name states the expected outcome, not just the function under test
- snake_case throughout; no Hungarian notation
- Short-lived loop/temp names are terse (`cur`, `row`, `conn`, `exc`), domain variables are descriptive (`resolved_via`, `last_stop_reason`, `tool_results`)
- Pydantic `BaseModel` subclasses in PascalCase suffixed by role: `*Input` for tool argument schemas (`SendReplyInput`, `LookupCustomerInput` in `src/relay/guardrails.py`), plain nouns for domain records (`Ticket`, `AgentEvent` in `src/relay/models.py`)
- `Enum` subclasses use lowercase string values matching the wire format: `TicketCategory.billing = "billing"` (`src/relay/models.py:8`)
- Dataclasses used for lightweight, non-validated structs: `@dataclass(frozen=True) class ToolSpec` (`src/relay/tools.py:26`), `@dataclass class ToolPolicy` (`src/relay/guardrails.py:55`)
## Code Style
- No formatter config found (no `.prettierrc`/`black` config); `ruff` is the sole tool for both lint and style enforcement
- Line length capped at 100 (`[tool.ruff] line-length = 100` in `pyproject.toml:40`)
- Trailing commas used consistently in multi-line literals and call args
- `ruff check src tests` run in CI (`.github/workflows/ci.yml`)
- No custom `select`/`ignore` rule set configured beyond defaults — relies on ruff's default rule set plus `line-length`
- Inline `# noqa` used sparingly and always justified with a comment, e.g. `except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed` (`src/relay/agent.py:54`)
## Import Organization
- None. All intra-package imports are relative (`from .guardrails import ...`), and tests import the installed package as `relay.*` (`from relay.agent import run_ticket`) since the project is installed in editable mode (`pip install -e ".[dev]"`).
## Error Handling
- Domain-specific exceptions instead of bare exceptions: `ToolInputError` (`src/relay/guardrails.py:51`) raised from Pydantic `ValidationError` with a message built for the model to read: `f"invalid tool input — {problems}"`.
- Tool execution never raises into the agent loop — `_execute_guarded` (`src/relay/agent.py:40`) catches `Exception` broadly at the tool boundary only, converting it to a `{"error": ...}` JSON string returned to the model. This is the single sanctioned broad-except in the codebase, explicitly commented.
- API failures from the Anthropic SDK are caught narrowly by type (`anthropic.APIConnectionError`, `anthropic.APIStatusError`) and turned into structured `AgentEvent(type="error", ...)` — never allowed to raise a stack trace to the caller (`src/relay/agent.py:127-139`).
- FastAPI routes raise `HTTPException` with explicit status codes and short messages, no custom exception handler middleware: `raise HTTPException(404, "ticket not found")` (`src/relay/main.py:151`), `raise HTTPException(409, f"ticket is already {ticket.status.value}")` (`src/relay/main.py:72`).
- MCP tool boundary (`src/relay/mcp_server.py`) converts policy denials and validation errors into `RuntimeError` with a matching message, asserted via `pytest.raises(RuntimeError, match="...")` in tests.
- No bare `except:` anywhere in the codebase; every catch names its exception type(s).
## Logging
- Logger acquired per-module with a dotted name matching the module's role: `logger = logging.getLogger("relay.agent")` (`src/relay/agent.py:36`).
- Log messages are short, dotted event names in past/imperative tense used as the `event` field, not free-form sentences: `"run.start"`, `"model.response"`, `"tool.executed"`, `"run.end"`, `"run.budget_exceeded"`.
- Structured context is always passed via `extra={"ctx": {...}}`, never string-interpolated into the message: `logger.info("tool.executed", extra={"ctx": {"ticket_id": ticket["id"], "tool": block.name, "is_error": is_error}})` (`src/relay/agent.py:165`).
- `uvicorn.access` logger is explicitly quieted to `WARNING` to avoid duplicating request context already captured by the app's own structured logs (`src/relay/telemetry.py:44`).
## Comments
- Module-level docstrings explain *why*, referencing the project phase and design rationale, not just *what*: see the top of `src/relay/agent.py` ("No framework: the loop is a plain request -> tool-execute -> append cycle so the control flow, guardrails, and event stream are fully visible and testable.") and `src/relay/telemetry.py`.
- Inline comments justify non-obvious choices at the exact line they apply to, e.g. explaining why a span is parented explicitly instead of made "current" in a generator (`src/relay/agent.py:83-85`), or why cache-read/write tokens are priced with multipliers (`src/relay/guardrails.py:74-77`).
- No comments restating what the code already says.
## Function Design
- Keyword-only parameters used for functions with more than 2-3 args, enforced with `*`: `record_run(conn, *, ticket_id, model, duration_ms, ...)` (`src/relay/telemetry.py:56`).
- Optional collaborators default to `None` and are constructed inline: `policy: ToolPolicy | None = None` then `policy = policy or ToolPolicy()` (`src/relay/agent.py:62,66`) — same pattern for `budget`.
- Type hints are mandatory on all function signatures, using modern `X | None` union syntax (Python 3.11+) rather than `Optional[X]`.
- Tool executor functions return JSON-encoded strings (`json.dumps(...)`), not dicts, because that's the wire format the model consumes: see every function in `src/relay/tools.py`.
- Functions that can fail without throwing return a `(result, is_error)` tuple rather than raising, when the failure is an expected, model-facing outcome: `_execute_guarded(...) -> tuple[str, bool]` (`src/relay/agent.py:40`).
- Async generators (`AsyncIterator[AgentEvent]`) are used for streaming multi-step results back to callers, e.g. `run_ticket` yields one `AgentEvent` per step rather than returning a final list.
## Module Design
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## System Overview
```text
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
- No agent framework (LangChain/etc.) — `run_ticket()` in `src/relay/agent.py` is an explicit while/for loop against the raw Anthropic Messages API, chosen deliberately so control flow, guardrails, and the event stream are fully visible and testable (see module docstring).
- Tool definitions are colocated with their executors as `ToolSpec` dataclasses (`src/relay/tools.py`), each carrying a Claude JSON schema, a permission tier (`"read"`/`"write"`), a Pydantic input model, and an `execute` closure bound to the live `sqlite3.Connection` and `kb_dir`.
- Guardrails are enforced in code, not just prompted: input validation happens before every tool call, write-tier tools are gated by `ToolPolicy`, and runs are hard-capped by `RunBudget` on cumulative dollar cost (`src/relay/guardrails.py`).
- Everything (HTTP API and MCP server) shares one tool registry built by `build_registry()` (`src/relay/tools.py`), so behavior does not diverge between entry points; `mcp_server.py` extends it with two ticket-lifecycle-only tools.
- The system is single-process, single-connection: one `sqlite3.Connection` is created at FastAPI startup and stored in `app.state.conn`, reused by every request (see `lifespan()` in `src/relay/main.py`).
## Layers
- Purpose: Accept ticket CRUD requests, stream agent runs as Server-Sent Events, serve a metrics dashboard.
- Location: `src/relay/main.py`
- Contains: FastAPI route handlers, SSE event formatting, a self-contained HTML/JS dashboard string.
- Depends on: agent loop (`agent.run_ticket`), guardrails (`ToolPolicy`), db (`connect`, `init_db`), telemetry (`record_run`, `run_metrics`), tools (`build_registry`).
- Used by: External clients (curl, browser dashboard, `scripts/demo.sh`).
- Purpose: Own the full lifecycle of one ticket run against Claude — request, tool dispatch, budget/step enforcement, tracing, and terminal-action detection.
- Location: `src/relay/agent.py`
- Contains: `run_ticket()` async generator, `_execute_guarded()` per-tool-call guard chain, OTel span management.
- Depends on: guardrails, models (`AgentEvent`), prompts, tools (`ToolSpec`), config (`settings`).
- Used by: `main.py` (HTTP), `evals.py` (eval harness).
- Purpose: Enforce safety/cost constraints independent of the model's behavior.
- Location: `src/relay/guardrails.py`
- Contains: Pydantic input models per tool, `validate_tool_input()`, `ToolPolicy` (write-tier gating), `RunBudget` (token/cost accounting with cache-pricing multipliers).
- Depends on: models (`TicketCategory`).
- Used by: agent loop, mcp_server.
- Purpose: Define what the agent/MCP client can do, and how each action executes against storage/kb.
- Location: `src/relay/tools.py`
- Contains: `ToolSpec` dataclass, tool executor functions (`lookup_customer`, `search_docs`, `create_escalation`, `send_reply`, `set_category`), `build_registry()` factory.
- Depends on: guardrails (input models), sqlite3, kb directory on disk.
- Used by: agent loop, mcp_server, evals.
- Purpose: Own the SQLite schema and connection lifecycle.
- Location: `src/relay/db.py`
- Contains: `SCHEMA` DDL string, `SEED_CUSTOMERS`, `connect()`, `init_db()`.
- Depends on: nothing internal (stdlib `sqlite3` only).
- Used by: main, tools, mcp_server, evals.
- Purpose: Structured logging, distributed tracing, and run-metrics aggregation.
- Location: `src/relay/telemetry.py`
- Contains: `JsonFormatter`, `configure_logging()`, `setup_tracing()`, `record_run()`, `run_metrics()`, `_percentile()`.
- Depends on: OpenTelemetry SDK, sqlite3 (`runs` table).
- Used by: main (lifespan + `/metrics`), agent (tracer only, via `opentelemetry.trace`).
- Purpose: Alternate entry point exposing the same tool registry (plus ticket-lifecycle tools) over the Model Context Protocol for external MCP clients (Claude Desktop, Claude Code, IDEs).
- Location: `src/relay/mcp_server.py`
- Contains: `build_mcp_registry()`, `list_mcp_tools()`, `call_mcp_tool()`, `create_server()`, stdio `amain()`/`main()` entry points.
- Depends on: agent (`_execute_guarded`), config, db, guardrails, tools.
- Used by: External MCP clients only; run standalone via `python -m relay.mcp_server`.
## Data Flow
### Primary Request Path (process a ticket via HTTP)
### Metrics / Dashboard Flow
- All persistent state lives in one SQLite file (`relay.db` by default, configurable via `RELAY_DB_PATH`/`settings.db_path`), opened once at startup and shared across requests via `app.state.conn` (`check_same_thread=False`).
- No in-memory session/conversation state persists between HTTP requests — each `/process` call re-derives the full message history within a single `run_ticket()` invocation; nothing is cached between ticket runs.
- Agent run-in-progress state (message list, budget accumulator) is local to the `run_ticket()` generator's stack frame and is discarded when the generator completes.
## Key Abstractions
- Purpose: Bundle a Claude-facing JSON tool schema with its permission tier, Pydantic validator, and executor in one frozen dataclass, so the agent loop and MCP server can treat all tools uniformly.
- Examples: `build_registry()` returns `dict[str, ToolSpec]` for `lookup_customer`, `search_docs`, `set_category`, `send_reply`, `create_escalation`.
- Pattern: Executors are closures capturing the live `sqlite3.Connection`/`kb_dir` at registry-build time (not passed per-call), so tool signatures match exactly what Claude sends.
- Purpose: Uniform envelope (`type` + `data`) for every observable step of an agent run, streamed as SSE and also consumed synchronously by the eval harness.
- Examples: `type="text"`, `"tool_use"`, `"tool_result"`, `"usage"`, `"resolution"`, `"error"`.
- Pattern: Producer (`agent.run_ticket`) is decoupled from consumers (`main.py` SSE formatter, `evals.py` `extract_outcome()`), both of which just filter on `event.type`.
- Purpose: Accumulate token usage/cost across a multi-step run and enforce a hard dollar ceiling (`settings.max_run_cost_usd`), including Anthropic prompt-cache pricing multipliers.
- Examples: instantiated once per `run_ticket()` call; `.add(response.usage)` called after every model response; `.exceeded` checked before continuing the loop.
- Pattern: Mutable accumulator object passed by reference through the loop; `.snapshot()` produces the dict embedded in `usage`/`resolution`/`error` events.
- Purpose: Single boolean gate (`allow_writes`) deciding whether write-tier tools may execute; used to implement `dry_run=true` on `/process` and `RELAY_MCP_ALLOW_WRITES` for the MCP server.
- Examples: `ToolPolicy(allow_writes=not dry_run)` in `main.py`; `ToolPolicy(allow_writes=settings.mcp_allow_writes)` in `mcp_server.py`.
- Pattern: Checked once per tool call in `_execute_guarded()`, returning a model-readable denial reason string instead of raising.
## Entry Points
- Location: `src/relay/main.py`
- Triggers: `uvicorn` process (see `Dockerfile` CMD, `scripts/demo.sh` usage comment).
- Responsibilities: Ticket CRUD, streaming agent execution, metrics dashboard, health check.
- Location: `src/relay/mcp_server.py`
- Triggers: Invoked directly by an MCP client (Claude Desktop, Claude Code, IDE integration) over stdio.
- Responsibilities: Expose the tool registry (plus `create_ticket`/`list_open_tickets`) as MCP tools; runs its own DB connection independent of the HTTP process.
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
### Broad exception swallowing at tool-execution boundary
## Error Handling
- HTTP-level validation errors use FastAPI/Pydantic's default 422 behavior; domain errors (ticket not found, already processed) raise `HTTPException` explicitly (`main.py:71`, `main.py:151`).
- Agent-loop errors are categorized by reason string (`api_connection_error`, `api_error`, `model_refusal`, `budget_exceeded`, `ended_without_action`, `step_limit_reached`) and always terminate the generator via `return` after yielding the error event.
- The eval harness wraps each case in a broad `except Exception` (`evals.py:212`) so one bad test case cannot sink the whole suite run.
## Cross-Cutting Concerns
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->

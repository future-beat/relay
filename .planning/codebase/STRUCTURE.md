# Codebase Structure

**Analysis Date:** 2026-08-05

## Directory Layout

```
Relay/
├── src/
│   └── relay/              # The installable package (single flat module layer)
│       ├── __init__.py     # __version__ only
│       ├── main.py         # FastAPI app, routes, SSE streaming, dashboard
│       ├── agent.py        # Hand-rolled agent loop (run_ticket)
│       ├── guardrails.py   # Tool input validation, ToolPolicy, RunBudget
│       ├── tools.py        # ToolSpec definitions + executors + build_registry()
│       ├── mcp_server.py   # MCP stdio server (alternate entry point)
│       ├── models.py       # Pydantic domain/API models + enums
│       ├── db.py           # SQLite schema, connect(), init_db(), seed data
│       ├── config.py       # pydantic-settings Settings (env: RELAY_*)
│       ├── prompts.py      # SYSTEM_PROMPT + ticket_prompt()
│       ├── telemetry.py    # JSON logging, OTel tracing, run metrics
│       └── evals.py        # Eval harness (python -m relay.evals)
├── tests/                  # pytest suite, mirrors src/relay module names
│   ├── conftest.py         # Shared fixtures
│   ├── helpers.py          # Test helper functions
│   ├── test_api.py         # Tests main.py HTTP routes
│   ├── test_evals.py       # Tests evals.py
│   ├── test_guardrails.py  # Tests guardrails.py
│   ├── test_mcp.py         # Tests mcp_server.py
│   ├── test_observability.py # Tests telemetry.py
│   └── test_tools.py       # Tests tools.py
├── kb/                      # Markdown knowledge base searched by search_docs tool
│   ├── account.md
│   ├── api.md
│   └── billing.md
├── evals/
│   └── golden.jsonl        # Golden eval dataset (one JSON object per line)
├── eval_results/            # Generated eval report JSON artifacts (git-tracked here)
├── scripts/
│   └── demo.sh              # curl-based end-to-end demo against a running server
├── docs/
│   └── PROJECT_BRIEF.md     # Product/project brief
├── .github/workflows/       # CI pipeline definitions
├── relay.db                  # Default SQLite database file (dev/local)
├── fly.toml                  # Fly.io deployment config
├── Dockerfile                 # Container build (pip install ., uvicorn CMD)
├── pyproject.toml             # Package metadata, deps, pytest/ruff config
└── README.md
```

## Directory Purposes

**`src/relay/`:**
- Purpose: The entire application. Not further subdivided into layers (no `api/`, `services/`, `repositories/` subpackages) — each concern is one flat module.
- Contains: FastAPI routes, agent loop, guardrails, tool definitions, SQLite access, config, prompts, telemetry, MCP server, eval harness.
- Key files: `main.py` (HTTP entry), `agent.py` (core loop), `tools.py` (capability surface), `mcp_server.py` (alternate entry).

**`tests/`:**
- Purpose: pytest suite; one test file roughly per `src/relay/*.py` module (excluding `config.py`, `models.py`, `prompts.py`, `db.py`, `agent.py`, which are exercised indirectly through `test_api.py`/`test_tools.py`/`test_guardrails.py`).
- Contains: `test_*.py` files, `conftest.py` for shared fixtures, `helpers.py` for shared assertions/utilities.
- Key files: `conftest.py`, `helpers.py`.

**`kb/`:**
- Purpose: Runtime knowledge base — plain markdown files read and keyword-scored by `search_docs()` in `src/relay/tools.py`. Not documentation about the codebase; this is product-facing content the agent grounds its answers in.
- Contains: `account.md`, `api.md`, `billing.md`.
- Generated: No. Committed: Yes.

**`evals/`:**
- Purpose: Input fixtures for the eval harness.
- Contains: `golden.jsonl` — one support-ticket test case per line with `expected_action`/`expected_categories`.
- Generated: No. Committed: Yes.

**`eval_results/`:**
- Purpose: Output artifacts from running `python -m relay.evals`.
- Contains: Timestamped JSON reports (`eval-YYYYMMDDTHHMMSSZ.json`).
- Generated: Yes (by `evals.py main()`). Committed: Yes (currently checked into the repo).

**`scripts/`:**
- Purpose: Operator-facing shell scripts, not part of the installable package.
- Contains: `demo.sh` (curl-driven end-to-end ticket demo against a running server).

**`docs/`:**
- Purpose: Human-readable project documentation (not API docs, not this codebase map).
- Contains: `PROJECT_BRIEF.md`.

**`.github/workflows/`:**
- Purpose: CI pipeline definitions (GitHub Actions).

## Key File Locations

**Entry Points:**
- `src/relay/main.py`: FastAPI app (`uvicorn relay.main:app`) — primary HTTP service.
- `src/relay/mcp_server.py`: MCP stdio server (`python -m relay.mcp_server`).
- `src/relay/evals.py`: Eval harness CLI (`python -m relay.evals`).

**Configuration:**
- `src/relay/config.py`: `Settings` (pydantic-settings), env prefix `RELAY_`, loads `.env`.
- `.env` / `.env.example`: Local environment variables (never read/quote `.env` contents — see forbidden files policy).
- `pyproject.toml`: Dependencies, build backend (hatchling), pytest config (`testpaths = ["tests"]`, `asyncio_mode = "auto"`), ruff config (`line-length = 100`).
- `fly.toml`: Fly.io deployment (region, mounts, internal port).
- `Dockerfile`: Container build; installs package via `pip install .`, copies `kb/` in, sets `RELAY_DB_PATH=/data/relay.db`.

**Core Logic:**
- `src/relay/agent.py`: The agent loop (`run_ticket`) — the heart of the system.
- `src/relay/tools.py`: All agent-callable capabilities (`build_registry`).
- `src/relay/guardrails.py`: Safety/cost enforcement (`ToolPolicy`, `RunBudget`, input validators).
- `src/relay/db.py`: SQLite schema (`SCHEMA`) — source of truth for the data model.

**Testing:**
- `tests/conftest.py`: Shared pytest fixtures (likely an in-memory DB + registry setup — read before adding new tests).
- `tests/helpers.py`: Shared test utility functions.
- Test files map 1:1 to the module under test by name (`test_api.py` ↔ `main.py`, `test_tools.py` ↔ `tools.py`, etc.).

## Naming Conventions

**Files:**
- Lowercase snake_case module names matching their primary responsibility noun (`agent.py`, `guardrails.py`, `tools.py`, `telemetry.py`).
- Test files: `test_<module>.py`, mirroring the `src/relay/<module>.py` they exercise.

**Directories:**
- Lowercase, no separators (`kb`, `evals`, `eval_results`, `scripts`, `docs`).
- No nested subpackages inside `src/relay/` — the package is intentionally flat (11 modules, no sub-directories).

**Python identifiers:**
- Functions/variables: snake_case (`lookup_customer`, `run_ticket`, `build_registry`).
- Classes/Pydantic models: PascalCase (`ToolSpec`, `AgentEvent`, `TicketCategory`, `RunBudget`).
- Constants: UPPER_SNAKE_CASE (`SYSTEM_PROMPT`, `TERMINAL_TOOLS`, `SCHEMA`, `SEED_CUSTOMERS`, `JUDGE_SCHEMA`, `GOLDEN_PATH`).
- Tool schema names (Claude-facing, dict keys in registries): snake_case matching the executor function name (`lookup_customer`, `search_docs`, `set_category`, `send_reply`, `create_escalation`, `create_ticket`, `list_open_tickets`).

## Where to Add New Code

**New agent tool:**
- Add a Pydantic input model to `src/relay/guardrails.py`.
- Add the executor function and a `ToolSpec` entry to `build_registry()` in `src/relay/tools.py` (set `tier="read"` or `tier="write"` correctly — write tools are gated by `ToolPolicy`).
- If the tool should end a ticket, add its name to `TERMINAL_TOOLS` in `src/relay/agent.py`.
- Add/extend tests in `tests/test_tools.py` and `tests/test_guardrails.py`.

**New HTTP endpoint:**
- Add the route directly to `src/relay/main.py` (no separate router modules currently exist — keep consistent with the flat-module pattern unless the file grows unwieldy).
- Add tests to `tests/test_api.py`.

**New MCP-only tool (ticket lifecycle, not part of the agent loop):**
- Add it inside `build_mcp_registry()` in `src/relay/mcp_server.py`, following the `create_ticket`/`list_open_tickets` pattern.
- Add tests to `tests/test_mcp.py`.

**New database table/column:**
- Extend the `SCHEMA` DDL string in `src/relay/db.py` (uses `CREATE TABLE IF NOT EXISTS`, so migrations are additive-only today — no migration framework is present).

**New knowledge-base content:**
- Add a `.md` file to `kb/`; `search_docs()` in `tools.py` picks up all `kb_dir.glob("*.md")` automatically.

**New eval case:**
- Append a JSON line to `evals/golden.jsonl` with `id`, `customer_email`, `subject`, `body`, `expected_action`, `expected_categories`.

**Shared utilities:**
- No dedicated `utils.py` exists; small helpers currently live inline in the module that uses them (e.g., `_percentile()` in `telemetry.py`, `_get_ticket()` in `main.py`). Follow this pattern for single-use helpers; only extract a shared module if a helper is needed by 2+ modules.

## Special Directories

**`eval_results/`:**
- Purpose: Timestamped JSON output from eval runs.
- Generated: Yes.
- Committed: Yes (currently tracked in git; consider `.gitignore` if this grows unbounded).

**`.venv/`:**
- Purpose: Local Python virtual environment.
- Generated: Yes. Committed: No (excluded via `.gitignore`).

**`.pytest_cache/`, `.ruff_cache/`:**
- Purpose: Tool caches for pytest and ruff.
- Generated: Yes. Committed: No.

**`relay.db`:**
- Purpose: Default local SQLite database file (dev/demo data), created by `db.connect()`/`init_db()` on first run.
- Generated: Yes (at runtime). Committed: Yes currently at repo root (production overrides via `RELAY_DB_PATH=/data/relay.db` per `Dockerfile`/`fly.toml`).

---

*Structure analysis: 2026-08-05*

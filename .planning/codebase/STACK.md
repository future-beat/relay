# Technology Stack

**Analysis Date:** 2026-08-05

## Languages

**Primary:**
- Python 3.11+ (requires-python `>=3.11` in `pyproject.toml`) - entire application (`src/relay/`)
  - CI runs Python 3.12 (`.github/workflows/ci.yml`, `.github/workflows/evals.yml`)
  - Docker image uses `python:3.12-slim` (`Dockerfile`)
  - Local `.venv` is built against Python 3.14.6 (`.venv/pyvenv.cfg`) — newer than the floor the project declares, worth noting as a potential dev/CI version drift

**Secondary:**
- HTML/CSS/JS (inline, no build step) - `src/relay/main.py` (`DASHBOARD_HTML` constant, a single-page dashboard with vanilla `fetch`/`setInterval` polling)
- Markdown - knowledge base content in `kb/*.md`, consumed by keyword search tool
- Bash - `scripts/demo.sh` (demo/smoke script)

## Runtime

**Environment:**
- CPython 3.11+ (project floor) / 3.12 (CI, Docker) / 3.14 (local venv)
- ASGI server: `uvicorn[standard]>=0.30` (`pyproject.toml`), started via `uvicorn relay.main:app` (Dockerfile CMD, README quick start)

**Package Manager:**
- pip with `pyproject.toml` (PEP 621 style), build backend `hatchling`
- No lockfile present (no `requirements.txt`, `poetry.lock`, or `uv.lock`) — dependency versions are floor-pinned only (`>=`)

## Frameworks

**Core:**
- FastAPI `>=0.115` (`pyproject.toml`) - REST API + SSE streaming, defined in `src/relay/main.py`
- Pydantic `[email]>=2.7` - request/response models (`src/relay/models.py`) and tool-input validation (`src/relay/guardrails.py`)
- Pydantic Settings `>=2.3` - typed environment config (`src/relay/config.py`)
- Anthropic Python SDK `>=0.60` (`anthropic`, `AsyncAnthropic`) - drives the hand-written agent loop against the Claude API (`src/relay/agent.py`)
- MCP SDK `>=1.2` (`mcp`) - exposes the tool registry over the Model Context Protocol via stdio (`src/relay/mcp_server.py`)

**Testing:**
- pytest `>=8.0` + `pytest-asyncio>=0.23` (`asyncio_mode = "auto"` in `pyproject.toml`) - test suite in `tests/`
- httpx `>=0.27` - used for FastAPI's `TestClient`/async HTTP testing

**Build/Dev:**
- ruff `>=0.5` - linting, `line-length = 100`, configured for `src` and `tests` (`pyproject.toml` `[tool.ruff]`)
- hatchling - wheel build backend (`[build-system]`, packages = `["src/relay"]`)

## Key Dependencies

**Critical:**
- `anthropic>=0.60` - all model calls (chat completion + tool use); also used directly for LLM-as-judge grading in `src/relay/evals.py`
- `fastapi>=0.115` + `uvicorn[standard]>=0.30` - HTTP service layer and streaming (`StreamingResponse`/SSE) in `src/relay/main.py`
- `mcp>=1.2` - alternate transport for the same tool registry (`src/relay/mcp_server.py`)

**Infrastructure:**
- `opentelemetry-sdk>=1.25` + `opentelemetry-exporter-otlp-proto-http>=1.25` - tracing setup in `src/relay/telemetry.py`; spans are a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set
- Python stdlib `sqlite3` - only persistence layer, wired in `src/relay/db.py` (no ORM/driver dependency)
- `pydantic-settings>=2.3` - `.env` file + `RELAY_`-prefixed environment variable loading

## Configuration

**Environment:**
- Loaded via `pydantic_settings.BaseSettings` in `src/relay/config.py`, `env_prefix="RELAY_"`, `env_file=".env"`, `extra="ignore"`
- `ANTHROPIC_API_KEY` is read without the `RELAY_` prefix (`validation_alias="ANTHROPIC_API_KEY"`) so it matches the Anthropic SDK's own lookup convention
- Key settings (all overridable via env, defaults shown):
  - `RELAY_MODEL` (default `claude-sonnet-5`)
  - `RELAY_DB_PATH` (default `relay.db`)
  - `RELAY_MAX_AGENT_STEPS` (default `10`)
  - `RELAY_MAX_TOKENS` (default `16000`, not in `.env.example` but present in `config.py`)
  - `RELAY_MAX_RUN_COST_USD` (default `0.50`) - hard per-run budget ceiling
  - `RELAY_PRICE_IN_PER_MTOK` / `RELAY_PRICE_OUT_PER_MTOK` (defaults `3.0` / `15.0`) - Claude Sonnet 5 per-MTok pricing used for cost tracking
  - `RELAY_MCP_ALLOW_WRITES` (default `true`) - gates write-tier tools on the MCP surface
  - `OTEL_EXPORTER_OTLP_ENDPOINT` (unprefixed, optional) - enables OTLP trace export when set
- Template: `.env.example` (copy to `.env`, never commit — enforced by `.gitignore`)
- `.env` file exists locally (contents not read; presence noted only per security policy)

**Build:**
- `pyproject.toml` - single source of truth for dependencies, build backend, pytest config, and ruff config
- `Dockerfile` - multi-stage-free single-stage build (`python:3.12-slim`), installs package via `pip install .`, copies `kb/`, sets `RELAY_DB_PATH=/data/relay.db` and `PORT=8000`, includes a container `HEALTHCHECK` hitting `/health`
- `fly.toml` - Fly.io app config (`app = 'relay-agent'`, region `syd`), mounts a persistent volume `relay_data` at `/data`, `PORT=8080` to match `internal_port`

## Platform Requirements

**Development:**
- Python 3.11+, `pip install -e ".[dev]"` (README Quick start)
- `.env` with `ANTHROPIC_API_KEY` required to run the agent loop
- SQLite file-based DB auto-created/seeded on startup (`init_db` in `src/relay/db.py`) — no external DB server needed locally

**Production:**
- Deployed to Fly.io (`fly.toml`, app `relay-agent`, live at `https://relay-agent.fly.dev` per `README.md`)
- Container: `shared-cpu-1x` VM, 512MB memory, auto-stop/auto-start machines, `min_machines_running = 0` (scale-to-zero)
- Persistent volume `relay_data` mounted at `/data` holds the SQLite file (`RELAY_DB_PATH=/data/relay.db`) — single-writer, single-instance deployment model (no external Postgres yet; `src/relay/db.py` docstring notes "Postgres comes with deployment (phase 6)" but SQLite is still what's wired up)
- `PORT` env var honored at runtime so the same image works unchanged on Fly/Render/Railway/Cloud Run (Dockerfile comment)

---

*Stack analysis: 2026-08-05*

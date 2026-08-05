# External Integrations

**Analysis Date:** 2026-08-05

## APIs & External Services

**AI / LLM:**
- Anthropic Claude API - the sole external network dependency. Used for two distinct purposes:
  - Agent loop model calls: `src/relay/agent.py` (`client.messages.create(...)`, tool-use loop, prompt caching via `cache_control: {"type": "ephemeral"}` on the system prompt)
  - Eval grading (LLM-as-judge): `src/relay/evals.py` (`AsyncAnthropic` client instantiated directly, `model=settings.model`)
  - SDK/Client: `anthropic` / `AsyncAnthropic` (`anthropic>=0.60`)
  - Auth: `ANTHROPIC_API_KEY` env var (read via `validation_alias` in `src/relay/config.py`, no `RELAY_` prefix)
  - Model name is itself configurable: `RELAY_MODEL` (default `claude-sonnet-5`)
  - Error handling: `anthropic.APIConnectionError` and `anthropic.APIStatusError` are caught explicitly in `src/relay/agent.py` and surfaced as structured `error` SSE events rather than propagating exceptions; transient 429/5xx retries are handled inside the SDK itself (per code comment)

**Observability export:**
- OpenTelemetry Protocol (OTLP) over HTTP - `opentelemetry-exporter-otlp-proto-http`, wired in `src/relay/telemetry.py::setup_tracing()`
  - Only activates when `OTEL_EXPORTER_OTLP_ENDPOINT` env var is set; otherwise tracing is a local no-op `TracerProvider`
  - No specific vendor is hardcoded (works with any OTLP-compatible collector — e.g. Honeycomb, Grafana Tempo, local collector)

## Data Storage

**Databases:**
- SQLite (Python stdlib `sqlite3`, no external driver) - `src/relay/db.py`
  - Connection: `RELAY_DB_PATH` env var (default `relay.db` locally, `/data/relay.db` in the Docker/Fly deployment)
  - Client: raw `sqlite3.connect()` with `row_factory = sqlite3.Row`, `PRAGMA foreign_keys = ON`; no ORM
  - Schema (`SCHEMA` constant in `db.py`) defines 5 tables: `customers`, `tickets`, `escalations`, `runs`, `replies`
  - Seed data: 4 fictional customers inserted on first run if `customers` table is empty (`SEED_CUSTOMERS`)
  - Single-writer model: `check_same_thread=False` used to share one connection across FastAPI's async handlers (stored on `app.state.conn`)
  - Production persistence: Fly.io volume mount `relay_data` → `/data` (`fly.toml`), so DB survives container restarts but does not scale beyond a single machine (`min_machines_running = 0`, scale-to-zero)

**File Storage:**
- Local filesystem only — knowledge base markdown files under `kb/` (`kb/account.md`, `kb/api.md`, `kb/billing.md`), read directly via `Path.glob("*.md")` in `src/relay/tools.py::search_docs()`. No object storage (S3/GCS) integration present.

**Caching:**
- No dedicated caching service (Redis/Memcached). The only "cache" present is Anthropic's own prompt caching, enabled per-request via `cache_control: {"type": "ephemeral"}` on the system prompt block in `src/relay/agent.py`.

## Authentication & Identity

**Auth Provider:**
- None. The FastAPI service (`src/relay/main.py`) has no authentication/authorization middleware — all endpoints (`/tickets`, `/tickets/{id}/process`, `/metrics`, `/dashboard`) are unauthenticated. This is a notable gap for a "production-grade" service exposed publicly on Fly.io.
- The only "auth" in the system is the Anthropic API key used server-side to call Claude.

## Monitoring & Observability

**Error Tracking:**
- No dedicated error-tracking service (e.g. Sentry). Errors surface as structured JSON log lines (`src/relay/telemetry.py::JsonFormatter`) and as `error`-typed SSE events (`src/relay/agent.py`).

**Logs:**
- Structured JSON logs to stdout via `configure_logging()` in `src/relay/telemetry.py`. Single-line JSON per event with `ts`, `level`, `logger`, `event`, plus a `ctx` dict of structured fields. `uvicorn.access` logs are suppressed to `WARNING` to avoid duplicate request logs.
- Tracing: OpenTelemetry spans per agent run (`agent.run`), per model call (`claude.request`), and per tool execution (`tool.<name>`), exported via OTLP when configured (see above).
- App-level metrics: every agent run is persisted to the `runs` SQLite table (`record_run()` in `telemetry.py`) and aggregated (p50/p95 latency, token/cost totals, outcome counts) by the `/metrics` endpoint and rendered on `/dashboard` (`src/relay/main.py`).

## CI/CD & Deployment

**Hosting:**
- Fly.io - app `relay-agent`, primary region `syd` (Sydney), config in `fly.toml`. Live demo: `https://relay-agent.fly.dev`.
- Container built via `Dockerfile`, single-stage, `python:3.12-slim` base.

**CI Pipeline:**
- GitHub Actions - two workflows:
  - `.github/workflows/ci.yml` - runs on every push to `main` and on PRs: `ruff check`, `pytest -q`, plus a Docker build + smoke test (`docker run` + polling `/health` with a placeholder `ANTHROPIC_API_KEY=ci-placeholder`)
  - `.github/workflows/evals.yml` - manual (`workflow_dispatch`) eval run against the real Anthropic API, using the `ANTHROPIC_API_KEY` GitHub secret, with a configurable pass-rate `threshold` (default 0.8); uploads `eval_results/` as a build artifact

## Environment Configuration

**Required env vars:**
- `ANTHROPIC_API_KEY` - required for any agent/eval run to actually call Claude (no default; `config.py` allows `None` but calls will fail without it)

**Optional env vars (with defaults):**
- `RELAY_MODEL`, `RELAY_DB_PATH`, `RELAY_KB_DIR`, `RELAY_MAX_AGENT_STEPS`, `RELAY_MAX_TOKENS`, `RELAY_MAX_RUN_COST_USD`, `RELAY_PRICE_IN_PER_MTOK`, `RELAY_PRICE_OUT_PER_MTOK`, `RELAY_MCP_ALLOW_WRITES`, `OTEL_EXPORTER_OTLP_ENDPOINT`

**Secrets location:**
- Local development: `.env` file (present, gitignored, not committed — `.env.example` is the tracked template)
- CI: GitHub Actions repository secret `ANTHROPIC_API_KEY` (used only in `evals.yml`; `ci.yml` uses a placeholder value since it only smoke-tests container startup)
- Production: not visible in this repo — presumably set as a Fly.io secret (`fly secrets set`), not present in `fly.toml`

## Webhooks & Callbacks

**Incoming:**
- None. No webhook receiver endpoints exist in `src/relay/main.py`.

**Outgoing:**
- None. `send_reply` in `src/relay/tools.py` explicitly mocks email delivery — the reply is persisted to the `replies` table only; nothing is sent over the network ("Email delivery is mocked: the reply is persisted, nothing leaves the system").

## Alternate Integration Surface

- **MCP (Model Context Protocol) server** — `src/relay/mcp_server.py` exposes the same tool registry used by the HTTP agent loop over stdio (`python -m relay.mcp_server`), so any MCP client (Claude Desktop, Claude Code, an IDE) can drive the system directly. Adds two MCP-only tools (`create_ticket`, `list_open_tickets`) and enforces the same Pydantic validation + `ToolPolicy` write-gating as the HTTP path (`RELAY_MCP_ALLOW_WRITES` env var, default read/write both allowed).

---

*Integration audit: 2026-08-05*

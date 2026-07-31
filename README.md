# Relay

An AI support-triage agent, built as a **production service** — not a notebook.

Relay receives support tickets for a fictional SaaS product (Lanekeep) over a REST
API and works each one autonomously: it looks the customer up in a real database,
classifies the ticket, searches the product documentation so every claim is
grounded, and either sends a resolved reply or escalates to a human with a
structured handover — streaming its reasoning steps to the client as server-sent
events the whole way.

The agent loop is written by hand on the [Claude API](https://platform.claude.com/)
(no orchestration framework), so the control flow, step caps, and event stream are
fully visible and testable.

## Status / roadmap

- [x] **Phase 1 — Core agent service**: FastAPI + SSE, hand-written agent loop,
      tools (`lookup_customer`, `search_docs`, `set_category`, `send_reply`,
      `create_escalation`), SQLite with seed data, keyword doc search
- [x] **Phase 2 — Guardrails**: Pydantic-validated tool inputs, per-run cost
      budget with hard abort, write-tool policy (`?dry_run=true`), structured
      error events on API failure, per-step `usage` events with running cost
- [x] **Phase 3 — Evaluation harness**: 12-ticket golden dataset, deterministic
      action/category grading plus LLM-as-judge grounding checks, JSON report
      artifact, threshold exit code for CI (`python -m relay.evals`)
- [x] **Phase 4 — Observability**: JSON structured logs, OpenTelemetry spans
      per run/model-call/tool (OTLP export via `OTEL_EXPORTER_OTLP_ENDPOINT`),
      per-run metrics in SQLite, `/metrics` aggregates, `/dashboard` page
- [x] **Phase 5 — MCP server**: the same tool registry (plus ticket
      lifecycle tools) served over the Model Context Protocol via stdio,
      behind the same validation and write-policy guardrails
- [ ] **Phase 6 — Ship it**: Docker, GitHub Actions CI, cloud deploy, live demo

See [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md) for the full project definition.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # add your ANTHROPIC_API_KEY

uvicorn relay.main:app --reload
```

Then, in another terminal:

```bash
./scripts/demo.sh
```

You'll see the agent's run streamed as SSE — text updates, each tool call and its
result, and a final `resolution` event.

## API

| Method | Path                          | Description                                   |
|--------|-------------------------------|-----------------------------------------------|
| `GET`  | `/health`                     | Liveness + configured model                   |
| `POST` | `/tickets`                    | Create a ticket                               |
| `GET`  | `/tickets/{id}`               | Fetch a ticket                                |
| `POST` | `/tickets/{id}/process`       | Run the agent; streams steps as SSE. `?dry_run=true` denies write tools by policy |
| `GET`  | `/metrics`                    | Run counts, outcomes, token/cost totals, latency p50/p95 |
| `GET`  | `/dashboard`                  | Minimal live dashboard over `/metrics`        |

## MCP server

The same tools are exposed over the [Model Context Protocol](https://modelcontextprotocol.io/),
so Claude Desktop, Claude Code, or any MCP client can drive Relay directly:

```bash
claude mcp add relay -- /path/to/.venv/bin/python -m relay.mcp_server
```

Tool calls go through the same guardrail chain as the agent loop (Pydantic
input validation + write policy). Set `RELAY_MCP_ALLOW_WRITES=false` to serve
a read-only surface.

## Tests

```bash
pytest
```

Tests cover the tools and the HTTP surface without calling the Claude API, so
they run free and fast in CI.

## Architecture

```
client ──POST /tickets/{id}/process──▶ FastAPI ──▶ agent loop (Claude API)
   ◀───────── SSE: text / tool_use / tool_result / resolution ─────────┘
                                          │
                          tools ──▶ SQLite (customers, tickets,
                                    escalations, replies)
                                └─▶ kb/*.md (docs search)
```

## License

MIT

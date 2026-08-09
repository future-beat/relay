# Relay — Remaster

## What This Is

Relay is an AI support-triage agent built as a production service: it receives support tickets over a REST API, works each one autonomously with a hand-written agent loop on the Claude API (customer lookup, classification, grounded doc search, reply or escalation), and streams its reasoning as SSE. This milestone remasters the existing v1 into a portfolio showpiece: production-hardened, with real semantic retrieval and a polished live dashboard.

## Core Value

A visitor hitting the live demo sees a credible, safe, observably-real AI agent service — impressive to read and watch, cheap to keep running.

## Requirements

### Validated

- ✓ Ticket REST API with SSE-streamed agent runs (create, fetch, process) — existing
- ✓ Hand-written agent loop on the Claude API with tool registry (lookup_customer, search_docs, set_category, send_reply, create_escalation) — existing
- ✓ Guardrails: Pydantic tool validation, per-run cost budget with hard abort, write-tool dry-run policy — existing
- ✓ Eval harness: 12-ticket golden dataset, deterministic + LLM-judge grading, CI threshold exit code — existing
- ✓ Observability: JSON logs, OpenTelemetry spans, per-run metrics in SQLite, /metrics endpoint — existing
- ✓ MCP server exposing the same tool registry over stdio behind the same guardrails — existing
- ✓ Shipped: Dockerfile, GitHub Actions CI, Fly.io deployment with live demo — existing

### Active

- [ ] API-key auth on all mutating/costly endpoints (env-var key check; dashboard/metrics may stay public read-only)
- [ ] Rate limiting on ticket creation and processing to cap aggregate Claude API spend
- [ ] Server-side ticket_id binding — the tool executor enforces the run's ticket id, never trusting the model's argument
- [ ] Async-safe SQLite access (thread offload or aiosqlite + WAL) replacing the shared blocking connection
- [ ] MCP writes default to disabled (explicit env opt-in)
- [ ] Semantic doc retrieval via Voyage embeddings API (indexing + query path) replacing keyword scoring
- [ ] Polished server-rendered dashboard: live run feed via SSE, cost/latency/outcome charts, no build step
- [ ] Graceful shutdown draining for in-flight SSE runs

### Out of Scope

- Postgres migration — SQLite with fixed access patterns is sufficient for a single-instance demo and keeps Fly cost near zero
- Separate SPA frontend — a server-rendered page keeps the one-container deploy and avoids a build pipeline/CORS surface
- Multi-tenancy, user accounts, secret rotation/vault — demo project; env-var API keys are acceptable
- Full rewrite/re-architecture — the hand-written agent loop is the point of the project; harden it, don't replace it
- Local/ONNX embedding models — chose Voyage API for retrieval quality; per-query cost is negligible at demo traffic

## Context

- Brownfield: v1 is complete through 6 phases (agent service, guardrails, evals, observability, MCP, shipping) and deployed at https://relay-agent.fly.dev.
- Codebase map exists at `.planning/codebase/` (2026-08-05). CONCERNS.md drives the hardening scope: no auth/rate-limiting, model-supplied ticket_id unchecked, blocking SQLite in async handlers, `mcp_allow_writes` defaulting to true, keyword-only retrieval.
- Test suite: 37 tests, no Claude API calls in CI; eval suite runs on demand against a repo secret.
- The fictional SaaS product is "Lanekeep"; KB is 3 markdown files in `kb/`.

## Constraints

- **Budget**: Live demo must stay cheap — single Fly machine, min_machines_running=0, per-run cost budget retained; Voyage usage is index-once + tiny per-query cost
- **Tech stack**: Python/FastAPI/SQLite/Claude API retained; no orchestration framework — the visible hand-written loop is a feature
- **Deploy**: One container, no build step, existing Fly.io + GitHub Actions pipeline keeps working
- **Compatibility**: SSE event contract and MCP tool surface stay backward compatible where practical; evals must keep passing

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Keep SQLite, fix access patterns | Single-instance demo; async-safe access solves the real problem without migration cost | — Pending |
| Voyage API embeddings for retrieval | Higher retrieval quality than local models; cost negligible at demo scale | — Pending |
| Server-rendered dashboard | No build step, one-container deploy preserved | — Pending |
| Auth via env-var API key | Right-sized for a demo; blocks cost abuse without account infrastructure | — Pending |
| MCP writes off by default | Safe default posture for any connecting client | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-08-05 after initialization*

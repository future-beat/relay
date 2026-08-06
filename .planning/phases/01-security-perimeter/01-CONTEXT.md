# Phase 1: Security Perimeter - Context

**Gathered:** 2026-08-06
**Status:** Ready for planning

<domain>
## Phase Boundary

Close the perimeter on the live, paid, currently-unauthenticated service: API-key auth on mutating/costly endpoints, tiered rate limiting, a persistent daily USD spend circuit breaker, server-side ticket_id binding in the tool executor, MCP writes off by default, and a published demo key. Requirements: SEC-01..SEC-06. No retrieval, data-layer, or dashboard work in this phase.

</domain>

<decisions>
## Implementation Decisions

### Key & demo-key policy
- **D-01:** Two env vars, matching the existing `RELAY_` settings pattern: `RELAY_API_KEY` (owner tier) and `RELAY_DEMO_KEY` (demo tier)
- **D-02:** The demo key is published openly — in the README and on the dashboard. It is rate-limited anyway; openness is the portfolio statement, obscurity adds nothing

### Limit & budget values
- **D-03:** Global daily spend ceiling: **$5/day**, computed from `runs.cost_usd` in SQLite (survives cold starts). When exhausted: 503 with reset time (resets 00:00 UTC)
- **D-04:** Demo key: **5 runs/hour per IP** on `/tickets/{id}/process`; **20/hour per IP** on `POST /tickets`
- **D-05:** Owner key: loose ceiling (~60 runs/hour) — protects against a leaked key, never blocks legitimate use
- **D-06:** Rate-limit keying: API key tier + client IP (from `Fly-Client-IP` header behind the Fly proxy, falling back to `request.client.host` locally)

### Public vs protected surface
- **D-07:** Public (no key): `GET /dashboard`, `GET /metrics`, `GET /health`. Key required: `POST /tickets`, `POST /tickets/{id}/process`, `GET /tickets/{id}`
- **D-08:** 429/503 rejection bodies are friendly JSON: which limit was hit, when it resets, and a one-liner noting this is a deliberate cost-control feature of the demo. Include `Retry-After` and `X-RateLimit-*` headers on 429

### Injection denial behavior (ticket_id binding)
- **D-09:** The executor **rejects** a mismatched model-supplied `ticket_id` with a model-readable denial reason — no silent override. The observable rejection is the demo artifact
- **D-10:** A denial does **not** terminate the run — the agent may self-correct within its existing step/budget limits
- **D-11:** Denials emit a **distinct `guardrail` SSE event type** (additive to the event contract) plus a structured log counter — this feeds the Phase 5/6 run trace and dashboard

### Claude's Discretion
- Exact token-bucket/moving-window strategy and library wiring (research recommends `limits>=5.8` async APIs as FastAPI route dependencies, not middleware — honor unless a blocker emerges)
- Constant-time comparison implementation (`secrets.compare_digest`), 401-vs-403 wiring details
- How the MCP default flip (`mcp_allow_writes` → False) is documented/migrated
- Test structure, following existing conventions in `tests/`

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Research (this milestone)
- `.planning/research/ARCHITECTURE.md` — auth/rate-limiting as route dependencies not middleware (SSE-safe); three-tier rate limiting; spend cap from `runs` table; build-order rationale
- `.planning/research/STACK.md` — `limits>=5.8` over slowapi; Fly-Client-IP trap; version pins
- `.planning/research/PITFALLS.md` — registry-binding race (bind at call time in `_execute_guarded`, NOT into `app.state.registry`); health-check breakage from global auth; `BaseHTTPMiddleware` breaks `request.is_disconnected()`
- `.planning/research/FEATURES.md` — 401/403/429 semantics table stakes; demo-key rationale

### Codebase map
- `.planning/codebase/CONCERNS.md` — the exact vulnerabilities this phase closes (no auth, no rate limit, unchecked ticket_id, MCP writes default-on)
- `.planning/codebase/ARCHITECTURE.md` — current layering; `_execute_guarded` guard chain; `app.state` singletons
- `.planning/codebase/CONVENTIONS.md` — naming, error-handling, logging patterns to follow

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ToolPolicy` in `src/relay/guardrails.py` — existing per-call policy gate pattern; ticket_id binding extends this guard chain, returning model-readable denial strings like the write-policy denial does
- `_execute_guarded` in `src/relay/agent.py` — the single seam where every tool call passes; `run_ticket` has `ticket["id"]` in scope there (bind at call time — never into the shared registry)
- `AgentEvent` in `src/relay/models.py` — envelope for the new `guardrail` event type (additive)
- `Settings` in `src/relay/config.py` — `RELAY_`-prefixed env vars pattern for the new keys/limits/budget settings

### Established Patterns
- Guardrails enforced in code, not prompts; denials returned as model-readable strings, not exceptions
- FastAPI routes raise `HTTPException` with explicit status codes; new auth/limit rejections follow as dependencies
- Structured logging via `logger.info("event.name", extra={"ctx": {...}})` — denial counter follows

### Integration Points
- Auth + rate-limit + budget checks: FastAPI route dependencies on `POST /tickets`, `POST /tickets/{id}/process`, `GET /tickets/{id}` in `src/relay/main.py` (NOT middleware — SSE streaming)
- ticket_id binding: `_execute_guarded` signature gains the run's ticket id (threaded from `run_ticket`)
- MCP default: `src/relay/config.py:19` (`mcp_allow_writes: bool = True` → `False`)
- Docker HEALTHCHECK and CI curl hit `/health` — must stay public or CI/Fly health checks break

</code_context>

<specifics>
## Specific Ideas

- The 429/503 bodies should read as intentional product copy ("demo budget exhausted, resets at 00:00 UTC") — the guardrail is portfolio content
- The prompt-injection eval case that proves SEC-04 lands in Phase 4 (EVAL-02); this phase must make the guard observable enough (guardrail event) that the eval can assert on it

</specifics>

<deferred>
## Deferred Ideas

- Per-key usage accounting/dashboard breakdown — Phase 5/6 territory (rejected-action counter is a v2 dashboard metric)
- `/demo-key` endpoint variant — rejected in favor of publishing the key openly

</deferred>

---

*Phase: 01-security-perimeter*
*Context gathered: 2026-08-06*

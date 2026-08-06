# Requirements — Relay Remaster

**Defined:** 2026-08-05
**Milestone:** v2 "Remaster" (brownfield — v1 is complete and deployed)

## v1 Requirements

### Security Perimeter

- [ ] **SEC-01**: All mutating/costly endpoints (`POST /tickets`, `POST /tickets/{id}/process`) require an API key via `X-API-Key` header, checked with constant-time compare against env-var keys; 401 (with `WWW-Authenticate`) vs 403 semantics are correct
- [ ] **SEC-02**: Ticket creation and processing are rate-limited per key (IP fallback), with `429` responses carrying `Retry-After` and rate-limit headers; limits key on `Fly-Client-IP` behind the Fly proxy
- [ ] **SEC-03**: A global daily USD spend circuit breaker (derived from `runs.cost_usd` in SQLite, surviving cold starts) returns 503 with a clear reset message when the demo budget is exhausted
- [ ] **SEC-04**: The tool executor binds `ticket_id` server-side to the run's actual ticket; a mismatched model-supplied id produces a model-visible denial event (not a crash) and a counter increment
- [ ] **SEC-05**: MCP writes default to disabled; enabling requires an explicit `RELAY_MCP_ALLOW_WRITES=true`
- [ ] **SEC-06**: A published, tightly-limited demo key lets visitors run the agent; auth/rate-limit tiers distinguish it from the owner key

### Data Layer

- [ ] **DATA-01**: All SQLite access is async-safe (thread offload at the `_execute_guarded`/handler seam, per-connection ownership fixed, WAL + busy_timeout on file databases); the sync `ToolSpec.execute` contract is preserved for MCP and tests
- [ ] **DATA-02**: Graceful shutdown drains in-flight SSE runs before closing the database; `record_run` persists even when a run is interrupted (moved to a `finally` path)
- [ ] **DATA-03**: A `run_events` table persists per-run step events (tool calls, results, retrieval, denials, usage) written during the stream, enabling per-run drill-down

### Semantic Retrieval

- [ ] **RAG-01**: `search_docs` uses semantic retrieval over a precomputed `voyage-4-lite` embeddings index (heading-aware chunks, correct `input_type` at index vs query time, cosine over in-memory numpy)
- [ ] **RAG-02**: The embeddings index is a committed offline artifact (built by a script, KB-hash-stamped, staleness-checked in CI) — no Voyage call on the cold-start or CI path
- [ ] **RAG-03**: Retrieval results carry stable citation IDs (`{doc}#{heading}`) with doc, heading, text, and score
- [ ] **RAG-04**: `send_reply` accepts a structured `citations` argument; the executor validates every cited id was actually retrieved during the run
- [ ] **RAG-05**: Retrieval degrades gracefully to the keyword scorer when Voyage is unavailable, logging and surfacing the degradation in the run event stream

### Evaluation

- [ ] **EVAL-01**: A retrieval eval set (labeled query → relevant chunk ids) reports recall@k and MRR, wired into the existing harness, and the existing 12-ticket eval suite does not regress below its CI threshold
- [ ] **EVAL-02**: A prompt-injection golden case (ticket body attempting to act on another ticket) asserts the SEC-04 guard fires
- [ ] **EVAL-03**: A citation-faithfulness check asserts every chunk id cited in a reply was retrieved in that run (deterministic; no LLM judge)

### Dashboard

- [ ] **DASH-01**: The dashboard receives a live run feed over a public, projection-only SSE `/events` endpoint (no polling; no sensitive data)
- [ ] **DASH-02**: Aggregate cards and outcome distribution (resolved/escalated/error/budget_exceeded/step_limit) render from `/metrics`, computed via SQL aggregation
- [ ] **DASH-03**: Per-run drill-down shows the full trace from `run_events`: tool calls with inputs/outputs/timings, retrieval chunks with scores and cited-vs-not highlighting, and guardrail denials
- [ ] **DASH-04**: Cost/latency-over-time renders as hand-rolled inline SVG; a gauge shows remaining daily demo budget — no CDN scripts, no build step
- [ ] **DASH-05**: A "Try it" form with prefilled example tickets submits via the demo key and streams the run live on the page

## v2 Requirements (Deferred)

- **README keyword-vs-semantic comparison** — pure writing once recall@k numbers exist for both retrievers
- **Eval results panel on the dashboard** — needs an eval-artifact storage decision (Fly volume vs repo)
- **Rejected-action counter as a dashboard metric** — rides on run_events; add once denials accumulate
- **Citation-faithfulness LLM-judge criterion** — semantic half of EVAL-03, after the structural check is stable
- **Cost attribution per agent stage** — marginal over per-run cost once drill-down exists
- **Dependency lockfile** — real but orthogonal; good filler item
- **SSE event schema versioning** — no external consumers yet

## Out of Scope

- **Postgres / vector DB / Redis** — single Fly machine, ~30-chunk corpus; each breaks the one-container near-zero-cost deploy
- **Reranker** — reranking top-10 of ~30 chunks is noise; document as considered-and-rejected
- **User accounts, OAuth, JWT, key rotation** — zero-tenant demo; static env-var keys with a documented threat model
- **WebSockets / `Last-Event-ID` resume** — SSE with EventSource auto-reconnect is the right shape; resume complexity exceeds value for ~20s runs
- **Alerting (PagerDuty/webhooks)** — nobody is on-call; the spend circuit breaker is the automated response
- **LLM-judge grading of live traffic** — doubles per-run cost on the endpoint being cost-capped; judging stays in the on-demand eval suite
- **SPA frontend / build step / CDN scripts** — server-rendered page preserves the one-container deploy
- **Local ONNX embeddings** — Voyage voyage-4-lite chosen; 200M free tokens/month makes it effectively free

## Traceability

Mapped by roadmap creation 2026-08-06 — 22/22 v1 requirements, no orphans.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SEC-01 | Phase 1 | Pending |
| SEC-02 | Phase 1 | Pending |
| SEC-03 | Phase 1 | Pending |
| SEC-04 | Phase 1 | Pending |
| SEC-05 | Phase 1 | Pending |
| SEC-06 | Phase 1 | Pending |
| DATA-01 | Phase 2 | Pending |
| DATA-02 | Phase 2 | Pending |
| RAG-01 | Phase 3 | Pending |
| RAG-02 | Phase 3 | Pending |
| RAG-03 | Phase 3 | Pending |
| RAG-04 | Phase 3 | Pending |
| RAG-05 | Phase 3 | Pending |
| EVAL-01 | Phase 4 | Pending |
| EVAL-02 | Phase 4 | Pending |
| EVAL-03 | Phase 4 | Pending |
| DATA-03 | Phase 5 | Pending |
| DASH-01 | Phase 5 | Pending |
| DASH-02 | Phase 6 | Pending |
| DASH-03 | Phase 6 | Pending |
| DASH-04 | Phase 6 | Pending |
| DASH-05 | Phase 6 | Pending |

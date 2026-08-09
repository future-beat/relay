# Roadmap: Relay — Remaster

## Overview

Relay v1 is live at https://relay-agent.fly.dev with a working agent loop, guardrails, evals, observability, MCP server, and CI/CD. This milestone remasters it into a portfolio showpiece. The journey runs outside-in by blast radius: first close the perimeter on a paid, unauthenticated endpoint (auth, tiered rate limits, a persistent daily spend ceiling, server-side `ticket_id` binding, MCP writes off); then make the data layer async-safe and drain in-flight SSE runs on shutdown, before retrieval and the dashboard add new call sites that would otherwise be refactored twice; then replace keyword scoring with a committed Voyage embeddings index that yields stable citation IDs and validated citations; then prove the new guarantees in the eval harness (recall@k, prompt-injection, citation faithfulness); and finally persist per-run events and build the server-rendered dashboard — live feed, charts, drill-down, and a "Try it" form — on top of a stable, safe base. Every phase ships to the same one-container Fly deploy with no build step.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Security Perimeter** - API-key auth, tiered rate limits, daily spend circuit breaker, server-bound ticket_id, MCP writes off by default (completed 2026-08-09)
- [ ] **Phase 2: Async-Safe Data Layer & Graceful Shutdown** - Thread-safe SQLite with WAL, single async offload seam, drain in-flight SSE runs before close
- [ ] **Phase 3: Semantic Retrieval** - Committed Voyage embeddings index, cited replies, keyword fallback
- [ ] **Phase 4: Evaluation Coverage** - Retrieval recall@k, prompt-injection guard case, citation-faithfulness check, no regression
- [ ] **Phase 5: Run Event Persistence & Live Feed** - run_events table plus a public, redacted SSE feed rendering live on the dashboard
- [ ] **Phase 6: Dashboard Experience** - Metric cards, outcome distribution, per-run drill-down, SVG charts, budget gauge, "Try it" form

## Phase Details

### Phase 1: Security Perimeter

**Goal**: The live demo can no longer be abused into unbounded Claude spend or cross-ticket writes
**Depends on**: Nothing (first phase)
**Requirements**: SEC-01, SEC-02, SEC-03, SEC-04, SEC-05, SEC-06
**Success Criteria** (what must be TRUE):

  1. Calling `POST /tickets` or `POST /tickets/{id}/process` without a valid `X-API-Key` returns 401 with `WWW-Authenticate`; a valid key succeeds and the dashboard/metrics stay publicly readable
  2. Exceeding the per-key (IP-fallback) limit returns 429 with `Retry-After` and rate-limit headers, and the published demo key is limited more tightly than the owner key
  3. Once the daily USD spend ceiling (read from `runs.cost_usd`) is reached, processing returns 503 with a reset message — and it stays enforced across a cold start
  4. A ticket body that instructs the agent to act on a different ticket produces a visible denial event in the run stream (the model retries in-run, the service does not crash) and the write lands on the correct ticket
  5. A freshly started MCP server refuses write tools unless `RELAY_MCP_ALLOW_WRITES=true` is explicitly set

**Plans**: 5 plans

Plans:
**Wave 1**

- [x] 01-01-PLAN.md — Config settings, `limits` dependency, MCP writes off by default (SEC-05)
- [x] 01-02-PLAN.md — Server-side `ticket_id` binding and the `guardrail` event (SEC-04)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 01-03-PLAN.md — `auth.py` and `ratelimit.py` modules plus the shared test harness (SEC-01/02/03/06)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 01-04-PLAN.md — Wire the composed gate into routes; integration coverage (SEC-01/02/03/06)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 01-05-PLAN.md — Publish the demo key, docs, demo script, end-to-end verification (SEC-06)

### Phase 2: Async-Safe Data Layer & Graceful Shutdown

**Goal**: Concurrent runs and deploy restarts no longer block the event loop, corrupt connection state, or lose run records
**Depends on**: Phase 1
**Requirements**: DATA-01, DATA-02
**Success Criteria** (what must be TRUE):

  1. Multiple tickets processed concurrently all stream normally, with no `SQLite objects created in a thread` errors and no stalled SSE streams
  2. The existing 37-test suite and the MCP server still work against the unchanged sync `ToolSpec.execute` contract (no executor is a coroutine function)
  3. Sending SIGTERM during an in-flight run lets that run finish streaming before the database closes, instead of erroring mid-stream
  4. A run interrupted by client disconnect or shutdown still appears in `runs` with its cost and outcome recorded

**Plans**: 5 plans

Plans:
**Wave 1**

- [ ] 02-01-PLAN.md — `Database` wrapper (RLock + materialised results + `transaction()`), WAL/busy_timeout pragmas, `idx_runs_created_at`, storage tests (DATA-01)
- [ ] 02-02-PLAN.md — `runs.py` `RunRegistry`, `shutdown_drain_seconds`, drain unit tests (DATA-02)

**Wave 2** *(blocked on Wave 1 completion)*

- [ ] 02-03-PLAN.md — Transactions in `tools.py`/`telemetry.py`, the single `asyncio.to_thread` seam in `agent.py`, ticket-aware test double (DATA-01)
- [ ] 02-04-PLAN.md — HTTP edge: offloaded handlers, registry wiring, drain before `conn.close()`, 503-while-draining (DATA-01/DATA-02)

**Wave 3** *(blocked on Wave 2 completion)*

- [ ] 02-05-PLAN.md — Concurrency/contract/lifecycle integration tests plus `fly.toml` `kill_timeout` and the Dockerfile graceful-shutdown window (DATA-01/DATA-02)

### Phase 3: Semantic Retrieval

**Goal**: The agent grounds replies in semantically retrieved docs with verifiable citations
**Depends on**: Phase 2
**Requirements**: RAG-01, RAG-02, RAG-03, RAG-04, RAG-05
**Success Criteria** (what must be TRUE):

  1. A ticket phrased in wording absent from the KB still retrieves the right doc, and a question the KB does not cover still returns nothing (escalation path preserved)
  2. Retrieval results show stable citation IDs (`{doc}#{heading}`) with doc, heading, text, and score in the run stream
  3. A reply citing an id that was not retrieved during that run is rejected by the executor
  4. Cold start and CI make zero Voyage calls — the index is a committed, KB-hash-stamped artifact and CI fails when it is stale
  5. With `VOYAGE_API_KEY` unset or the API failing, runs still complete via the keyword scorer and the degradation is visible in the run stream

**Plans**: TBD

### Phase 4: Evaluation Coverage

**Goal**: The eval harness measurably proves the retrieval and guardrail claims, not just asserts them
**Depends on**: Phase 3
**Requirements**: EVAL-01, EVAL-02, EVAL-03
**Success Criteria** (what must be TRUE):

  1. Running the eval harness reports recall@k and MRR for a labeled retrieval set alongside the existing ticket results
  2. The 12-ticket suite passes at or above its pre-change baseline and stays above the CI threshold
  3. A prompt-injection golden case fails if the server-side `ticket_id` guard is removed
  4. A deterministic citation-faithfulness check (no LLM judge) fails if a reply cites an unretrieved chunk

**Plans**: TBD

### Phase 5: Run Event Persistence & Live Feed

**Goal**: Every agent step is durably recorded and visitors watch runs happen live
**Depends on**: Phase 4
**Requirements**: DATA-03, DASH-01
**Success Criteria** (what must be TRUE):

  1. After a run completes, its full step sequence (tool calls, results, retrieval, denials, usage) is queryable from `run_events`
  2. An open dashboard tab shows runs appearing in real time over `/events` with no polling
  3. The public feed contains no ticket bodies, customer data, or API keys — only redacted projections
  4. A slow or abandoned browser tab never stalls or delays a paid agent run, and streams are capped so the machine can still scale to zero

**Plans**: TBD
**UI hint**: yes

### Phase 6: Dashboard Experience

**Goal**: A visitor can understand the system's cost, quality, and behavior in under a minute — and run it themselves
**Depends on**: Phase 5
**Requirements**: DASH-02, DASH-03, DASH-04, DASH-05
**Success Criteria** (what must be TRUE):

  1. The dashboard shows aggregate cards and an outcome distribution (resolved/escalated/error/budget_exceeded/step_limit) computed by SQL aggregation
  2. Clicking a run opens a drill-down with tool inputs/outputs and timings, retrieval chunks with scores and cited-vs-not highlighting, and guardrail denials
  3. Cost and latency over time render as inline SVG and a gauge shows remaining daily demo budget — with no CDN scripts and no build step
  4. A visitor can submit a prefilled example ticket from the page with the demo key and watch that run stream live

**Plans**: TBD
**UI hint**: yes

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Security Perimeter | 5/5 | Complete   | 2026-08-09 |
| 2. Async-Safe Data Layer & Graceful Shutdown | 0/5 | Planned | - |
| 3. Semantic Retrieval | 0/TBD | Not started | - |
| 4. Evaluation Coverage | 0/TBD | Not started | - |
| 5. Run Event Persistence & Live Feed | 0/TBD | Not started | - |
| 6. Dashboard Experience | 0/TBD | Not started | - |

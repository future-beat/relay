# Project Research Summary

**Project:** Relay — Remaster
**Domain:** Brownfield hardening of a single-instance FastAPI + SQLite + Claude API AI support-triage agent into a portfolio-grade production service (auth, spend-capped rate limiting, async-safe DB, semantic RAG with citations, live SSE observability dashboard)
**Researched:** 2026-08-06
**Confidence:** MEDIUM-HIGH

## Executive Summary

Relay already has a working, well-tested agent loop, eval harness, and Fly.io deployment; this milestone is not "build an agent," it's "make an unauthenticated, cost-exposed demo defensible to a hiring-manager-grade reviewer while keeping it cheap to run on a scale-to-zero 512 MB machine." Research across all four dimensions converges on a consistent, low-dependency, high-judgment approach: enforce auth and spend limits as FastAPI route dependencies (never `BaseHTTPMiddleware`, which silently breaks the SSE stream); make SQLite safe via a locked connection + WAL + a single `to_thread` offload seam rather than an `aiosqlite` rewrite that would force `async` through the tool registry, MCP server, and evals; do RAG with a raw `httpx` POST to Voyage plus a numpy/pure-Python in-memory index rather than the `voyageai` SDK or a vector database, because the entire KB is 3 files (~381 words); and build the dashboard with Jinja2 + vanilla `EventSource` + vendored Chart.js/Pico, never a CDN script or an f-string template.

The single highest-leverage design decision, repeated across FEATURES.md and PITFALLS.md, is pairing a public, heavily-rate-limited "try it" path with a persistent, SQLite-derived daily spend ceiling — this satisfies both the drive-by visitor (something to click) and the reviewer (evidence of cost engineering), and it is explicitly what OWASP LLM10 (Unbounded Consumption) asks for. Two structural findings raise the ceiling on required scope: (1) the dashboard's most valuable feature — per-run drill-down — requires a `run_events` persistence table that does not exist today and is not explicit in PROJECT.md, so it should be treated as a first-class requirement, not an implementation detail; and (2) the KB is small enough that "proper RAG" (chunking, always-return-top-k) is a **regression risk against the existing eval suite**, not a quality upgrade — indexing must preserve full-document returns and an empty-result floor, with the eval suite as the primary acceptance gate rather than a formality.

The dominant risk theme across PITFALLS.md is contract ripple: several "obvious" fixes (making `ToolSpec.execute` async, binding `ticket_id` by mutating the shared startup-built registry, wrapping the existing shared SQLite connection in bare `to_thread`, auth as global middleware) each look like local one-line changes but actually break a cross-cutting invariant (the sync tool contract shared by HTTP/MCP/evals, per-run isolation under concurrency, connection-scoped transaction atomicity, or SSE streaming semantics). Every research file independently arrives at the same mitigation pattern: keep contracts stable, push new behavior to a single well-defined seam (dependency, call site, or offline artifact), and use the existing eval suite plus new concurrency/negative tests as the forcing function that catches these regressions before they reach the live demo.

## Key Findings

### Recommended Stack

The stack additions are deliberately minimal — six new runtime dependencies, all justified against a 512 MB, scale-to-zero Fly machine and a `>=3.11` Python floor. Full detail in STACK.md.

**Core technologies:**
- `limits>=5.8,<6` (async namespace `limits.aio`): rate-limiting engine used directly as a FastAPI dependency — not `slowapi`, whose alpha-status decorator machinery is documented-incompatible with `StreamingResponse` endpoints
- `aiosqlite>=0.22,<1` + WAL + `busy_timeout`: async SQLite driver giving a dedicated writer connection with a serialized queue, solving both the event-loop-blocking and shared-connection-thread-safety problems at once
- Raw `httpx` (already transitively installed via `anthropic`) POST to `api.voyageai.com/v1/embeddings`: avoids the `voyageai` SDK's heavy transitive dependency tree (`requests`, `aiohttp`, `tokenizers`, `pillow`, `langchain-text-splitters`) for what is a single HTTP call
- `numpy>=2.3,<3` (not `>=2.5`, which requires Python 3.12+) loaded from a prebuilt `.npz`/`.json` index: exact, instant, zero-infra vector search for a corpus of ~30-80 chunks — `sqlite-vec` is verified unusable in the local dev environment (`enable_load_extension=False`)
- `jinja2>=3.1.6` + `sse-starlette>=3.4.8`: server-rendered dashboard templates and SSE responses that hook uvicorn's SIGTERM handler for graceful shutdown draining out of the box
- Vendored Chart.js 4.5.1 + Pico.css 2.1.1 (no CDN, no build step): keeps the "no build pipeline" constraint and removes third-party runtime dependencies from the demo's front door

`voyage-4-lite` at `output_dimension=512` is the recommended embedding model — current generation (not legacy voyage-3.5), 200M free tokens/month, effectively $0.00 cost at this scale. Architecture and pitfalls research diverge slightly from stack research on whether to add `numpy` at all (Architecture leans toward pure-Python dot product to avoid the wheel); either is fine at this corpus size — resolve during planning, not now.

### Expected Features

Full landscape, prioritization matrix, and dependency graph in FEATURES.md. The core framing: every feature is judged against two audiences — the 6-minute reviewer screening for cost engineering/evals/observability, and the drive-by visitor who wants to click one button and watch something real happen within 15 seconds.

**Must have (table stakes):**
- API key auth (constant-time compare, correct 401/403) + two-tier rate limiting with `429`/`Retry-After` on mutating/costly endpoints
- Server-side `ticket_id` binding in the tool executor (fixes the most technically interesting bug in the repo — prompt-injection-driven cross-ticket writes)
- MCP writes default off; async-safe DB access; graceful shutdown draining in-flight SSE runs
- Real embedding-based retrieval (Voyage) with correct `input_type`, heading-aware handling, stable citation IDs, and cited replies
- Live SSE run feed, aggregate metric cards, outcome distribution, per-run drill-down trace (blocked on new `run_events` persistence), cost/latency chart, budget gauge

**Should have (differentiators):**
- Public demo key + global daily spend circuit breaker (the single highest-leverage feature — satisfies both audiences at once)
- Retrieval eval set with recall@k run in CI, with a documented keyword-vs-semantic comparison in the README
- Citation-faithfulness eval check; prompt-injection golden eval case; "Try it" form with prefilled examples; rejected-action counter

**Defer (v2+):**
- Reranker (near-noise at ~30 chunks; document as considered-and-rejected)
- Cost attribution per agent stage, SSE schema versioning, any multi-instance infrastructure (Redis, Postgres, horizontal scale) — explicitly and permanently out of scope

### Architecture Approach

Full detail, diagrams, and build order in ARCHITECTURE.md. The package stays a flat module layout (5 new modules, no subpackages) with `templates/` and `static/` living inside `src/relay/` so Hatchling ships them automatically. Enforcement happens exclusively at the FastAPI dependency layer, never in `BaseHTTPMiddleware`, because Starlette explicitly documents that middleware form as unsafe with `StreamingResponse`. SQLite gets a thread-safe `Database` wrapper (lock + WAL + `busy_timeout`) with exactly one new `await asyncio.to_thread(...)` seam in the agent loop — deliberately not a full `aiosqlite` rewrite, to avoid forcing `async` through the tool registry, MCP server, and eval harness. The embedding index is built offline (`index_kb.py` CLI) and committed as a hash-verified artifact — never built in `lifespan`, since the machine boots frequently under scale-to-zero. The live dashboard is a broadcaster of *redacted projections* published from the HTTP edge (`main.py`), never from `agent.py` directly, over bounded drop-oldest queues so a slow browser can never backpressure a paid Claude run.

**Major components:**
1. `auth.py` — API-key dependency, constant-time compare, key-id passthrough for rate-limit keying
2. `ratelimit.py` — three-tier spend control: per-caller token bucket, `asyncio.Semaphore` concurrency cap, SQLite-derived persistent daily cost ceiling
3. `db.py` (Database wrapper) — thread-safe connection, WAL, single async offload seam
4. `retrieval.py` + `index_kb.py` — offline-built Voyage index, cosine ranking, keyword fallback on any failure
5. `events.py` (RunBroadcaster) — in-process pub/sub publishing bounded, redacted event projections to the dashboard's SSE feed

Suggested build order (also mirrored in roadmap implications below): security perimeter → async-safe data layer + graceful shutdown → semantic retrieval → dashboard + live feed. This order is driven by blast radius and by the live, unauthenticated, cost-bearing endpoint being the most urgent exposure right now.

### Critical Pitfalls

Top findings from PITFALLS.md's 15 documented pitfalls (all codebase-verified, HIGH confidence):

1. **Async contagion breaks the shared sync `ToolSpec.execute` contract** — switching any executor to `async` (via `aiosqlite` or a Voyage call) turns `tool_result` into a raw coroutine object across three consumers (agent loop, MCP server, evals) with no compile-time error. Avoid by offloading at the call site (`to_thread`) and keeping the registry contract sync; add a guard test asserting no executor is a coroutine function.
2. **Binding `ticket_id` into the shared, startup-built `app.state.registry` creates a cross-run race** — the registry is built once and shared across all concurrent runs; mutating it per-run corrupts other in-flight tickets. Bind at call time (pass `bound_ticket_id` into `_execute_guarded`), never at build time.
3. **Silently overwriting vs. hard-rejecting the model's `ticket_id`** both break something — overwriting makes the SSE stream/eval harness lie about what happened; hard-rejecting drops eval pass rate because `TERMINAL_TOOLS` resolution semantics assume denials only came from dry-run policy. Reject with a *recoverable* error message so the model retries in-run, and emit the effective input.
4. **`BaseHTTPMiddleware` for auth/rate limiting degrades SSE disconnect semantics**, letting the agent loop keep running (and spending) after a browser tab closes — the exact abuse vector auth was meant to close. Use route dependencies and pure-ASGI middleware only.
5. **In-memory rate limiting alone does not cap aggregate spend on a scale-to-zero machine** — the token bucket resets on every cold start (`min_machines_running=0`). A persistent, SQLite-derived daily spend ceiling is the actual acceptance criterion for the "cap aggregate spend" requirement.
6. **Naive RAG (chunking + always-return-top-k) regresses the eval suite** on a 381-word, 3-file KB — less context per chunk plus no similarity floor removes the "docs don't cover this → escalate" signal the keyword scorer currently provides. Embed whole files, preserve the empty-result path, and gate the phase on eval pass rate ≥ pre-change baseline (not just ≥ 0.8).

## Implications for Roadmap

Based on combined research (architecture's suggested build order + pitfalls' phase mapping + features' dependency graph, which independently converge on the same structure):

### Phase 1: Security Perimeter
**Rationale:** The service is live and unauthenticated right now; every other phase makes the demo more attractive to abuse. These changes are small, mutually independent, and touch only `main.py`/`guardrails.py`, so nothing blocks them and they block nothing else critical.
**Delivers:** `auth.py` (API-key dependency), `ratelimit.py` (token bucket + semaphore + daily SQLite-derived spend ceiling), server-side `ticket_id` binding at the `_execute_guarded` call-time seam, `mcp_allow_writes` default flip to `False`.
**Addresses:** API key auth, two-tier rate limiting + 429 semantics, daily spend circuit breaker, `ticket_id` binding, MCP writes off (all FEATURES.md P1 items).
**Avoids:** Pitfalls 2-7, 13 — cross-run registry race, silent-overwrite eval regression, `BaseHTTPMiddleware` SSE degradation, `slowapi`/`StreamingResponse` incompatibility, in-memory-only spend cap, `RELAY_` env-prefix key mismatch.

### Phase 2: Async-Safe Data Layer + Graceful Shutdown
**Rationale:** Widest blast radius (`main`, `tools`, `telemetry`, `mcp_server`, `agent`, `conftest.py`) — do it before retrieval and the dashboard add new call sites, or those get refactored twice. Shutdown draining is a `lifespan`/connection-teardown ordering problem that belongs here, not in the dashboard phase.
**Delivers:** Thread-safe `Database` wrapper (lock/connection-per-op + WAL + `busy_timeout` + `foreign_keys=ON` applied per connection), a single `to_thread` offload seam in the agent loop, in-flight SSE stream tracking with drain-before-close teardown, `record_run` moved into a `finally` so interrupted runs still get logged.
**Uses:** `aiosqlite` or a locked-connection alternative from STACK.md (either is defensible — architecture research leans toward the locked-connection/`to_thread` approach specifically to avoid async contagion).
**Implements:** `db.Database`, the `_execute_guarded` async offload seam (Architecture Pattern 3).
**Avoids:** Pitfall 1 (async contagion — must be decided here before Voyage inherits it), Pitfall 9 (partial commits from naive `to_thread` on a shared connection; WAL is a no-op on `:memory:` so tests need a `tmp_path` file DB), Pitfall 12 (context managers across `yield` corrupting OTel span parenting; `record_run` skipped on disconnect).

### Phase 3: Semantic Retrieval (Voyage Embeddings)
**Rationale:** Self-contained inside `tools.py` plus two new modules, with an objective acceptance gate (the eval suite must not regress) that should run against the now-stable data layer from Phase 2. Largely independent of Phase 1 and could be parallelized with it if desired.
**Delivers:** `index_kb.py` offline CLI producing a hash-verified, committed `kb/index.json` (or `.npz`) artifact; `retrieval.py` with correct `input_type` handling and a keyword fallback on any Voyage failure; stable citation IDs and a structured `citations` argument on `send_reply`, validated against retrieved chunks; a recall@k eval set wired into the existing harness; a prompt-injection golden eval case.
**Uses:** Raw `httpx` to Voyage, `voyage-4-lite` at 512 dims, numpy or pure-Python cosine ranking (STACK.md / ARCHITECTURE.md).
**Avoids:** Pitfall 10 (chunking/no-floor regressing the eval suite — do not chunk, preserve full-document returns and the empty-result path, diff per-case against baseline), Pitfall 11 (index built at startup or persisted to the volume instead of shipped in the image), Pitfall 13 (`VOYAGE_API_KEY` renamed by the `RELAY_` env prefix).

### Phase 4: Dashboard + Live Feed
**Rationale:** The only component that consumes everything else — it should surface rate-limit state, rebound-`ticket_id` events, and retrieval mode, all of which must exist first. It's also the most visible deliverable, so it benefits from landing on a stable base.
**Delivers:** `run_events` persistence table (the hidden critical-path dependency for every high-value dashboard feature); `events.py` `RunBroadcaster` publishing redacted projections; Jinja2 `templates/dashboard.html` + `static/` (migrated off the `DASHBOARD_HTML` string constant before any new feature is added); live SSE feed with a server-side max lifetime; SQL-aggregated `/metrics`; per-run drill-down trace; cost/latency chart; outcome distribution; budget gauge; "Try it" form with a published, rate-limited demo key.
**Implements:** `events.RunBroadcaster`, dashboard component (ARCHITECTURE.md).
**Avoids:** Pitfall 8 (persistent SSE connections defeating scale-to-zero — cap stream lifetime, client backoff), Pitfall 14 (f-string/template-literal collision in `DASHBOARD_HTML` — migrate to Jinja2 first), Pitfall 15 (public feed leaking ticket content/PII — publish redacted projections only, never raw `AgentEvent`s).

### Phase Ordering Rationale

- **Security first** was explicitly weighed against "data layer first" in ARCHITECTURE.md and rejected the alternative: doing the data layer first leaves a paid, unauthenticated Claude endpoint open for the duration of a broad refactor. The only cost of security-first is a single deferred conversion (the spend-cap SQL read) in Phase 2.
- **Async data layer before retrieval** because the async-executor-contract decision (Pitfall 1) is a prerequisite for both SQLite and Voyage changes, and the retriever should be written once against the final registry signature rather than twice.
- **Dashboard last** because it is purely a consumer of every other phase's outputs (spend state, `ticket_id` rebind events, retrieval mode, `run_events`), and because Pitfall 12 (interrupted-run data loss) becomes visible to real visitors once a live feed exists — so graceful shutdown must land first.
- Retrieval (Phase 3) is architecturally independent enough from Phase 1 to be parallelized if the team splits work, provided a single `build_registry` signature is agreed up front.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3 (Semantic Retrieval):** Highest-uncertainty phase in the milestone — Pitfall 10 notes retrieval quality on a 381-word corpus is untested by anyone publicly; the regression risk is reasoned from the eval harness's grading logic, not a published benchmark. Recommend running a baseline eval before committing to chunking/threshold decisions, and treat similarity-floor tuning as an empirical task during planning.
- **Phase 4 (Dashboard):** The `run_events` schema and redaction boundary (what a public projection may contain) are design decisions not fully specified in any research file — worth a short design pass before implementation.

Phases with standard patterns (skip research-phase):
- **Phase 1 (Security Perimeter):** Well-documented FastAPI dependency patterns, verified against Starlette/uvicorn docs and OWASP LLM10 guidance.
- **Phase 2 (Async Data Layer):** SQLite WAL/PRAGMA behavior is standard and well-documented (official SQLite docs); the main risk is a known pitfall (async contagion) rather than an unknown.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Versions verified against PyPI/npm registry APIs and official docs on the research date; a couple of architectural calls (numpy vs. pure-Python, aiosqlite vs. locked-connection) are MEDIUM-HIGH and intentionally left open for planning |
| Features | MEDIUM-HIGH | HIGH on Voyage/OWASP/observability-metric specifics from official docs; MEDIUM on "what reviewers expect," which is synthesized from hiring-guidance content rather than primary research |
| Architecture | MEDIUM-HIGH | Integration seams verified directly against the actual codebase; external-library claims verified against official docs; a few opinionated calls (e.g., hand-rolled rate limiter over slowapi) explicitly marked MEDIUM |
| Pitfalls | HIGH (codebase) / MEDIUM (external) | Codebase-derived pitfalls read directly from source (agent.py, tools.py, db.py, mcp_server.py, evals.py); library/platform behaviors verified against official docs and maintainer issue threads |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

- **Retrieval regression risk (Phase 3) is reasoned, not measured.** No published benchmark exists for embeddings quality on a corpus this small; obtain a baseline eval run before finalizing chunking/threshold decisions during Phase 3 planning.
- **`aiosqlite` vs. locked-connection-with-`to_thread`** is presented differently across STACK.md (recommends `aiosqlite`) and ARCHITECTURE.md/PITFALLS.md (leans toward a locked connection + single offload seam to avoid async contagion). Resolve explicitly during Phase 2 planning — the architecture/pitfalls reasoning (avoiding async contagion through the tool registry) is more directly grounded in this specific codebase and should likely win, but this is a genuine open decision, not a settled one.
- **`run_events` schema is unspecified.** PROJECT.md's dashboard requirement silently requires this table (per FEATURES.md's "Key Corrections to Current Plan"); it should be explicitly added as a requirement and designed during Phase 4 planning, not assumed.
- **Exact pinned Starlette/FastAPI version in production is unknown** (no lockfile currently exists) — Pitfall 5's disconnect-cancellation semantics should be confirmed empirically with a real disconnect test rather than assumed from version-general docs. A lockfile is recommended as an orthogonal, low-cost improvement during this milestone.

## Sources

### Primary (HIGH confidence)
- PyPI JSON API and npm registry API, queried 2026-08-06 — exact package versions, `requires_python`, dependency trees
- https://docs.voyageai.com/docs/embeddings, /docs/pricing, /reference/embeddings-api — model family, `input_type`, dimensions, pricing, free tier
- https://starlette.dev/middleware/ and encode/starlette issue/discussion threads — `BaseHTTPMiddleware` vs `StreamingResponse` incompatibility
- https://www.sqlite.org/wal.html, https://sqlite.org/pragma.html — WAL unavailable on `:memory:`, PRAGMA semantics
- https://fly.io/docs/reference/configuration/, /reference/fly-proxy-autostop-autostart/ — `kill_timeout`, autostop-on-idle behavior
- https://limits.readthedocs.io/en/stable/async.html — `limits.aio` import paths and design
- Direct codebase reads: `src/relay/{main,agent,tools,guardrails,db,telemetry,mcp_server,config,evals}.py`, `Dockerfile`, `fly.toml`, `.github/workflows/ci.yml`, `.planning/{PROJECT,codebase/ARCHITECTURE,codebase/CONCERNS,codebase/TESTING}.md`

### Secondary (MEDIUM confidence)
- Langfuse and LangSmith documentation — observability dashboard reference class (metrics, trace views, quality scores)
- OWASP Top 10 for LLM Applications 2025, LLM10:2025 Unbounded Consumption — API security / cost-control guidance
- Fly.io community threads and uvicorn GitHub discussions — SSE drain behavior under `--timeout-graceful-shutdown`
- `slowapi` source code — decorator/`StreamingResponse` incompatibility, read directly

### Tertiary (LOW-MEDIUM confidence)
- Portfolio/hiring-expectation articles (DEV.to, Elite AI Advantage, hireagentic.dev) — directional only, opinion content not primary research, but converged consistently across independent sources
- WebSearch on `aiosqlite` vs `to_thread` — corroborating only; no single authoritative comparison found, recommendation rests on `aiosqlite`'s own documented design

---
*Research completed: 2026-08-06*
*Ready for roadmap: yes*

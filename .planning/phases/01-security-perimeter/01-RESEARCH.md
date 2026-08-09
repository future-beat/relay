# Phase 1: Security Perimeter - Research

**Researched:** 2026-08-06
**Domain:** API-key auth, tiered rate limiting, persistent spend circuit breaker, and server-side identity binding retrofitted into a live FastAPI + SSE + SQLite agent service
**Confidence:** HIGH (every library claim verified by executing code against the actual installed/target versions; every codebase claim read from source)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Key & demo-key policy**
- **D-01:** Two env vars, matching the existing `RELAY_` settings pattern: `RELAY_API_KEY` (owner tier) and `RELAY_DEMO_KEY` (demo tier)
- **D-02:** The demo key is published openly — in the README and on the dashboard. It is rate-limited anyway; openness is the portfolio statement, obscurity adds nothing

**Limit & budget values**
- **D-03:** Global daily spend ceiling: **$5/day**, computed from `runs.cost_usd` in SQLite (survives cold starts). When exhausted: 503 with reset time (resets 00:00 UTC)
- **D-04:** Demo key: **5 runs/hour per IP** on `/tickets/{id}/process`; **20/hour per IP** on `POST /tickets`
- **D-05:** Owner key: loose ceiling (~60 runs/hour) — protects against a leaked key, never blocks legitimate use
- **D-06:** Rate-limit keying: API key tier + client IP (from `Fly-Client-IP` header behind the Fly proxy, falling back to `request.client.host` locally)

**Public vs protected surface**
- **D-07:** Public (no key): `GET /dashboard`, `GET /metrics`, `GET /health`. Key required: `POST /tickets`, `POST /tickets/{id}/process`, `GET /tickets/{id}`
- **D-08:** 429/503 rejection bodies are friendly JSON: which limit was hit, when it resets, and a one-liner noting this is a deliberate cost-control feature of the demo. Include `Retry-After` and `X-RateLimit-*` headers on 429

**Injection denial behavior (ticket_id binding)**
- **D-09:** The executor **rejects** a mismatched model-supplied `ticket_id` with a model-readable denial reason — no silent override. The observable rejection is the demo artifact
- **D-10:** A denial does **not** terminate the run — the agent may self-correct within its existing step/budget limits
- **D-11:** Denials emit a **distinct `guardrail` SSE event type** (additive to the event contract) plus a structured log counter — this feeds the Phase 5/6 run trace and dashboard

### Claude's Discretion
- Exact token-bucket/moving-window strategy and library wiring (research recommends `limits>=5.8` async APIs as FastAPI route dependencies, not middleware — honor unless a blocker emerges)
- Constant-time comparison implementation (`secrets.compare_digest`), 401-vs-403 wiring details
- How the MCP default flip (`mcp_allow_writes` → False) is documented/migrated
- Test structure, following existing conventions in `tests/`

### Deferred Ideas (OUT OF SCOPE)
- Per-key usage accounting/dashboard breakdown — Phase 5/6 territory (rejected-action counter is a v2 dashboard metric)
- `/demo-key` endpoint variant — rejected in favor of publishing the key openly
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SEC-01 | `POST /tickets`, `POST /tickets/{id}/process` require `X-API-Key`, constant-time compare against env-var keys; 401 (with `WWW-Authenticate`) vs 403 semantics correct | Pattern 1 (auth as route dependency), Code Example 1, Open Question 1 (403 trigger), Pitfall 2 (non-ASCII `compare_digest` TypeError) |
| SEC-02 | Ticket creation and processing rate-limited per key (IP fallback), 429 with `Retry-After` + rate-limit headers, keyed on `Fly-Client-IP` behind the Fly proxy | Pattern 2, Code Examples 2–3, Standard Stack (`limits` 5.8.0 async API verified), Pitfall 4 (`Fly-Client-IP` spoofable off-Fly), Pitfall 6 (module-level limiter leaks across tests) |
| SEC-03 | Global daily USD spend circuit breaker from `runs.cost_usd`, surviving cold starts, 503 + reset message | Pattern 3, Code Example 4, verified UTC-day SQL semantics, Pitfall 5 (in-flight runs invisible to the ceiling) |
| SEC-04 | Tool executor binds `ticket_id` server-side; mismatch produces model-visible denial + counter, not a crash | Pattern 4, Code Example 5, Divergence D-09 vs milestone research, Pitfall 1 (registry race), Pitfall 3 (denial→`ended_without_action` eval regression) |
| SEC-05 | MCP writes default disabled; enabling requires explicit `RELAY_MCP_ALLOW_WRITES=true` | Pattern 5 — one-line config flip + docstring/`.env.example`/README; verified `tests/test_mcp.py` is unaffected |
| SEC-06 | Published, tightly-limited demo key; auth/rate-limit tiers distinguish it from the owner key | Pattern 1 tier resolution + Pattern 2 tier-keyed limits; Deployment State Inventory (README/dashboard/`demo.sh`/Fly secrets) |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

Directives the planner must honor (same authority as locked decisions):

| Constraint | Implication for this phase |
|------------|---------------------------|
| No orchestration framework — the visible hand-written loop is a feature | Keep `run_ticket` shape; the new guard is a few lines inside `_execute_guarded`, not a middleware framework |
| Single Fly machine, `min_machines_running=0`, per-run cost budget retained | In-memory rate-limit storage is correct; **no Redis**. Daily cap must be SQLite-derived (survives cold start) |
| One container, no build step; existing Fly.io + GH Actions pipeline keeps working | `/health` must stay unauthenticated (Docker `HEALTHCHECK` + CI `curl -sf`) |
| SSE event contract stays backward compatible where practical; evals must keep passing | `guardrail` is **additive** — `evals.extract_outcome` ignores unknown event types (verified, `src/relay/evals.py:extract_outcome` has no `else` branch) |
| Naming: snake_case verb-first functions, `_`-prefixed private helpers, one concern per module | New modules `src/relay/auth.py`, `src/relay/ratelimit.py` (per `.planning/research/ARCHITECTURE.md` structure) |
| Type hints mandatory, modern `X \| None` unions; keyword-only params past 2–3 args | `_execute_guarded(..., *, bound_ticket_id: int \| None = None)` |
| Errors: domain exceptions or `HTTPException` with explicit status; no bare `except:`; single sanctioned broad-except at the tool boundary | Denials return model-readable strings, never raise |
| Logging: `logger.info("event.name", extra={"ctx": {...}})`, dotted event names | `guardrail.ticket_id_mismatch`, `auth.rejected`, `ratelimit.exceeded`, `budget.daily_exceeded` |
| Ruff `line-length = 100`, `ruff check src tests` in CI | Keep lines ≤ 100 |
| GSD workflow enforcement | Work proceeds via `/gsd:execute-phase` |

## Summary

This phase adds exactly **one** runtime dependency (`limits` 5.8.0) and two new flat modules (`auth.py`, `ratelimit.py`); everything else is stdlib (`secrets`, `datetime`) plus a handful of lines threaded into existing seams. The heavy lifting is not the code, it's the *placement*: every control must run as a **FastAPI route dependency**, resolved before `StreamingResponse` is constructed, because the moment the SSE generator yields its first byte the status line is locked at 200 and a 401/429/503 becomes impossible. `BaseHTTPMiddleware` (i.e. `@app.middleware("http")`) is forbidden here for the same reason plus its documented interference with streaming disconnect semantics.

I verified the whole rate-limit surface by executing it: `limits` 5.8.0 exposes `limits.aio.storage.MemoryStorage` and `limits.aio.strategies.MovingWindowRateLimiter`, `await limiter.hit(item, *identifiers) -> bool`, and `await limiter.get_window_stats(item, *identifiers) -> WindowStats(reset_time: float, remaining: int)` where `reset_time` is epoch seconds — exactly what `Retry-After` and `X-RateLimit-Reset` need. `parse("5/hour").key_for(...)` embeds the amount in the storage key, so per-tier items with the same identifiers do **not** collide. I also verified against the *installed* FastAPI 0.141.1 that `APIKeyHeader` with `auto_error=True` now raises **401** with `WWW-Authenticate: APIKey` (the old 403 behavior is gone), that route `dependencies=[...]` execute in declaration order and short-circuit on raise, and that a `dict` `HTTPException.detail` plus custom headers survives to the wire — which is all the machinery D-08 needs.

Two findings will bite if the planner doesn't budget for them. First, the daily spend ceiling reads `runs`, but `record_run` only fires *after* a stream completes, so N concurrent in-flight runs are invisible to the ceiling — a burst of 20 parallel runs at $0.50 each blows through $5 while the SQL sum still reads $0. Second, the rate limiter's `MemoryStorage` is module-level process state, so it accumulates across the entire pytest session and will start returning 429 to unrelated tests; an autouse reset fixture is mandatory, not optional.

**Primary recommendation:** Build `auth.py` (tier resolution via `secrets.compare_digest` on **bytes**) and `ratelimit.py` (limits-backed moving window + SQLite daily cap + in-flight reservation) as pure functions, compose them into a single ordered `run_gate` dependency applied per-route, and thread `bound_ticket_id` into `_execute_guarded` as a keyword-only arg so the MCP and eval call sites need zero changes.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| API-key extraction + tier resolution | API / Backend (`auth.py`) | — | Credentials never touch the browser tier; `EventSource` cannot set headers, which is why the dashboard stays on public read-only routes (D-07) |
| Per-tier/per-IP request throttling | API / Backend (`ratelimit.py`, in-process) | — | Single machine, no shared state to coordinate; CDN/edge throttling is not available on the Fly plan in use |
| Client IP determination | API / Backend, reading a proxy-injected header | Fly proxy (sets `Fly-Client-IP`) | The TCP peer is the Fly proxy; the real client IP only exists as a header |
| Daily USD spend ceiling | Database / Storage (`runs` table) | API / Backend (dependency reads it) | Must survive cold starts on a scale-to-zero machine; only the mounted volume persists |
| In-flight run cost reservation | API / Backend (process memory) | — | Complements the DB ceiling for the window before `record_run` writes |
| `ticket_id` binding | API / Backend (`agent._execute_guarded`) | — | Server-side truth must flow outward from the route; it must never flow inward from model output |
| Guardrail denial visibility | API / Backend emits `AgentEvent` → SSE | Browser (Phase 5/6 renders it) | This phase only needs the event to exist and be observable; rendering is Phase 5/6 |
| MCP write gating | API / Backend (`config.mcp_allow_writes`) | — | stdio transport has no HTTP layer to protect; the only control is the policy default |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `limits` | `5.8.0` (constrain `>=5.8,<6`) | Rate-limiting engine (moving window over in-memory storage) | The library `slowapi` is a thin wrapper around. Ships a first-class async namespace so limit checks never block the event loop or a threadpool. Requires Python `>=3.10` — compatible with the project's `>=3.11` floor. [VERIFIED: executed against limits 5.8.0 in an isolated venv] |
| `secrets` (stdlib) | — | Constant-time key comparison | `secrets.compare_digest` is the stdlib primitive for credential equality. Zero deps. [CITED: docs.python.org/3/library/secrets.html] |
| `fastapi.security.APIKeyHeader` | bundled with `fastapi` 0.141.1 (installed) | `X-API-Key` extraction + OpenAPI documentation of the auth requirement | Makes `/docs` show the auth requirement, and its own `make_not_authenticated_error()` already returns 401 + `WWW-Authenticate: APIKey`, which is the exact SEC-01 shape. [VERIFIED: read `fastapi/security/api_key.py` source from the installed 0.141.1] |
| `datetime` (stdlib, `UTC`) | — | UTC-midnight reset computation for the 503 body and `Retry-After` | Already imported this way in `src/relay/telemetry.py` (`from datetime import UTC, datetime`) — follow the existing convention |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `sqlite3` (stdlib) | — | `SELECT SUM(cost_usd) FROM runs WHERE created_at >= date('now')` | The daily ceiling. No new table, no migration — `runs` already has everything |
| `pydantic-settings` | 2.14.2 (installed) | `RELAY_API_KEY`, `RELAY_DEMO_KEY`, limit/budget settings | Extend the existing `Settings` class in `src/relay/config.py`; do not introduce a second config mechanism |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `limits` direct | `slowapi` 0.1.x | Self-declared alpha since 2020. Its `_inject_headers` requires the endpoint to declare and return a `Response`; `process_ticket` returns a `StreamingResponse` and takes no `response:` param → 500 on the exact endpoint most needing protection. Decorator ordering is a silent-failure footgun. [CITED: `.planning/research/PITFALLS.md` Pitfall 6; slowapi docs] |
| `limits` | ~40-line hand-rolled `TokenBucket` | `.planning/research/ARCHITECTURE.md` prefers hand-rolling for thematic consistency; CONTEXT.md D-"Claude's Discretion" resolves this in favour of `limits` ("honor unless a blocker emerges"). No blocker found — the async API is clean and `WindowStats.reset_time` gives correct `Retry-After` for free, which a naive bucket does not. **Follow CONTEXT.md: use `limits`.** |
| `MemoryStorage` | Redis / `fastapi-limiter` | Explicitly out of scope (single scale-to-zero machine; Redis would cost more than the Claude spend it guards) |
| Route dependencies | `BaseHTTPMiddleware` / `@app.middleware("http")` | Breaks `request.is_disconnected()` and cancellation propagation for `StreamingResponse`; forces a path-prefix allowlist for `/health`. Forbidden here |
| `MovingWindowRateLimiter` | `FixedWindowRateLimiter` | Fixed window is cheaper but allows a 2× burst across a boundary (10 runs in 2 minutes on a 5/hour limit). Moving window costs more memory per key — irrelevant at demo scale. **Use moving window.** |

**Installation:**
```bash
pip install "limits>=5.8,<6"
```

`pyproject.toml` addition (into `[project] dependencies`, not the dev extra — it runs in production):
```toml
"limits>=5.8,<6",
```

`limits` 5.8.0 transitive footprint (verified by fresh install): `Deprecated 1.3.1`, `packaging`, `typing_extensions`, `wrapt`. Four small pure-Python packages — negligible on a 512 MB machine. [VERIFIED: `pip list` in isolated venv]

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| `limits` | PyPI | Mature project (5.x line; 5.8.0 is current) | High (transitive dep of slowapi/flask-limiter ecosystem) | github.com/alisaifee/limits | `[OK]` | **Approved** |

**Packages removed due to slopcheck `[SLOP]` verdict:** none
**Packages flagged as suspicious `[SUS]`:** none

Verification performed 2026-08-06:
- `slopcheck install limits` → `[OK] limits (pypi)`, `1 OK` [VERIFIED]
- Correct-ecosystem registry check: installed from **PyPI** (not npm) and imported successfully at 5.8.0 [VERIFIED]
- Source repo confirmed reachable and containing the exact symbols recommended below (`limits/aio/storage/__init__.py` exports `MemoryStorage`; `limits/util.py` defines `WindowStats`) [VERIFIED: raw.githubusercontent.com]
- Postinstall-script check is N/A for PyPI wheels; `limits` ships a pure-Python wheel with no build hooks

## Architecture Patterns

### System Architecture Diagram

```
                       ┌──────────────────────────────────────────────┐
   HTTP request ──────▶│  FastAPI route matching (src/relay/main.py)  │
   X-API-Key: <key>    └───────────┬──────────────────────────┬───────┘
                                   │ protected route          │ public route
                                   ▼                          ▼
              ┌────────────────────────────────┐   ┌────────────────────────┐
              │  run_gate dependency (ordered)  │   │ /health /metrics       │
              │                                 │   │ /dashboard  /  →       │
              │  1. Security(require_key)       │   │ no credentials         │
              │       auth.resolve_tier()       │   └────────────────────────┘
              │       ├─ no/unknown key ─▶ 401 + WWW-Authenticate: APIKey
              │       └─ tier not allowed ─▶ 403
              │  2. ratelimit.check_daily_spend()
              │       SUM(runs.cost_usd) WHERE created_at >= date('now')
              │         + in-flight reservation
              │       └─ over $5 ─▶ 503 + Retry-After(→ next 00:00 UTC)
              │  3. ratelimit.hit(bucket, tier, client_ip)
              │       limits MovingWindowRateLimiter / MemoryStorage
              │       └─ exhausted ─▶ 429 + Retry-After + X-RateLimit-*
              └───────────────┬─────────────────┘
                              │ tier (str)  — all checks passed
                              ▼
              ┌───────────────────────────────────────────────┐
              │  process_ticket handler                        │
              │  ticket = _get_ticket(id)  ← SERVER TRUTH      │
              │  reserve(max_run_cost_usd) … release in finally│
              └───────────────┬───────────────────────────────┘
                              ▼
              StreamingResponse(event_stream())   ← status locked at 200 here
                              │
                              ▼
              agent.run_ticket(..., ticket)  bound_ticket_id = ticket["id"]
                              │
                     model emits tool_use{ticket_id: 99}
                              ▼
              ┌───────────────────────────────────────────────┐
              │ _execute_guarded(spec, name, raw, policy,     │
              │                  *, bound_ticket_id)          │
              │   unknown tool ─────────────▶ error           │
              │   ToolPolicy.denial_reason ─▶ error (policy)  │
              │   validate_tool_input ──────▶ error (pydantic)│
              │   ★ ticket_id != bound ─────▶ error           │
              │        {"denied_by": "ticket_binding", …}     │
              │   spec.execute(**validated)                   │
              └───────────────┬───────────────────────────────┘
                              │ (result_json, is_error)  ← arity UNCHANGED
                              ▼
              run_ticket: parse result once
                 denied_by == "ticket_binding" ?
                   ├─ yes ▶ yield AgentEvent(type="guardrail", …)   ← NEW
                   │        span attr relay.tool.binding_violation
                   │        logger.warning("guardrail.ticket_id_mismatch")
                   └────── ▶ yield AgentEvent(type="tool_result", …)
                              │        run continues — model may retry (D-10)
                              ▼
              main.event_stream: f"event: {event.type}\ndata: {json}\n\n"
                 (generic — "guardrail" serializes with zero code change)
                              ▼
              record_run() ─────▶ runs table ─────▶ feeds the daily ceiling
                                                    on the NEXT request
```

Note the feedback loop at the bottom: `runs.cost_usd` is written by the observability layer and *read* by the enforcement layer. The metrics table becomes a control input — worth calling out in the README as a design point.

### Recommended Project Structure

Follows `.planning/research/ARCHITECTURE.md` (flat modules, `test_<module>.py` 1:1):

```
src/relay/
├── auth.py            # NEW  APIKeyHeader dep, tier resolution, 401/403
├── ratelimit.py       # NEW  limits wiring, daily spend cap, in-flight reservation
├── main.py            # CHANGED  route dependencies; nothing else
├── config.py          # CHANGED  new RELAY_* settings; mcp_allow_writes default flip
├── agent.py           # CHANGED  bound_ticket_id kwarg + guardrail event emission
├── models.py          # CHANGED  AgentEvent docstring comment lists "guardrail"
└── mcp_server.py      # CHANGED  docstring only (default is now read-only)
tests/
├── test_auth.py       # NEW
├── test_ratelimit.py  # NEW
├── conftest.py        # CHANGED  autouse limiter reset + authed client fixture
├── test_api.py        # CHANGED  all 3 tests need a key
└── test_observability.py  # CHANGED  _make_ticket + process calls need a key
```

### Pattern 1: Auth as a route dependency with explicit tier resolution

**What:** `APIKeyHeader(auto_error=False)` extracts the header; a pure function resolves it to a tier; a dependency factory enforces which tiers are allowed.

**When to use:** Every protected route in D-07. Never as middleware.

**Why a dependency, not middleware (three independent reasons):**
1. Dependencies resolve **before** the handler returns `StreamingResponse`, so 401/429/503 are real HTTP status codes. Once the generator yields, the status line is flushed and an auth failure can only appear as an in-stream `event: error` on a 200 — which is not a rate limit at all.
2. `BaseHTTPMiddleware` interposes an anyio stream that breaks `request.is_disconnected()` and cancellation propagation for streaming responses — the SSE stream is the single most valuable thing in the demo.
3. Per-route granularity. `/health` must stay public or the Docker `HEALTHCHECK` and the CI `curl -sf http://127.0.0.1:8000/health` smoke job break. An allowlist of protected routes beats a denylist of exempt paths.

**Verified execution semantics** (FastAPI 0.141.1, executed):
- Route `dependencies=[Depends(a), Depends(b), Depends(c)]` run in **declaration order** and short-circuit on the first raise (`a` ran, `b`/`c`/handler did not).
- `HTTPException(401, "nope", headers={"WWW-Authenticate": "APIKey"})` reaches the wire with the header intact.
- `HTTPException(429, detail={"error": ..., "resets_at": ...}, headers={"Retry-After": "60"})` produces body `{"detail": {"error": ..., "resets_at": ...}}` with `Retry-After: 60`.

**Design call — body shape for D-08:** FastAPI nests a dict `detail` under `{"detail": ...}`. That is idiomatic and consistent with the existing `HTTPException(404, "ticket not found")` convention in `main.py`. If a flat top-level body is wanted instead, register `@app.exception_handler(RelayLimitError)` returning a `JSONResponse` directly. Recommend the nested form — one fewer moving part, and it keeps `/docs` error schemas honest.

**Anti-pattern:** do **not** accept the key as a query parameter. Query strings land in Fly proxy logs, browser history, and `Referer` headers.

### Pattern 2: Tier-keyed moving window via `limits`, checked in the dependency

**What:** One `MovingWindowRateLimiter` over one `MemoryStorage`, with a `RateLimitItem` per (bucket, tier) pair, hit with identifiers `(bucket, tier, client_ip)`.

**Verified API surface** (`limits` 5.8.0, executed):
```python
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

await limiter.hit(item, *identifiers, cost=1) -> bool          # True = allowed, consumes
await limiter.test(item, *identifiers, cost=1) -> bool          # peek, does not consume
await limiter.get_window_stats(item, *identifiers) -> WindowStats
# WindowStats is a NamedTuple: (reset_time: float, remaining: int)
# reset_time is epoch seconds  →  Retry-After = ceil(reset_time - time.time())
```

Verified details that matter:
- `MemoryStorage()` constructs fine **outside** a running event loop, so it can be a module-level singleton.
- `parse("5/hour").key_for("process", "demo", "1.2.3.4")` → `LIMITER/process/demo/1.2.3.4/5/1/hour`. The **amount is part of the key**, so a 5/hour item and a 20/hour item with identical identifiers occupy separate buckets — no cross-contamination between tiers or routes.
- `MemoryStorage` exposes `reset()` (returns `int | None`) — this is the test-isolation hook (see Pitfall 6).
- Before any hit, `get_window_stats` returns `reset_time = now + window`, `remaining = amount`.

**Limit table (from D-04/D-05; owner values and the read limit are Claude's discretion):**

| Bucket | Route | Demo tier | Owner tier |
|--------|-------|-----------|------------|
| `process` | `POST /tickets/{id}/process` | `5/hour` (D-04) | `60/hour` (D-05) |
| `create` | `POST /tickets` | `20/hour` (D-04) | `120/hour` (discretion) |
| `read` | `GET /tickets/{id}` | `120/hour` (discretion) | `600/hour` (discretion) |

`GET /tickets/{id}` is key-protected per D-07 but costs nothing in Claude spend; a loose limit exists only to stop enumeration scraping of ticket bodies. The planner may also choose to leave it unlimited — flag as a decision, not a requirement.

**Client IP resolution (D-06):**
```python
def client_ip(request: Request) -> str:
    if settings.trust_proxy_header:
        forwarded = request.headers.get("fly-client-ip")
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"
```
`Fly-Client-IP` is set by the Fly proxy and is authoritative *behind* the proxy. Off-Fly it is fully client-controlled, so an attacker could rotate the header to get unlimited buckets. Gate it behind a setting (`RELAY_TRUST_PROXY`, default `false`) and set `RELAY_TRUST_PROXY = 'true'` in `fly.toml`'s `[env]` block alongside the existing `PORT`/`RELAY_DB_PATH` entries.

### Pattern 3: Daily spend circuit breaker from `runs`, plus an in-flight reservation

**What:** Sum today's `cost_usd` from SQLite, add the reserved cost of currently-streaming runs, compare to `RELAY_MAX_DAILY_COST_USD` (default `5.0` per D-03).

**Verified UTC-day semantics** (executed against the real `SCHEMA` from `src/relay/db.py`):
- `runs.created_at TEXT NOT NULL DEFAULT (datetime('now'))` — SQLite's `datetime('now')` is **UTC**, format `'2026-08-06 03:57:15'`.
- `date('now')` is the **UTC** date, `'2026-08-06'`.
- `WHERE created_at >= date('now')` works by lexicographic string comparison and correctly selects today's rows (verified: returns `0.25` for a row inserted moments earlier). `datetime('now','start of day')` is equivalent and more explicit — either is correct; prefer the explicit form for readability.
- No timezone conversion is needed anywhere, and no `created_at` index is warranted at demo table sizes.

**Reset time for the 503 body and `Retry-After`:** next 00:00 UTC.
```python
def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
```

**The in-flight gap (this is the part that is easy to miss):** `record_run` is only called after the SSE generator finishes (`src/relay/main.py:91`). So a burst of concurrent `/process` requests all read the same stale `SUM(cost_usd)` and all pass the ceiling. Worst case is `concurrency × max_run_cost_usd` over budget. Two mitigations, in order of preference:

1. **In-process reservation (recommended, ~12 lines):** a module-level `_reserved_usd: float` incremented by `settings.max_run_cost_usd` when a run is admitted and decremented in the handler's `finally`. Effective spend = `db_sum + _reserved_usd`. Closes the hole for the single-machine deployment this project targets.
2. **Concurrency semaphore:** bounds worst-case overshoot to `N × max_run_cost_usd`. Useful anyway for a 512 MB machine, but **must not** be acquired with `async with` wrapped around the agent loop — see Pitfall 7.

Phase 2 (DATA-02) moves `record_run` into a `finally` path, which reduces but does not eliminate the gap (the row still lands only at the end). Keep the reservation.

**HTTP status:** D-03 locks 503. Include `Retry-After` (seconds to next UTC midnight) — RFC 9110 explicitly sanctions `Retry-After` on 503.

### Pattern 4: `ticket_id` bound at call time, rejected on mismatch, observable as a `guardrail` event

**What:** `_execute_guarded` gains a keyword-only `bound_ticket_id: int | None = None`. `run_ticket` passes `ticket["id"]`. On mismatch, return a model-readable error with a `denied_by` marker; `run_ticket` turns that marker into a `guardrail` event.

**Where the check goes:** immediately *after* `validate_tool_input` and *before* `spec.execute`. That is the single choke point every tool call passes through, for the HTTP loop and the MCP server alike.

**Why call time, not build time (Pitfall 1 in the milestone research):** `build_registry(conn, kb_dir)` is called **once** at lifespan startup and stored in `app.state.registry`; every concurrent `/process` run shares the same dict of the same closures. Binding into the registry — or mutating any per-run state on it — makes two overlapping runs overwrite each other's ticket id, reproducing the exact cross-ticket-write bug with a race as the trigger instead of an injection.

**Why the return arity must stay `(str, bool)`:** `mcp_server.call_mcp_tool` unpacks exactly two values (`src/relay/mcp_server.py:119`). Adding a third element breaks the MCP path and its six tests. Signal the denial through a `denied_by` field in the JSON payload instead — this mirrors the existing policy denial, which already emits `{"error": ..., "denied_by": "policy"}` (`src/relay/agent.py:47`).

**Why `run_ticket` shouldn't parse twice:** the loop already does `json.loads(result)` when constructing the `tool_result` event (`src/relay/agent.py:180`). Parse once, inspect `denied_by`, reuse for both events.

**Ordering:** emit `guardrail` **before** `tool_result` so a consumer reading the stream sees cause then effect.

**Divergence from `.planning/research/ARCHITECTURE.md` — resolved by CONTEXT.md:** the milestone architecture research recommends *rebinding* (silently overwriting the model's `ticket_id` with the run's). **CONTEXT.md D-09 overrides this: reject, do not override.** The planner must follow D-09. The rebind approach is also independently worse for this project: it makes the `tool_use` SSE event and the dashboard lie about what happened, and `evals.extract_outcome` reads `event.data["input"]` from `tool_use` events, so it would record the model's bogus id rather than the effective one — turning a neutralized injection into an invisible one. Rejection is both the locked decision and the better one.

**MCP is a different threat model:** there is no "current run" over stdio, so `bound_ticket_id` stays `None` there and MCP behavior is unchanged. MCP's protection is SEC-05's default flip.

### Pattern 5: MCP default flip is a config change plus a documentation sweep

`src/relay/config.py:19` — `mcp_allow_writes: bool = True` → `False`. That is the whole functional change; `create_server` already reads `settings.mcp_allow_writes` at call time (`src/relay/mcp_server.py:127`).

The documentation sweep is the part that gets forgotten (all four are currently wrong after the flip):

| Location | Current | After |
|----------|---------|-------|
| `src/relay/config.py:19` | `mcp_allow_writes: bool = True` | `False` |
| `src/relay/mcp_server.py` module docstring | "Set `RELAY_MCP_ALLOW_WRITES=false` to serve a read-only tool surface." | "Writes are disabled by default; set `RELAY_MCP_ALLOW_WRITES=true` to enable them." |
| `.env.example` | `RELAY_MCP_ALLOW_WRITES=true` | `RELAY_MCP_ALLOW_WRITES=false` (with a comment that `true` opts in) |
| `README.md` MCP section | (check for stale wording) | Document the opt-in |

**Test impact: none of the six existing MCP tests break.** `tests/test_mcp.py` constructs `ToolPolicy()` explicitly (dataclass default `allow_writes=True`, unchanged) and never reads `settings.mcp_allow_writes` [VERIFIED: read `tests/test_mcp.py` in full]. Add one new test asserting `Settings().mcp_allow_writes is False` and one asserting `create_server` under default settings produces a policy that denies a write tool.

### Anti-Patterns to Avoid

- **`@app.middleware("http")` / `BaseHTTPMiddleware` for auth or limits:** breaks SSE disconnect semantics, breaks `/health`, and forces a path-prefix denylist. Use route dependencies.
- **Rate-limit or budget checks inside `event_stream()`:** the response is already 200 by then. A rejection expressed as an in-stream `event: error` is not a rate limit.
- **`==` for key comparison:** timing-attack vulnerable. Use `secrets.compare_digest` on bytes.
- **Short-circuiting the tier comparison** (`if compare_digest(k, owner): ... elif compare_digest(k, demo): ...`) leaks *which* key was closer via timing. Evaluate both comparisons unconditionally, then branch on the booleans.
- **Binding `ticket_id` into `build_registry` or into `app.state`:** cross-run race (Pattern 4).
- **Changing `_execute_guarded`'s return arity:** breaks `mcp_server.call_mcp_tool`.
- **`async with` (semaphore, limiter, drain tracker) wrapped around the agent loop:** the loop suspends at every `yield`; contextvar-based state leaks across concurrent runs and corrupts the OTel span tree. `src/relay/agent.py:83-85` carries this warning explicitly.
- **API key in a query string:** lands in Fly proxy logs, browser history, referrers.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Sliding/moving window accounting with correct reset times | Custom deque-of-timestamps bucket | `limits` `MovingWindowRateLimiter` + `WindowStats.reset_time` | Correct `Retry-After` requires knowing when the *oldest* entry in the window expires — the part hand-rolled buckets get wrong. Also gets you `test()` (peek without consuming) and `reset()` (test isolation) for free |
| Constant-time credential comparison | Manual XOR loop or `==` | `secrets.compare_digest` | Length-independent, compiled, audited |
| 401 + `WWW-Authenticate` challenge shape | Manual header assembly | `APIKeyHeader.make_not_authenticated_error()` (or copy its exact shape) | FastAPI 0.141.1 already implements RFC 9110's "401 MUST include WWW-Authenticate" with the `APIKey` challenge and a source comment citing the RFC |
| UTC-day boundary arithmetic | String slicing on `created_at`, or Python-side date math against local time | SQLite `date('now')` / `datetime('now','start of day')` | `datetime('now')` writes UTC and `date('now')` reads UTC — they agree by construction, so there is no timezone seam to get wrong. Verified empirically |
| Per-run cost accounting | New cost tracker | Existing `RunBudget` + `runs.cost_usd` | Already correct, already priced for cache reads/writes |
| SSE event framing for the new type | Special-case serialization for `guardrail` | Existing generic `f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"` | `src/relay/main.py:90` is already type-agnostic — a new `AgentEvent.type` needs **zero** serialization changes |

**Key insight:** the only genuinely novel logic in this phase is the tier/limit *policy table* and the `ticket_id` comparison. Everything else already exists in stdlib, FastAPI, `limits`, or the current codebase. If a plan task is writing more than ~30 lines of new algorithmic code, it is probably re-implementing something on this list.

## Common Pitfalls

### Pitfall 1: `secrets.compare_digest` raises `TypeError` on non-ASCII `str`

**What goes wrong:** A client sends `X-API-Key: kéy`. Starlette decodes headers as latin-1, so the value is a `str` containing a codepoint > 127. `secrets.compare_digest("kéy", settings.api_key)` raises `TypeError: comparing strings with non-ASCII characters is not supported` → an unhandled 500 on the auth path. [VERIFIED: executed]

**How to avoid:** always `.encode()` both sides. `secrets.compare_digest("kéy".encode(), "abc".encode())` returns `False` cleanly. [VERIFIED: executed]

**Warning signs:** a 500 (not 401) in logs from `auth.py`; any `compare_digest` call taking `str` arguments.

**Test:** send `X-API-Key` with a non-ASCII byte and assert 401, not 500.

### Pitfall 2: The unauthenticated-404 ordering flip breaks an existing test in a non-obvious way

**What goes wrong:** `tests/test_api.py::test_get_missing_ticket_404` asserts `client.get("/tickets/9999").status_code == 404`. Once `GET /tickets/{id}` is key-protected (D-07), the dependency runs *before* the handler, so an unauthenticated request now returns **401**, and the test fails with a confusing "expected 404 got 401".

**Why it happens:** the test's intent (missing ticket → 404) is still valid; only the precondition changed. It is easy to "fix" it by asserting 401, silently losing 404 coverage.

**How to avoid:** update it to send a valid key and keep asserting 404, then add a *separate* test asserting unauthenticated → 401. Both behaviors need coverage.

### Pitfall 3: A binding denial can regress the eval suite through `ended_without_action`

**What goes wrong:** `agent.py:184` sets `resolved_via` only when `not is_error and block.name in TERMINAL_TOOLS`. A rejected `send_reply` leaves `resolved_via = None`; if the model then stops (`stop_reason == "end_turn"`), the loop yields `error: ended_without_action`. `evals.py` computes `result.action_ok = outcome["action"] == case["expected_action"]` → the case fails, and the CI eval gate has a pass-rate threshold.

**Why it happens:** the denial is a genuinely new state transition through code written on the assumption that denials only came from dry-run policy, where `ended_without_action` was the *desired* outcome.

**How to avoid:** make the denial message explicitly recoverable so the model retries within the same run (D-10 already requires the run to continue). Phrase it as an instruction, not a refusal:
> `"ticket_id 99 is not this run's ticket. This run may only act on ticket 1. Retry with ticket_id=1."`

Include `expected_ticket_id` as a structured field so the model doesn't have to parse prose.

**Warning signs:** a spike of `error:ended_without_action` in `runs.outcome`; eval pass rate dropping on cases that previously passed.

**Test:** script a `FakeClient` that emits `send_reply(ticket_id=99)` then `send_reply(ticket_id=1)`; assert (a) no row in `replies` for ticket 99, (b) a `guardrail` event was emitted, (c) the run still reaches `resolution`. Asserting only (a) is the classic incomplete test.

### Pitfall 4: `Fly-Client-IP` is trusted blindly and becomes a rate-limit bypass

**What goes wrong:** reading `Fly-Client-IP` unconditionally means anyone hitting the app *not* through the Fly proxy (local runs, a future direct-IP path, a misconfigured deploy) can send an arbitrary value and mint a fresh rate-limit bucket per request.

**How to avoid:** gate on `settings.trust_proxy_header` (default `false`), set `RELAY_TRUST_PROXY = 'true'` in `fly.toml` `[env]`. Fall back to `request.client.host`, and to the literal `"unknown"` when `request.client` is `None` (it can be, e.g. under some ASGI test transports).

**Warning signs:** `/metrics` run count rising while no single IP shows repeated hits in logs; rate limits never triggering in production.

### Pitfall 5: The daily ceiling reads `runs`, but concurrent runs haven't written rows yet

Covered in Pattern 3. Restated as a pitfall because it is the difference between "$5/day cap" and "$5/day cap unless someone sends 20 requests at once." **Acceptance test:** admit N runs concurrently with the in-flight reservation active and assert the (N+1)th is refused with 503 before any `record_run` has fired.

### Pitfall 6: The module-level rate limiter leaks state across the whole pytest session

**What goes wrong:** `MemoryStorage` is process state. Every test that posts to `/tickets` or `/process` consumes from the same buckets. `tests/test_observability.py` alone makes 2 process calls and 2 create calls today, and the phase will add more. As the suite grows past the limit for whatever tier the tests authenticate as, tests start failing with 429 — and, worse, **failing based on test execution order**, which looks like flakiness rather than a fixture bug.

**How to avoid:** an autouse fixture in `tests/conftest.py` that resets storage before each test. `MemoryStorage` exposes `reset()` [VERIFIED: present in `dir(MemoryStorage)` with signature `(self) -> int | None`]. Expose a small `relay.ratelimit.reset_limits()` wrapper rather than reaching into library internals from tests. Alternatively (and additively), have the tests authenticate as the **owner** tier, whose limits are deliberately loose.

**Warning signs:** a test passes alone and fails in the full suite; failures move when tests are reordered; 429 in a test that never intended to test rate limiting.

### Pitfall 7: Acquiring a semaphore or limiter with `async with` around the agent loop

**What goes wrong:** `run_ticket` is an async generator that suspends at every `yield`. `src/relay/agent.py:83-85` documents why the run span is parented manually rather than made current: a context manager held across yields leaks into whatever coroutine runs in between. Any `async with semaphore:` / `async with limiter:` wrapped around the step loop has the same shape and will attach unrelated concurrent work to this run's OTel context.

**How to avoid:** increment/decrement explicit counters in the existing `try/finally` (which already correctly runs `run_span.end()`), or hold the reservation in the *handler* (`main.py`) rather than inside the generator.

**Warning signs:** `async with` newly appearing around `for _ in range(settings.max_agent_steps)`; span trees where one run's `tool.*` spans appear under another run.

### Pitfall 8: Fail-open when keys are unconfigured

**What goes wrong:** the natural dev-friendly design — "if `RELAY_API_KEY` is unset, allow everything" (this is what `.planning/research/ARCHITECTURE.md` sketches) — means a Fly deploy that forgets `fly secrets set RELAY_API_KEY` silently ships a wide-open, paid endpoint. That is the exact failure this phase exists to prevent, reintroduced as a config omission.

**How to avoid:** fail **closed**. With no keys configured, protected routes return 503 `{"error": "auth_not_configured"}`. The CI docker smoke job only curls `/health` (public), so it is unaffected [VERIFIED: read `.github/workflows/ci.yml`]. Ship dev placeholder keys in `.env.example` so local setup stays one copy command.

**This is not locked by CONTEXT.md** — flag it as a planner decision, with fail-closed as the recommendation.

**Warning signs:** any `if not settings.api_key: return "local"` branch.

## Code Examples

Patterns below are grounded in verified library behavior; adapt naming to `CONVENTIONS.md`.

### Example 1: Tier resolution and the auth dependency

```python
# src/relay/auth.py
import secrets
from typing import Literal

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from .config import settings

Tier = Literal["owner", "demo"]

_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_UNAUTHENTICATED = {"WWW-Authenticate": "APIKey"}  # matches FastAPI's own challenge


def resolve_tier(presented: str | None) -> Tier | None:
    """Constant-time tier lookup. Both comparisons always run so timing does
    not reveal which key was closer."""
    if not presented:
        return None
    candidate = presented.encode()  # non-ASCII str would raise TypeError
    is_owner = bool(settings.api_key) and secrets.compare_digest(
        candidate, settings.api_key.encode()
    )
    is_demo = bool(settings.demo_key) and secrets.compare_digest(
        candidate, settings.demo_key.encode()
    )
    if is_owner:
        return "owner"
    if is_demo:
        return "demo"
    return None


def require_tier(*allowed: Tier):
    """Dependency factory. 401 = unauthenticated; 403 = authenticated but
    not permitted for this surface."""

    def _dependency(presented: str | None = Security(_HEADER)) -> Tier:
        if not settings.api_key and not settings.demo_key:
            raise HTTPException(503, "auth is not configured on this deployment")
        tier = resolve_tier(presented)
        if tier is None:
            raise HTTPException(401, "missing or invalid API key",
                                headers=_UNAUTHENTICATED)
        if tier not in allowed:
            raise HTTPException(403, f"the {tier} key may not use this endpoint")
        return tier

    return _dependency
```

### Example 2: Rate limiter wiring

```python
# src/relay/ratelimit.py
import math
import time

from fastapi import HTTPException, Request
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

from .config import settings

_storage = MemoryStorage()          # safe to build outside a running loop (verified)
_limiter = MovingWindowRateLimiter(_storage)

LIMITS: dict[tuple[str, str], RateLimitItem] = {
    ("process", "demo"): parse(settings.demo_process_limit),   # "5/hour"
    ("process", "owner"): parse(settings.owner_process_limit),  # "60/hour"
    ("create", "demo"): parse(settings.demo_create_limit),      # "20/hour"
    ("create", "owner"): parse(settings.owner_create_limit),    # "120/hour"
}


def client_ip(request: Request) -> str:
    if settings.trust_proxy_header:
        forwarded = request.headers.get("fly-client-ip")
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


async def enforce(bucket: str, tier: str, request: Request) -> None:
    item = LIMITS[(bucket, tier)]
    ip = client_ip(request)
    if await _limiter.hit(item, bucket, tier, ip):
        return
    stats = await _limiter.get_window_stats(item, bucket, tier, ip)
    retry_after = max(1, math.ceil(stats.reset_time - time.time()))
    raise HTTPException(
        429,
        detail={
            "error": "rate_limited",
            "limit": str(item),
            "tier": tier,
            "retry_after_seconds": retry_after,
            "note": "This is a deliberate cost control on the public demo.",
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(item.amount),
            "X-RateLimit-Remaining": str(stats.remaining),
            "X-RateLimit-Reset": str(int(stats.reset_time)),
        },
    )


async def reset_limits() -> None:
    """Test hook — MemoryStorage is process-wide state."""
    await _storage.reset()
```

### Example 3: Daily spend circuit breaker

```python
# src/relay/ratelimit.py (continued)
import sqlite3
from datetime import UTC, datetime, timedelta

DAILY_SPEND_SQL = (
    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM runs"
    " WHERE created_at >= datetime('now', 'start of day')"
)

_reserved_usd = 0.0  # cost of runs admitted but not yet written to `runs`


def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(conn: sqlite3.Connection) -> float:
    return float(conn.execute(DAILY_SPEND_SQL).fetchone()[0]) + _reserved_usd


def enforce_daily_budget(conn: sqlite3.Connection) -> None:
    spent = spent_today(conn)
    if spent < settings.max_daily_cost_usd:
        return
    resets_at = next_utc_midnight()
    raise HTTPException(
        503,
        detail={
            "error": "daily_budget_exhausted",
            "spent_usd": round(spent, 4),
            "limit_usd": settings.max_daily_cost_usd,
            "resets_at": resets_at.isoformat(),
            "note": "The demo caps Claude spend at "
                    f"${settings.max_daily_cost_usd:.2f}/day. Resets at 00:00 UTC.",
        },
        headers={
            "Retry-After": str(int((resets_at - datetime.now(UTC)).total_seconds())),
        },
    )
```

`datetime('now','start of day')` is UTC and `created_at` is written with `datetime('now')` (also UTC), so the comparison is exact with no timezone conversion. [VERIFIED: executed against the real `db.SCHEMA`]

### Example 4: Composed gate applied to routes

```python
# src/relay/main.py
async def run_gate(request: Request, tier: str = Depends(require_tier("owner", "demo"))) -> str:
    """Ordered: auth (sub-dependency, resolved first) → daily budget → rate limit."""
    enforce_daily_budget(app.state.conn)
    await enforce("process", tier, request)
    return tier


@app.post("/tickets/{ticket_id}/process")
async def process_ticket(
    ticket_id: int,
    dry_run: bool = False,
    tier: str = Depends(run_gate),
) -> StreamingResponse:
    ...
```

Ordering rationale: the spend breaker is a *global* condition, so checking it first avoids burning a caller's per-IP token during a budget outage. FastAPI resolves sub-dependencies before the dependant, so `require_tier` runs before either check. [VERIFIED: dependency ordering executed against FastAPI 0.141.1]

### Example 5: `ticket_id` binding in the guard chain

```python
# src/relay/agent.py
def _execute_guarded(
    spec: ToolSpec | None,
    name: str,
    raw_input: dict[str, Any],
    policy: ToolPolicy,
    *,
    bound_ticket_id: int | None = None,
) -> tuple[str, bool]:                      # arity unchanged — MCP path unaffected
    if spec is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    denial = policy.denial_reason(spec.tier)
    if denial:
        return json.dumps({"error": denial, "denied_by": "policy"}), True
    try:
        validated = validate_tool_input(spec.input_model, raw_input)
    except ToolInputError as exc:
        return json.dumps({"error": str(exc)}), True

    supplied = validated.get("ticket_id")
    if bound_ticket_id is not None and supplied is not None and supplied != bound_ticket_id:
        return json.dumps({
            "error": (
                f"ticket_id {supplied} is not this run's ticket. This run may only"
                f" act on ticket {bound_ticket_id}. Retry with"
                f" ticket_id={bound_ticket_id}."
            ),
            "denied_by": "ticket_binding",
            "expected_ticket_id": bound_ticket_id,
            "supplied_ticket_id": supplied,
        }), True

    try:
        return spec.execute(**validated), False
    except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
        return json.dumps({"error": str(exc)}), True
```

```python
# src/relay/agent.py — inside run_ticket's tool_use branch
result, is_error = _execute_guarded(
    spec, block.name, block.input, policy, bound_ticket_id=ticket["id"]
)
payload = json.loads(result)                     # parse once, reuse for both events
if is_error and payload.get("denied_by") == "ticket_binding":
    span.set_attribute("relay.tool.binding_violation", True)
    logger.warning("guardrail.ticket_id_mismatch", extra={"ctx": {
        "ticket_id": ticket["id"],
        "tool": block.name,
        "supplied_ticket_id": payload["supplied_ticket_id"],
    }})
    yield AgentEvent(type="guardrail", data={
        "guard": "ticket_binding",
        "tool": block.name,
        "expected_ticket_id": ticket["id"],
        "supplied_ticket_id": payload["supplied_ticket_id"],
        "action": "denied",
    })
yield AgentEvent(type="tool_result", data={
    "tool": block.name, "result": payload, "is_error": is_error,
})
```

No change is needed in `main.py`'s SSE formatter — `f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"` is already type-agnostic. And `evals.extract_outcome` iterates event types with no `else` branch, so `guardrail` is silently ignored there. [VERIFIED: read both]

## Existing-Code Impact Inventory

Every file the phase must touch, and why.

| File | Change | Risk |
|------|--------|------|
| `src/relay/config.py` | Add `api_key`, `demo_key`, `max_daily_cost_usd=5.0`, `trust_proxy_header=False`, four limit strings; flip `mcp_allow_writes` to `False` | Low. `Settings` uses `extra="ignore"` so unknown env vars are harmless |
| `src/relay/auth.py` | NEW | — |
| `src/relay/ratelimit.py` | NEW | — |
| `src/relay/main.py` | Add `dependencies`/`Depends` to 3 routes; keep `/health`, `/metrics`, `/dashboard`, `/` public; add in-flight reservation `try/finally` around the stream | Medium — must not introduce middleware; must not move checks into `event_stream()` |
| `src/relay/agent.py` | `_execute_guarded` kwarg + binding check; `run_ticket` passes `ticket["id"]`, parses result once, emits `guardrail` | Medium — file flagged fragile in CONCERNS.md; do not add `async with` around the loop |
| `src/relay/models.py` | Extend the `AgentEvent.type` comment to list `"guardrail"` | None |
| `src/relay/mcp_server.py` | Docstring only (the `False` default inverts the meaning) | None |
| `.env.example` | Add `RELAY_API_KEY`, `RELAY_DEMO_KEY`, `RELAY_MAX_DAILY_COST_USD`; flip `RELAY_MCP_ALLOW_WRITES` to `false` | None |
| `fly.toml` | Add `RELAY_TRUST_PROXY = 'true'` to `[env]` | Low |
| `scripts/demo.sh` | Add `-H "X-API-Key: ${RELAY_DEMO_KEY:-<published key>}"` to both curls | Low — currently unauthenticated (`scripts/demo.sh` lines 8 and 20) |
| `README.md` | Publish the demo key (D-02/SEC-06), document the tiers, limits, $5/day cap, and the MCP opt-in | None |
| `pyproject.toml` | Add `"limits>=5.8,<6"` to `[project] dependencies` | Low |

### Test Impact — exactly which tests break

Baseline: **38 tests pass** on the current tree (`.venv/bin/python -m pytest -q` → `38 passed in 1.76s`). [VERIFIED: executed]

| Test | Currently | After | Fix |
|------|-----------|-------|-----|
| `test_api.py::test_health` | GET `/health` | unchanged (public) | none — **add an explicit assertion that no credentials are required**, so the Docker/CI health path can never silently regress |
| `test_api.py::test_create_and_fetch_ticket` | POST `/tickets`, GET `/tickets/{id}` unauthenticated | **401** | authed client fixture |
| `test_api.py::test_get_missing_ticket_404` | asserts 404 | **401** (dep runs before handler) | authed fixture, keep asserting 404; add a separate unauth→401 test (Pitfall 2) |
| `test_observability.py::_make_ticket` (helper, used by 2 tests) | POST `/tickets` | **401** | authed fixture |
| `test_observability.py::test_process_streams_and_records_run` | POST `/process` | **401**, then rate-limit accumulation | authed fixture + limiter reset |
| `test_observability.py::test_failed_run_recorded_with_error_outcome` | POST `/process` | same | same |
| `test_observability.py::test_metrics_empty_state`, `test_dashboard_served`, `test_root_redirects_to_dashboard` | public routes | unchanged | none |
| `test_guardrails.py` (11), `test_tools.py`, `test_evals.py` | call `run_ticket`/registry directly | unchanged — `bound_ticket_id` derives from `ticket["id"]` inside `run_ticket`, so **no call site changes** | none |
| `test_mcp.py` (6) | pass `ToolPolicy()` explicitly | unchanged | none |

**Recommended fixture shape** (`tests/conftest.py`):
```python
@pytest.fixture(autouse=True)
async def _reset_limits():
    from relay.ratelimit import reset_limits
    await reset_limits()
    yield


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from relay.config import settings
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "api_key", "test-owner-key")
    monkeypatch.setattr(settings, "demo_key", "test-demo-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    with TestClient(app, headers={"X-API-Key": "test-owner-key"}) as client:
        yield client
```
Setting the header on `TestClient` itself keeps the existing test bodies unchanged — only the fixture moves. Tests authenticate as **owner** (loose limits) so they don't fight the limiter; dedicated demo-tier tests live in `test_ratelimit.py`.

**Infrastructure verified unaffected:**
- Docker `HEALTHCHECK` calls `urllib.request.urlopen(.../health)` — `/health` stays public. [VERIFIED: read `Dockerfile`]
- CI docker job polls `curl -sf http://127.0.0.1:8000/health` with only `ANTHROPIC_API_KEY` set — passes as long as `/health` is public and the app still boots without `RELAY_API_KEY`. **Fail-closed auth must not raise at startup**, only per-request. [VERIFIED: read `.github/workflows/ci.yml`]
- `fly.toml` defines no `[[http_service.checks]]`, so the Fly proxy uses a TCP check — unaffected by auth either way. [VERIFIED: read `fly.toml`]

## Deployment & Runtime State Inventory

This is not a rename phase, but it introduces secrets and published values that live *outside* git.

| Category | Items | Action Required |
|----------|-------|-----------------|
| Stored data | None — no schema change. `runs` already has `cost_usd` and `created_at`; no new table, no migration, no backfill | None |
| Live service config | Fly secrets: `RELAY_API_KEY` and `RELAY_DEMO_KEY` must be set via `fly secrets set` **before** the deploy that adds fail-closed auth, or the live demo 503s | Ops step in the plan; add to README deploy notes |
| Live service config | `fly.toml` `[env]` gains `RELAY_TRUST_PROXY = 'true'` (in git, ships with deploy) | Config edit |
| OS-registered state | None — no cron, no scheduler, no systemd/launchd units in this project | None (verified: no scheduler config in repo) |
| Secrets / env vars | `.env` exists locally and is gitignored; developers must add the two new keys. `.env.example` is the migration vector | Update `.env.example`; note in README |
| Published values | The demo key is published in README **and** on the dashboard (D-02). The dashboard is `DASHBOARD_HTML` in `src/relay/main.py` — a Python string constant, so publishing there is a source edit, not a template edit (Phase 6 moves it to Jinja2) | Two edits; keep them in sync |
| Build artifacts | None — `pip install .` from `pyproject.toml`; no compiled assets, no lockfile to regenerate (none exists) | None |
| Rate-limit state | Intentionally ephemeral — in-memory, resets on every cold start. This is *why* the daily cap must come from SQLite (D-03) | Documented, not migrated |

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | Everything | ✓ | 3.14.6 local / 3.12 CI+Docker / `>=3.11` declared floor | — |
| `limits` | SEC-02 | ✗ (not yet installed) | 5.8.0 available on PyPI, installs cleanly, requires Python `>=3.10` | none needed — hand-rolled bucket if a blocker emerges |
| `fastapi` (`APIKeyHeader`) | SEC-01 | ✓ | 0.141.1 (401 + `WWW-Authenticate` behavior confirmed in this version) | — |
| `secrets`, `datetime`, `sqlite3` | SEC-01/03 | ✓ | stdlib | — |
| Existing `runs` table | SEC-03 | ✓ | schema already has `cost_usd` REAL + `created_at` TEXT UTC | — |
| pytest / pytest-asyncio | tests | ✓ | pytest 9.1.1, pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`) | — |
| PyPI network access | install | ✓ (verified via pip) | — | — |

**Missing dependencies with no fallback:** none
**Missing dependencies with fallback:** `limits` — trivially installable; a ~40-line hand-rolled bucket is the documented fallback if it fails CI on Python 3.12.

⚠️ **Version-drift note (not this phase's job, but visible from here):** the declared floors are far behind what is installed — `anthropic>=0.60` vs 0.120.2 installed, `mcp>=1.2` vs 2.0.0 installed, `pytest-asyncio>=0.23` vs 1.4.0 installed. Adding `limits` is a natural moment to also pin upper bounds, but a lockfile is explicitly a **v2 deferred** item in REQUIREMENTS.md. Do not expand scope; just add `"limits>=5.8,<6"` with the bound already present.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` → `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `.venv/bin/python -m pytest tests/test_auth.py tests/test_ratelimit.py -q` |
| Full suite command | `.venv/bin/python -m pytest -q` (baseline: 38 passed in 1.76s) |
| Lint gate | `ruff check src tests` (CI runs this before pytest) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SEC-01 | Unauthenticated `POST /tickets` → 401 with `WWW-Authenticate: APIKey` | integration | `pytest tests/test_auth.py::test_missing_key_returns_401_with_challenge -x` | ❌ Wave 0 |
| SEC-01 | Wrong key → 401; valid owner key → 201 | integration | `pytest tests/test_auth.py::test_invalid_key_401 tests/test_auth.py::test_valid_key_allows -x` | ❌ Wave 0 |
| SEC-01 | Non-ASCII key value → 401, not 500 (Pitfall 1) | unit | `pytest tests/test_auth.py::test_non_ascii_key_is_rejected_cleanly -x` | ❌ Wave 0 |
| SEC-01 | `require_tier("owner")` with a demo key → 403 | unit | `pytest tests/test_auth.py::test_tier_mismatch_returns_403 -x` | ❌ Wave 0 |
| SEC-01 | `/health`, `/metrics`, `/dashboard`, `/` stay public (protects CI + Docker healthcheck) | integration | `pytest tests/test_auth.py::test_public_routes_need_no_key -x` | ❌ Wave 0 |
| SEC-01 | `GET /tickets/9999` **with** a key still 404s (Pitfall 2) | integration | `pytest tests/test_api.py::test_get_missing_ticket_404 -x` | ✅ (needs update) |
| SEC-02 | 6th demo `/process` in an hour → 429 with `Retry-After` + `X-RateLimit-*` | integration | `pytest tests/test_ratelimit.py::test_demo_process_limit_429 -x` | ❌ Wave 0 |
| SEC-02 | 21st demo `POST /tickets` → 429 | integration | `pytest tests/test_ratelimit.py::test_demo_create_limit_429 -x` | ❌ Wave 0 |
| SEC-02 | Owner tier is not blocked at the demo threshold | integration | `pytest tests/test_ratelimit.py::test_owner_tier_looser -x` | ❌ Wave 0 |
| SEC-02 | `Fly-Client-IP` keys the bucket when `trust_proxy_header`; ignored when not | unit | `pytest tests/test_ratelimit.py::test_client_ip_source -x` | ❌ Wave 0 |
| SEC-03 | `runs` seeded past $5 today → `/process` returns 503 with `resets_at` | integration | `pytest tests/test_ratelimit.py::test_daily_budget_503 -x` | ❌ Wave 0 |
| SEC-03 | Yesterday's rows do not count toward today (UTC boundary) | unit | `pytest tests/test_ratelimit.py::test_daily_sum_is_utc_day_scoped -x` | ❌ Wave 0 |
| SEC-03 | Cold-start survival: a fresh app over the same DB file still 503s | integration | `pytest tests/test_ratelimit.py::test_budget_survives_restart -x` | ❌ Wave 0 |
| SEC-03 | In-flight reservation blocks a concurrent burst before `record_run` fires | integration | `pytest tests/test_ratelimit.py::test_in_flight_reservation -x` | ❌ Wave 0 |
| SEC-04 | Mismatched `ticket_id` writes nothing to the other ticket | integration | `pytest tests/test_guardrails.py::test_mismatched_ticket_id_is_denied -x` | ✅ file exists, test ❌ |
| SEC-04 | A `guardrail` event is emitted with expected/supplied ids | integration | `pytest tests/test_guardrails.py::test_binding_denial_emits_guardrail_event -x` | ✅ file exists, test ❌ |
| SEC-04 | Run **continues** and still resolves after the model retries (D-10, Pitfall 3) | integration | `pytest tests/test_guardrails.py::test_run_recovers_after_binding_denial -x` | ✅ file exists, test ❌ |
| SEC-04 | MCP path (`bound_ticket_id=None`) is unaffected | integration | `pytest tests/test_mcp.py -x` | ✅ |
| SEC-04 | Two concurrent runs never cross-write (registry race, Pitfall 1) | integration | `pytest tests/test_guardrails.py::test_concurrent_runs_do_not_cross_bind -x` | ❌ Wave 0 |
| SEC-05 | `Settings().mcp_allow_writes is False` by default | unit | `pytest tests/test_mcp.py::test_writes_disabled_by_default -x` | ✅ file exists, test ❌ |
| SEC-05 | `create_server` under default settings denies a write tool | unit | `pytest tests/test_mcp.py::test_default_server_is_read_only -x` | ✅ file exists, test ❌ |
| SEC-06 | Demo key succeeds where no key 401s, and hits the tighter limit first | integration | `pytest tests/test_auth.py::test_demo_key_works_with_tight_limits -x` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest tests/test_auth.py tests/test_ratelimit.py tests/test_guardrails.py -q` (< 5s)
- **Per wave merge:** `.venv/bin/python -m pytest -q && ruff check src tests`
- **Phase gate:** Full suite green (38 baseline + new tests, zero regressions) before `/gsd:verify-work`. The `evals.yml` workflow is the *separate* gate for Pitfall 3 — worth one manual `python -m relay.evals --limit 3` before declaring the phase done, since a binding denial that isn't recoverable regresses eval pass rate in a way the unit suite cannot see.

### Wave 0 Gaps

- [ ] `tests/test_auth.py` — covers SEC-01, SEC-06
- [ ] `tests/test_ratelimit.py` — covers SEC-02, SEC-03
- [ ] `tests/conftest.py` — autouse `reset_limits()` fixture + authed `client` fixture (blocks everything; Pitfall 6)
- [ ] `tests/test_api.py`, `tests/test_observability.py` — migrate to the authed fixture
- [ ] No framework install needed — pytest, pytest-asyncio, httpx already present

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V1 Encoding & Sanitization | yes (existing) | Pydantic models bound `subject`/`body` length; parameterized SQL everywhere (verified: every `conn.execute` in `tools.py`, `main.py`, `telemetry.py` uses `?` placeholders) |
| V2 Validation & Business Logic | **yes — core of SEC-04** | `validate_tool_input` (Pydantic) + server-side `ticket_id` binding. Business-logic control: model output is untrusted input |
| V3 Web Frontend Security | partial | Demo key published deliberately (D-02); no cookies, no CSRF surface (header-based auth is not ambient). No CORS configured — out of scope this phase |
| V6 Authentication | **yes — SEC-01** | `fastapi.security.APIKeyHeader` + `secrets.compare_digest` on bytes. Static env-var keys; rotation/OAuth explicitly out of scope per REQUIREMENTS.md |
| V7 Session Management | no | Stateless; no sessions, no tokens with lifetime |
| V8 Authorization | **yes — SEC-01 tiers, SEC-05** | `require_tier(*allowed)` deny-by-default; MCP write tier default-off |
| V10 OAuth & OIDC | no | Explicitly out of scope |
| V11 Cryptography | partial | No key derivation or storage — comparison only. Never hand-roll; `secrets.compare_digest` is the whole crypto surface |
| V12 Secure Communication | yes (existing) | `fly.toml` sets `force_https = true` — the key never traverses plaintext in production |
| V16 Security Logging | **yes — SEC-04** | Structured `logger.warning("guardrail.ticket_id_mismatch", extra={"ctx": {...}})`; must **not** log key material. Log the tier, never the presented key |
| V17 WebRTC | no | N/A |

### Known Threat Patterns for FastAPI + Claude-API agent + SQLite

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Unauthenticated cost amplification (each request → up to 10 Claude calls) | Denial of Service | API key + per-tier rate limit + **persistent** daily USD ceiling (the rate limit alone is insufficient on a scale-to-zero machine) |
| Indirect prompt injection via ticket body → cross-ticket write | Tampering / Elevation of Privilege | Server-side `ticket_id` binding at the single guard-chain choke point; reject + observable `guardrail` event |
| Timing side channel on key comparison | Information Disclosure | `secrets.compare_digest`; evaluate all tier comparisons unconditionally |
| Rate-limit bypass via forged `Fly-Client-IP` | Spoofing | Trust the header only when `RELAY_TRUST_PROXY` is set (i.e. only behind the Fly proxy) |
| Rate-limit bypass via IP rotation | Denial of Service | Per-IP limits cannot stop this by design — the **daily spend ceiling** is the actual control, which is why SEC-03 exists separately from SEC-02 |
| Rate-limit bypass via cold-start reset (`min_machines_running = 0`) | Denial of Service | Same — SQLite-backed daily cap survives restarts; in-memory buckets do not and are not expected to |
| Credential leakage via URL | Information Disclosure | Header only, never a query parameter; `Fly-Client-IP`/proxy logs record URLs |
| Secrets in logs | Information Disclosure | Log the resolved tier, never the presented key; the `JsonFormatter` dumps the whole `ctx` dict, so a stray key in `ctx` ships to stdout |
| Config-omission fail-open on deploy | Elevation of Privilege | Fail closed when no keys are configured (Pitfall 8) |
| MCP client gets destructive access by default | Elevation of Privilege | `mcp_allow_writes = False` default (SEC-05) |
| SQL injection | Tampering | Already mitigated — all queries parameterized; keep it that way in the new daily-spend query (it has no user input at all) |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `Fly-Client-IP` is the correct client-IP header behind the Fly proxy and is overwritten (not appended to) by the proxy | Pattern 2, D-06 | If Fly changed the header name, rate limits key on the proxy IP → the entire internet shares one bucket. Inherited from `.planning/research/STACK.md` (MEDIUM-HIGH). **Verify on the live deploy** by logging the resolved IP for one request post-deploy |
| A2 | Owner-tier `POST /tickets` at 120/hour and `GET /tickets/{id}` at 120/600 per hour are sensible | Pattern 2 limit table | Only D-04 (demo 5/20) and D-05 (owner ~60 process) are locked; the rest are my proposals. Too tight would block legitimate owner use — bias loose |
| A3 | Fail-closed when no keys are configured is the right default | Pitfall 8 | Not locked by CONTEXT.md. If the planner picks fail-open, local dev is easier but a Fly secret omission ships an open paid endpoint |
| A4 | Reserving `max_run_cost_usd` per in-flight run is the right concurrency mitigation (vs. a semaphore, vs. accepting the gap) | Pattern 3 | Over-reserving makes the $5 cap trip early under concurrency; the alternative is transient over-spend. Both are small at demo scale |
| A5 | `MovingWindow` over `FixedWindow` is worth the extra per-key memory | Alternatives | Negligible either way at this scale; fixed window would allow a 2× boundary burst |
| A6 | The `guardrail` event shape (`guard`/`tool`/`expected_ticket_id`/`supplied_ticket_id`/`action`) is what Phase 4 EVAL-02 and Phase 5/6 drill-down will want | Pattern 4 | A later reshape is an SSE contract change. Cheap to get right now; confirm the field names with the Phase 4/5 consumer intent |
| A7 | `MemoryStorage.reset()` fully clears all buckets (not just expired entries) | Pitfall 6 | If it only prunes, tests stay order-dependent. Signature verified (`(self) -> int | None`) but semantics not executed end-to-end — **verify in the first `test_ratelimit.py` test** |

## Open Questions (RESOLVED)

> All four questions were resolved by the planner on 2026-08-06 and locked into the phase plans. Resolutions are recorded inline below.

1. **RESOLVED (plan 01-03) — What triggers a 403? SEC-01 requires "401 vs 403 semantics are correct," but D-07 defines no owner-only route.**
   - **Resolution:** the recommendation was adopted. `require_tier(*allowed)` is implemented as a factory and applied as `require_tier("owner", "demo")` on all three protected routes; the 403 branch is unit-tested by calling `require_tier("owner")` directly with a demo key. No fake admin endpoint was created.
   - What we know: 401 = unauthenticated (missing/unknown key, MUST carry `WWW-Authenticate`); 403 = authenticated but not permitted. FastAPI's historic bug was returning 403 for *unauthenticated*, which 0.141.1 has fixed. Both tiers are permitted on all three protected routes, so 403 is currently unreachable through any route.
   - What's unclear: whether to invent an owner-only surface just to exercise 403.
   - **Recommendation:** implement `require_tier(*allowed)` as a factory (Example 1). Apply `require_tier("owner", "demo")` to all three routes today, and unit-test the 403 branch by calling `require_tier("owner")` directly with a demo key. This satisfies "semantics are correct" with real code coverage and zero invented endpoints. Do **not** create a fake admin route.

2. **RESOLVED (plans 01-03, 01-05) — Should `GET /tickets/{id}` be rate-limited at all, and should the demo tier see other visitors' tickets?**
   - **Resolution:** the recommendation was adopted. A loose read limit applies (120/hour demo, 600/hour owner), the enumeration exposure is documented as an accepted risk in the README threat-model paragraph, and per-tier ticket ownership is deferred to Phase 5/6.
   - What we know: D-07 puts it behind a key. It leaks `customer_email` and full ticket bodies of any ticket id, and the demo key is published (D-02), so effectively anyone can enumerate ticket bodies.
   - What's unclear: whether that matters for a demo seeded with fictional customers. Scoping demo reads to demo-created tickets needs a new `tickets` column — real scope creep, and Phase 5/6 territory.
   - **Recommendation:** apply a loose limit (120/hour demo) this phase, note the enumeration exposure in the README threat-model paragraph, and defer per-tier ticket ownership. Confirm with the user during planning.

3. **RESOLVED (plan 01-05) — Where exactly does the published demo key live on the dashboard?**
   - **Resolution:** the recommendation was adopted. The key is rendered from `settings.demo_key` at serve time rather than hardcoded, so README, dashboard, and the accepted key cannot drift.
   - What we know: D-02 says README *and* dashboard. The dashboard is currently a Python string constant (`DASHBOARD_HTML`, `src/relay/main.py:112`), and Phase 6 replaces it with Jinja2 templates.
   - **Recommendation:** add a single line to `DASHBOARD_HTML` this phase (cheap, and Phase 6 will move it anyway), sourced from `settings.demo_key` rather than hardcoded, so README and dashboard cannot drift.

4. **RESOLVED (plan 01-02) — Is `guardrail` the right event name, given Phase 5 will persist `run_events`?**
   - **Resolution:** the recommendation was adopted. `guardrail` is retained with a `guard` discriminator field inside `data`, so future guard types (e.g. RAG-04 citation validation) reuse the type rather than adding new ones.
   - Low risk, but the name becomes part of the SSE contract. `guardrail` reads well and is generic enough to carry future denials (policy denials, citation-validation failures in RAG-04). **Recommendation:** keep `guardrail` with a `guard` discriminator field inside `data` (as in Example 5) so future guard types don't need new event types.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| FastAPI `APIKeyHeader(auto_error=True)` raising **403** for a missing key (a long-standing complaint) | Raises **401** with `WWW-Authenticate: APIKey`, with a source comment citing RFC 9110 | Present in the installed 0.141.1 | SEC-01's "401 vs 403 semantics correct" is now the framework default rather than something to work around. Still use `auto_error=False` to control the message and to distinguish missing from invalid |
| `slowapi` as the reflexive FastAPI rate-limit answer | Use `limits` directly; `slowapi` remains self-declared alpha and is structurally incompatible with `StreamingResponse` | Ongoing | Avoids a 500 on the SSE endpoint |
| Rate limiting == spend control | Rate limiting (burst) and spend ceilings (aggregate, persistent) are separate controls with separate storage | — | SEC-02 and SEC-03 are deliberately two requirements, not one |
| Ad-hoc `X-RateLimit-*` headers | IETF `RateLimit-*` draft headers coexist with the de-facto `X-RateLimit-*` | Draft, not finalized | D-08 specifies `X-RateLimit-*` — the widely-understood form. No change needed |

**Deprecated/outdated:**
- `BaseHTTPMiddleware` for anything touching streaming responses — Starlette documents the incompatibility and has been moving away from it.
- The `.planning/research/ARCHITECTURE.md` "rebind, don't reject" recommendation for `ticket_id` — superseded by CONTEXT.md D-09.
- The `.planning/research/ARCHITECTURE.md` "unset key => open, for local dev" auth sketch — see Pitfall 8; recommend fail-closed instead.

## Sources

### Primary (HIGH confidence)
- **Executed code** in an isolated venv against `limits` 5.8.0 — `MemoryStorage` import path and out-of-loop construction, `MovingWindowRateLimiter.hit/test/get_window_stats` signatures and return values, `WindowStats(reset_time: float, remaining: int)`, `parse("5/hour").key_for(...)` key composition, `MemoryStorage.reset()` presence
- **Executed code** against the project's own `.venv` (FastAPI 0.141.1) — route `dependencies` ordering and short-circuit, 401 + `WWW-Authenticate` propagation, dict `HTTPException.detail` serialization, custom response headers
- **Executed SQL** against `relay.db.SCHEMA` — `datetime('now')` and `date('now')` are UTC, `created_at >= date('now')` correctly scopes to the UTC day, `COALESCE(SUM(cost_usd), 0)` returns today's spend
- **Executed** `.venv/bin/python -m pytest -q` — 38-test green baseline
- **Executed** `secrets.compare_digest` with non-ASCII `str` → `TypeError`; with `bytes` → clean `False`
- **Read from installed source** — `fastapi/security/api_key.py` `APIKeyBase.make_not_authenticated_error()` (401 + `WWW-Authenticate: APIKey`, RFC 9110 citation in the docstring)
- **Read from repo source** — `src/relay/{agent,main,guardrails,tools,db,config,models,telemetry,mcp_server,evals}.py`, `tests/*`, `Dockerfile`, `fly.toml`, `.github/workflows/ci.yml`, `scripts/demo.sh`, `.env.example`, `pyproject.toml`
- `raw.githubusercontent.com/alisaifee/limits/master/limits/{aio/storage/__init__.py,util.py,strategies.py,limits.py}` — `MemoryStorage` in `__all__`, `WindowStats` NamedTuple definition, strategy class list, `RateLimitItem` subclasses
- `slopcheck install limits` → `[OK] limits (pypi)`
- `.planning/research/{ARCHITECTURE,STACK,PITFALLS}.md`, `.planning/codebase/CONCERNS.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `CLAUDE.md`

### Secondary (MEDIUM confidence)
- `limits.readthedocs.io/en/stable/async.html` and `/api.html` — narrative confirmation of the async namespace (note: the API-reference page's rendering does **not** list async `MemoryStorage`; the source and runtime both do, so the docs page is incomplete, not the library)
- `pypi.org/pypi/limits/json` — 5.8.0, `requires_python >=3.10`, dependency list
- `fly.io/docs/networking/request-headers/` via `.planning/research/STACK.md` — `Fly-Client-IP` semantics

### Tertiary (LOW confidence)
- Owner-tier limit values for `POST /tickets` and `GET /tickets/{id}` — my proposals, not sourced (see Assumptions A2)

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — the single new dependency was installed, imported, executed, and slopchecked; the rest is stdlib and already-installed FastAPI
- Architecture / control placement: **HIGH** — dependency ordering and streaming-status constraints verified by execution; placement rationale corroborated across three milestone research docs
- SQL / UTC semantics: **HIGH** — executed against the project's real schema
- Test-impact inventory: **HIGH** — every listed test was read; the 38-test baseline was executed
- Pitfalls: **HIGH** for codebase-derived ones (read from source), **MEDIUM** for the `Fly-Client-IP` trust boundary (documented but not observed on a live Fly request)
- Limit values / 403 trigger / fail-closed default: **MEDIUM** — reasoned proposals where CONTEXT.md is silent; see Assumptions Log and Open Questions

**Research date:** 2026-08-06
**Valid until:** 2026-09-05 (30 days — `limits` 5.x and FastAPI's auth surface are stable; re-verify if `limits` 6.0 ships or FastAPI majors)

---
*Phase 1 research for: Relay — Remaster*

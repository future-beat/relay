# Architecture Research

**Domain:** Production hardening + retrieval + live UI for an existing FastAPI/SSE AI agent service (brownfield)
**Researched:** 2026-08-06
**Confidence:** MEDIUM-HIGH (integration seams verified against the actual codebase; external-library claims verified against official docs; a few opinionated calls are marked MEDIUM)

## Standard Architecture

### System Overview

Target shape after this milestone. **Bold** = new or substantially changed.

```
                          ┌──────────────────────────────────┐
   browser / curl ───────▶│      HTTP edge (main.py)         │
   MCP client ──┐         │  route deps, not middleware:     │
                │         │  ┌────────────┐ ┌──────────────┐ │
                │         │  │ **auth.py**│ │**ratelimit** │ │
                │         │  │ API-key dep│ │ bucket+cap   │ │
                │         │  └─────┬──────┘ └──────┬───────┘ │
                │         └────────┼───────────────┼─────────┘
                │                  │ protected     │ public read-only
                │        ┌─────────▼──────┐   ┌────▼────────────────┐
                │        │ POST /tickets  │   │ GET /metrics        │
                │        │ POST /:id/     │   │ GET **/events**(SSE)│
                │        │      process   │   │ GET /dashboard      │
                │        └───────┬────────┘   └────▲────────────────┘
                │                │                 │ projection only
                │                ▼                 │
                │        ┌──────────────────┐  ┌───┴───────────────┐
                │        │ agent.run_ticket │─▶│ **events.py**     │
                │        │ (unchanged loop) │  │ RunBroadcaster    │
                │        │ + **RunContext** │  │ fan-out queues    │
                │        └───────┬──────────┘  └───────────────────┘
                │                │ await to_thread(_execute_guarded)
                │        ┌───────▼──────────────────────────────┐
                └───────▶│ guardrails: ToolPolicy, RunBudget,   │
                         │ validate_tool_input,                 │
                         │ **bind_run_context (ticket_id)**     │
                         └───────┬──────────────────────────────┘
                                 ▼
                         ┌──────────────────────┐
                         │ tools.py (sync)      │
                         │ registry unchanged   │
                         └───┬──────────────┬───┘
                             │              │
              ┌──────────────▼───┐   ┌──────▼─────────────────────┐
              │ **db.Database**  │   │ **retrieval.Retriever**    │
              │ conn + Lock,     │   │ loads kb/index.json,       │
              │ WAL, busy_timeout│   │ embeds query via Voyage,   │
              │ sync API,        │   │ cosine rank,               │
              │ .run() offload   │   │ keyword fallback           │
              └────────┬─────────┘   └──────┬─────────────────────┘
                       ▼                    │            ▲
                 relay.db (SQLite)          │            │ built offline
                                            ▼            │
                                   Voyage API      **index_kb.py** CLI
                                   (query only)     kb/*.md → kb/index.json
```

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|------------------------|
| **auth.py** | Decide whether a caller may invoke a costly/mutating route | FastAPI `Security` dependency over `APIKeyHeader`, `secrets.compare_digest` against `settings.api_key`; returns the key id for downstream rate-limit keying |
| **ratelimit.py** | Cap aggregate Claude spend across three axes | In-process token bucket per key/IP + `asyncio.Semaphore` for concurrent runs + daily USD cap read from `runs` |
| **db.Database** | Own the SQLite connection, make it safe from any thread, expose an async offload seam | `sqlite3.Connection` + `threading.Lock`, WAL + `busy_timeout`, `.run(fn)` = `asyncio.to_thread` |
| **RunContext** (guardrails) | Carry per-run server-side truth (the real `ticket_id`) into tool execution | Frozen dataclass passed alongside `ToolPolicy`; overrides model-supplied `ticket_id` after validation |
| **retrieval.Retriever** | Turn a query string into ranked KB chunks | Loads a committed index artifact at startup; one Voyage embed call per query + pure-Python cosine; degrades to keyword scoring |
| **index_kb.py** | Build the embedding artifact offline | CLI: chunk `kb/*.md` by heading → Voyage `input_type="document"` → `kb/index.json` with a KB content hash |
| **events.RunBroadcaster** | Fan one run's events out to N dashboard subscribers | Dict of bounded `asyncio.Queue`s, drop-oldest on backpressure, publishes a *projection* not the raw `AgentEvent` |
| **Dashboard** | Render metrics + live feed with no build step | Jinja2 template + static JS/CSS shipped inside the package; `EventSource("/events")` + polled `/metrics` |

## Recommended Project Structure

```
src/relay/
├── main.py            # routes only; dashboard HTML string removed
├── auth.py            # NEW  api_key dependency
├── ratelimit.py       # NEW  TokenBucket, run semaphore, daily spend cap
├── events.py          # NEW  RunBroadcaster (in-process pub/sub)
├── retrieval.py       # NEW  index load + query embed + rank + fallback
├── index_kb.py        # NEW  offline CLI (python -m relay.index_kb)
├── db.py              # CHANGED  Database wrapper (lock, WAL, .run offload)
├── guardrails.py      # CHANGED  + RunContext / bind_run_context
├── agent.py           # CHANGED  one seam: await to_thread(_execute_guarded)
├── tools.py           # CHANGED  search_docs takes Retriever; executors stay sync
├── telemetry.py       # CHANGED  aggregate in SQL, not by loading all rows
├── templates/
│   └── dashboard.html # NEW  Jinja2
└── static/
    ├── dashboard.css  # NEW
    └── dashboard.js   # NEW
kb/
├── *.md
└── index.json         # NEW  committed embedding artifact
```

### Structure Rationale

- **Keep the flat module layout.** The package is 1.4k lines across 12 modules; introducing `api/`, `services/`, `retrieval/` subpackages now would be more restructuring than the milestone justifies and would fight the existing `test_<module>.py` 1:1 convention. Five new flat modules is the consistent move.
- **`templates/` and `static/` live *inside* `src/relay/`.** Hatchling's `packages = ["src/relay"]` ships every file under the package dir, so the assets land in the wheel automatically. Put them at repo root instead and you must edit the Dockerfile (which only `COPY`s `src` and `kb`) — a silent 500 in production, working locally.
- **`kb/index.json` lives next to the markdown it describes.** The Dockerfile already does `COPY kb ./kb`, so the artifact ships with zero build changes, and `search_docs`'s existing `kb_dir.glob("*.md")` won't pick it up.
- **`auth.py` / `ratelimit.py` separate from `main.py`.** They need their own test files, and keeping `main.py` as pure routing preserves the readability that is this project's selling point.

## Architectural Patterns

### Pattern 1: Auth and rate limiting as route dependencies, never as `BaseHTTPMiddleware`

**What:** Enforce at the FastAPI dependency layer (`dependencies=[Security(require_api_key), Depends(limit_runs)]`), not in an HTTP middleware.

**When to use:** Any service where at least one route returns a `StreamingResponse`. That is exactly this service.

**Trade-offs:** You must remember the dependency on each protected route (mitigate with a shared `PROTECTED = [Security(...), Depends(...)]` list). In exchange you get three things middleware can't give you:

1. **No streaming interference.** Starlette explicitly documents that `BaseHTTPMiddleware` should not be used with `StreamingResponse` endpoints; the project has been moving toward deprecating it partly for this reason. Wrapping `/tickets/{id}/process` in a `BaseHTTPMiddleware` risks buffering or breaking the SSE stream — the single most valuable thing in the demo.
2. **Per-route granularity.** `/metrics`, `/events`, `/dashboard`, `/health` stay public read-only; `POST /tickets` and `/process` are protected. Middleware forces a path-prefix allowlist, which is a bug factory.
3. **Correct status codes.** Dependencies run *before* the handler returns the `StreamingResponse`, so a 401/429 is a real HTTP status. Once the SSE generator has started, the status line is already flushed — an auth failure raised inside `event_stream()` can only be reported as an SSE `error` frame on a 200 response.

**Example:**
```python
# auth.py
_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(key: str | None = Security(_header)) -> str:
    if not settings.api_key:              # unset => open, for local dev
        return "local"
    if not key or not secrets.compare_digest(key, settings.api_key):
        raise HTTPException(401, "invalid or missing API key")
    return "primary"

# main.py
@app.post("/tickets/{ticket_id}/process",
          dependencies=[Security(require_api_key), Depends(limit_runs)])
async def process_ticket(...) -> StreamingResponse: ...
```

**Browser consequence:** the dashboard must not call a protected route with `EventSource`, because `EventSource` cannot set request headers — a well-known, unfixable limitation of the API. Two clean resolutions, both used together here:
- The dashboard's live feed subscribes to **public, read-only `/events`** (a projection of runs, no secrets) via `EventSource`. No auth needed, no header problem.
- A "trigger a run" button (if built) uses `fetch()` + `ReadableStream` to read SSE, which *can* set `X-API-Key`.

Do **not** solve this by accepting the API key as a query parameter: query strings land in access logs, proxy logs, and browser history.

### Pattern 2: Three-tier spend limiting, not one request-rate limiter

**What:** Requests-per-minute is the wrong unit when one request can cost $0.50. Layer three limits with different jobs:

| Tier | Mechanism | Guards against |
|------|-----------|----------------|
| Per-caller burst | Token bucket keyed by API key, falling back to client IP | One script hammering `/process` |
| Global concurrency | `asyncio.Semaphore(N)` acquired for the life of the SSE stream | N parallel runs each holding a Claude connection on a 512 MB machine |
| Daily budget | `SELECT SUM(cost_usd) FROM runs WHERE created_at > date('now')` vs `settings.max_daily_cost_usd` | Slow, distributed abuse that never trips the bucket |

The existing `RunBudget` caps *one* run; these cap the *aggregate*, which is the actual exposure noted in CONCERNS.md.

**When to use:** Any endpoint whose cost is denominated in dollars rather than CPU.

**Trade-offs:** In-process state means limits reset on machine restart — and with `min_machines_running = 0` and `auto_stop_machines = 'stop'`, this machine stops when idle. The token bucket therefore resets on every cold start. That is acceptable for burst protection (a burst implies the machine is awake) but it means **the daily cap must be derived from SQLite, not from memory** — SQLite is on a mounted volume and survives restarts. This is the one place where persistence matters, and it is why the spend cap reads the `runs` table.

**Build vs. buy:** `slowapi` is the standard FastAPI answer and is a reasonable choice. Recommendation is a ~40-line `TokenBucket` in `ratelimit.py` instead, for three reasons: it must be a dependency rather than middleware anyway (Pattern 1); the semaphore and spend-cap tiers have no library equivalent, so a dependency is being written regardless; and a visible, tested hand-rolled limiter is on-theme for a project whose thesis is "the hand-written loop is the point." (Opinion, MEDIUM confidence — `slowapi` would also work; the choice is stylistic, not technical.)

### Pattern 3: Async-safe SQLite via a locked connection plus a single offload seam

**What:** Wrap the connection in a `Database` object that is safe to call from any thread, and offload at exactly one place in the agent loop.

```python
# db.py
class Database:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()

    def execute(self, sql, params=()):           # sync, safe from any thread
        with self._lock:
            return self._conn.execute(sql, params)

    async def run(self, fn, *a, **kw):           # async offload
        return await asyncio.to_thread(fn, *a, **kw)
```

```python
# agent.py — the only new await in the loop
result, is_error = await asyncio.to_thread(
    _execute_guarded, spec, name, raw_input, policy, ctx
)
```

**Why not `aiosqlite`:** `aiosqlite` is the more conventional answer and works well — it proxies a real connection onto one dedicated thread with a serialized request queue, which is genuinely close to what the lock above achieves. The problem is contract ripple: `ToolSpec.execute` is `Callable[..., str]` and `_execute_guarded` is a plain sync function. Going `aiosqlite` makes every executor `async`, which forces `async` through `tools.py`, `agent.py`, `mcp_server.py`, and `evals.py`, and makes `tests/test_tools.py` async. Offloading the whole guarded call to a thread instead touches **one line in `agent.py`** and leaves the tool registry synchronous, uniform, and trivially testable. (Opinion, MEDIUM-HIGH confidence. Revisit if a Postgres migration ever happens — then `aiosqlite`/`asyncpg`-style async-all-the-way is correct.)

**Non-negotiable regardless of driver:** WAL mode plus `busy_timeout`. Lack of WAL is the standard cause of `database is locked` under concurrency, and the shared-connection-without-locking issue in CONCERNS.md gets *worse*, not better, once tool execution moves onto worker threads.

### Pattern 4: Server-side binding of run-scoped identity into tool inputs

**What:** After Pydantic validation and before execution, overwrite any `ticket_id` in the validated input with the run's authoritative id.

```python
# guardrails.py
@dataclass(frozen=True)
class RunContext:
    ticket_id: int

def bind_run_context(validated: dict, ctx: RunContext) -> tuple[dict, str | None]:
    supplied = validated.get("ticket_id")
    if supplied is None:
        return validated, None
    if supplied != ctx.ticket_id:
        return {**validated, "ticket_id": ctx.ticket_id}, (
            f"ticket_id {supplied} rebound to {ctx.ticket_id}")
    return validated, None
```

**Where it goes:** in `_execute_guarded`, immediately after `validate_tool_input`. That function is already the single choke point every tool call passes through — for the HTTP loop *and* the MCP server. Putting the binding anywhere else (per-executor, per-schema) means five places to get right instead of one.

**Design calls:**
- **Keep `ticket_id` in the tool schemas.** Removing it changes what the model sees, which perturbs the prompt, the eval suite, and the MCP tool surface — all of which the milestone constraints say to keep compatible. Accept the argument, ignore its value.
- **Rebind rather than reject.** A hard error would abort runs on a harmless model slip. Rebinding is silent to the customer and loud to the operator.
- **Make the rebind observable.** Emit the mismatch as a span attribute and a structured log line (`tool.ticket_id_rebound`), and surface it on the dashboard. A prompt-injection attempt that gets neutralised is a *feature demo*, not a swallowed warning.
- **MCP is a different threat model.** There is no "current run" over stdio, so binding is not applicable there; MCP's protection is `mcp_allow_writes` defaulting to `False`.

**Test that must exist:** CONCERNS.md flags "cross-ticket `ticket_id` mismatch is untested" as High priority. A test asserting `send_reply(ticket_id=99)` during a run on ticket 1 mutates ticket 1 is the acceptance criterion for this work.

### Pattern 5: Embedding index as a committed build artifact, not a startup job

**What:** `python -m relay.index_kb` reads `kb/*.md`, chunks by heading, calls Voyage with `input_type="document"`, and writes `kb/index.json`. That file is committed and baked into the image. At startup the app *loads* it — it never *builds* it.

**Artifact shape:**
```json
{ "model": "voyage-4-lite", "dim": 512, "kb_sha256": "…",
  "built_at": "2026-08-06T…",
  "chunks": [ {"doc": "billing.md", "heading": "Refunds",
               "text": "…", "vec": "<base64 float32>"} ] }
```

**Why offline wins here, decisively:**

| | Build at startup | Committed artifact |
|---|---|---|
| Cold start | Adds N Voyage calls to every boot — and `min_machines_running = 0` + `auto_stop_machines = 'stop'` means boots are *frequent* | Load + parse a ~40 KB file |
| Failure mode | Voyage outage or missing key at boot = broken deploy | Voyage outage affects queries only; index still serves |
| CI / tests | Every test run needs a live Voyage key, or elaborate mocking | Artifact is a fixture; CI stays key-free (a stated project constraint) |
| Cost | Re-embeds the same 3 files on every restart | Embed once per KB edit |
| Reproducibility | Index silently varies with model version drift | Model + dim + hash pinned in the file |

**Guard against staleness:** the artifact stores `kb_sha256` over the concatenated markdown. At startup, recompute and compare. On mismatch, log `ERROR index.stale` and fall back to keyword scoring rather than serving a wrong index silently. Add a CI test that fails when `kb/` and `kb/index.json` disagree — this is the forcing function that keeps "edit a doc, forget to rebuild" from shipping.

**Ranking implementation:** for a 3-file KB (~20-50 chunks after heading-level chunking), use a pure-Python dot product. 50 chunks × 512 dims is ~25k multiply-adds — tens of microseconds, and it keeps `numpy` (a ~15 MB wheel) off a 512 MB machine. `numpy` starts paying for itself somewhere around a few thousand chunks; `sqlite-vec` or a real vector DB somewhere around 10⁵. Neither threshold is remotely in view. (Opinion, MEDIUM confidence — the *conclusion* is safe, the exact thresholds are estimates.)

**Model choice:** `voyage-4-lite` at 512 dimensions — $0.02/M tokens with a 200M-token free tier, 32k context, and Matryoshka dimension support so 512 costs nothing in quality terms at this corpus size. Indexing this KB is a few thousand tokens; queries are ~20 tokens each. Total spend is effectively zero and lands inside the free tier indefinitely. (HIGH confidence — verified against Voyage's current model and pricing docs.)

### Pattern 6: Retriever degrades to keyword, never fails the run

**What:** `Retriever.search(query)` tries Voyage; on any exception, timeout, missing key, or stale index it falls back to the *existing* keyword scorer and returns results tagged with the mode used.

**Why this is structural, not defensive coding:** `search_docs` is called inside the agent loop. Without a fallback, a Voyage blip turns into a failed ticket run, a broken eval suite, and a broken CI (which has no Voyage key by design). The tool's JSON contract (`{"results": [...]}`) must not change — that keeps `evals/golden.jsonl` and the MCP surface backward compatible, as the milestone requires. Add a `"mode": "semantic" | "keyword"` field so the dashboard and eval reports can show which path ran.

Also cache query embeddings in a small LRU dict. Eval runs replay similar queries, and one saved round trip is ~100-300 ms off the loop.

### Pattern 7: Live feed as a broadcaster of projections, published from the edge

**What:** `events.RunBroadcaster` holds a dict of bounded `asyncio.Queue`s (one per dashboard subscriber). `GET /events` registers a queue and streams from it; `main.py`'s existing `event_stream()` publishes to it as each `AgentEvent` passes through.

**Two boundary decisions that matter:**

1. **Publish from `main.py`, not from `agent.py`.** The agent loop must stay ignorant of the dashboard. Keeping the broadcaster out of `agent.py` means `evals.py` and `mcp_server.py` are unaffected, the loop stays testable without a broadcaster fixture, and the fragile span/budget/yield interplay flagged in CONCERNS.md is not touched at all.
2. **Broadcast a projection, not the raw `AgentEvent`.** `/events` is public read-only. Raw events carry reply bodies, customer emails, and ticket text. Publish `{run_id, ticket_id, type, tool, tokens, cost_usd, ms}` — enough for a compelling live feed, nothing that leaks content. This also bounds frame size, which matters for slow subscribers.

**Backpressure:** bounded queue (say 100) with drop-oldest on overflow. A dashboard left open on a slow connection must never apply backpressure to an agent run. Drop frames, never block the producer.

**Charts without a build step:** the constraint is "no build pipeline," not "no JavaScript." Hand-drawn inline SVG (sparklines for latency/cost, bars for outcomes) computed from `/metrics` needs no dependency, no CDN, and no CSP relaxation. A vendored single-file chart lib in `static/` is the fallback if the charts get ambitious. Avoid a CDN `<script>` — it adds an uncontrolled third-party origin to a page that is the project's front door.

## Data Flow

### Request Flow — protected run

```
POST /tickets/1/process   X-API-Key: …
    ↓
Security(require_api_key) ──401──▶ reject before any handler code
    ↓ key id
Depends(limit_runs) ──429──▶ bucket empty / semaphore full / daily cap hit
    ↓
handler: Database.run(get_ticket) → RunContext(ticket_id=1)
    ↓
StreamingResponse(event_stream())
    ↓
agent.run_ticket ──▶ Claude ──▶ tool_use
    ↓
await to_thread(_execute_guarded):
      ToolPolicy → validate_tool_input → bind_run_context(ticket_id←1)
      → spec.execute(...) → Database (locked, WAL)
                          → Retriever → Voyage → cosine → chunks
    ↓ AgentEvent
event_stream(): yield SSE frame to caller  ──┐
                broadcaster.publish(project(event)) ──┐
    ↓                                                 │
record_run() → runs table                             │
```

### Live feed / dashboard flow

```
GET /dashboard  (public) ─▶ Jinja2 template + /static assets
        │
        ├─▶ EventSource("/events")  ◀── RunBroadcaster queue ◀── projections
        └─▶ fetch("/metrics") every 5s ─▶ telemetry.run_metrics (SQL aggregates)
```

### Key Data Flows

1. **Server-side identity flow:** `ticket_id` originates at the HTTP route, travels in `RunContext` alongside `ToolPolicy`, and *overwrites* the model's value at the guard chain. Identity flows outward from the server; it never flows inward from the model.
2. **Index build flow (offline, out-of-band):** `kb/*.md` → `index_kb.py` → Voyage → `kb/index.json` → git → Docker image → memory at startup. The runtime is a pure consumer.
3. **Query flow (online):** query string → Voyage embed (`input_type="query"`) → cosine over in-memory vectors → top-k chunks → `search_docs` JSON. One external call, ~100-300 ms, cached, with a keyword fallback.
4. **Cost feedback loop:** `runs.cost_usd` is written by `record_run` at end of run, and *read* by the daily spend cap on the next request. The observability table becomes an enforcement input — a nice property worth calling out on the dashboard.

### State Management

| State | Lives in | Survives restart | Notes |
|-------|----------|------------------|-------|
| Tickets, runs, replies | SQLite on `/data` volume | Yes | Single writer, WAL |
| Embedding index | Process memory, loaded from disk | Rebuilt from artifact | Read-only after load |
| Token buckets | Process memory | No | Acceptable; burst-only guard |
| Run concurrency semaphore | Process memory | No | Correct — in-flight runs die with the process anyway |
| Daily spend total | Derived from SQLite | Yes | Deliberately not cached |
| Broadcaster subscribers | Process memory | No | Browsers reconnect; `EventSource` auto-retries |

## Scaling Considerations

| Scale | Architecture adjustments |
|-------|--------------------------|
| Demo traffic (the real target) | Everything above is sufficient. Single Fly machine, `min_machines_running = 0`. |
| ~10 concurrent runs | Raise the semaphore, keep WAL. `/metrics` must already be SQL-aggregated by then. |
| Multi-machine | Everything in-process breaks at once: buckets, semaphore, broadcaster fan-out, and the single SQLite volume. That is the Postgres + Redis line — explicitly out of scope, and correctly so. |

### Scaling Priorities

1. **First bottleneck — `/metrics`.** `run_metrics()` currently does `SELECT * FROM runs ORDER BY id` and aggregates in Python on every call. With a dashboard polling every 5 s across multiple open tabs, this is a full table scan per tab per 5 s, on the event loop. Fix during the dashboard work: move sums/counts into SQL, keep `last_runs` to a `LIMIT 20 ORDER BY id DESC`, and cache the result for a couple of seconds.
2. **Second bottleneck — SQLite write contention** once tool executors run on worker threads. WAL + `busy_timeout` + the connection lock handles it; the regression test is concurrent `/process` requests, which CONCERNS.md flags as an untested gap.
3. **Third — cold start.** Already why the index is a committed artifact. Keep startup work to: open DB, load JSON, build registry.

## Anti-Patterns

### Anti-Pattern 1: Auth as `BaseHTTPMiddleware` on a service with SSE

**What people do:** `app.add_middleware(AuthMiddleware)` because it feels like the "global" way to secure everything.
**Why it's wrong:** Starlette documents that `BaseHTTPMiddleware` should not be used with `StreamingResponse` endpoints and has been moving to deprecate it; it can buffer or break the stream. It also forces path allowlists to keep `/health` and `/dashboard` public, and it can't produce a correct status code for a response that has already started streaming.
**Do this instead:** Route dependencies (Pattern 1). If something genuinely must be global (request-id injection, say), write pure ASGI middleware, not `BaseHTTPMiddleware`.

### Anti-Pattern 2: API key in the SSE URL query string

**What people do:** `new EventSource("/tickets/1/process?api_key=…")` to route around `EventSource`'s inability to set headers.
**Why it's wrong:** Query strings are logged by proxies, servers, and browsers, and land in history and referrers. It converts a header secret into a broadly-replicated one.
**Do this instead:** Keep the browser-facing stream (`/events`) public and content-free, and use `fetch()` + `ReadableStream` for any authenticated stream the browser must initiate.

### Anti-Pattern 3: Building the embedding index in `lifespan`

**What people do:** "Just embed the KB at startup, it's only 3 files."
**Why it's wrong:** It couples boot to a third-party API on a machine that boots constantly (`min_machines_running = 0`), makes CI require a Voyage key (violating the no-API-calls-in-CI constraint), and makes retrieval quality drift silently with model updates.
**Do this instead:** Offline artifact + hash check + CI staleness test (Pattern 5).

### Anti-Pattern 4: Making `ToolSpec.execute` async to fix the blocking-DB problem

**What people do:** Reach for `aiosqlite` and propagate `async` through the tool registry.
**Why it's wrong:** It rewrites the tool contract, the MCP server, the eval harness, and the tool tests to solve a problem that is confined to one call site. The blast radius is an order of magnitude larger than the bug.
**Do this instead:** One `asyncio.to_thread` at the `_execute_guarded` seam + a thread-safe `Database` (Pattern 3).

### Anti-Pattern 5: Publishing raw `AgentEvent`s to a public feed

**What people do:** Wire the broadcaster straight into the agent loop and forward events verbatim.
**Why it's wrong:** It leaks reply bodies and customer data on an unauthenticated endpoint, couples the loop to the UI, and lets a slow browser exert backpressure on a paid Claude run.
**Do this instead:** Publish a bounded projection from the HTTP edge, over drop-oldest queues (Pattern 7).

### Anti-Pattern 6: Rejecting a mismatched `ticket_id` instead of rebinding it

**What people do:** Raise on mismatch, ending the run.
**Why it's wrong:** Turns a recoverable model slip into a failed ticket and a worse eval score, while the *actual* fix (never trust the value) is the same amount of code.
**Do this instead:** Rebind, log loudly, span-annotate, surface on the dashboard.

## Integration Points

### External Services

| Service | Integration pattern | Notes |
|---------|---------------------|-------|
| Voyage AI | Direct REST via `httpx` (already transitively present via `anthropic`) — `POST /v1/embeddings`, `input_type` = `document` at index time / `query` at query time | Adding the `voyageai` SDK for one endpoint is defensible but unnecessary; if retries/batching get fiddly, switch. Index-time batching matters little (≤50 chunks, limit is 1000/request). Set an explicit short timeout — this call sits inside the agent loop. MEDIUM confidence on the SDK-vs-httpx call. |
| Anthropic | Unchanged `AsyncAnthropic` in `app.state` | — |
| Fly.io | Unchanged single machine + volume | `--timeout-graceful-shutdown` is necessary but **not sufficient**: there are reported cases of SSE streams being killed on SIGTERM despite the flag. Track in-flight streams in an app-level set and await them in `lifespan` teardown *before* `conn.close()`, and stop accepting new runs once shutdown begins. The current code closes the DB while streams may still be running. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `main.py` ↔ `auth`/`ratelimit` | FastAPI dependency injection | One-way; limiters never import `main` |
| `main.py` ↔ `events` | Direct call to `broadcaster.publish(projection)` | `agent.py` must not import `events` |
| `agent.py` ↔ `guardrails` | `RunContext` passed with `ToolPolicy` | Same seam as existing policy — no new plumbing |
| `tools.py` ↔ `retrieval` | `Retriever` injected into `build_registry(db, kb_dir, retriever)` | Mirrors the existing closure-over-dependencies pattern; keeps `search_docs`'s output contract identical |
| `tools.py` ↔ `db` | `Database` object replaces the raw `sqlite3.Connection` | Executors stay sync; the offload happens above them |
| `mcp_server.py` ↔ everything | Builds its own `Database` and `Retriever` | Must be updated in lockstep with the `build_registry` signature — easy to forget, so it needs its own test |

## Suggested Build Order

Ordering is driven by (a) what is *currently exposed in production*, (b) which changes have the widest blast radius, and (c) which components consume others.

**1. Security perimeter** — `auth.py`, `ratelimit.py` (bucket + semaphore), `RunContext` binding, `mcp_allow_writes` default flip.
Rationale: the service is live and unauthenticated *right now*; every other item in this milestone makes the demo more attractive to abuse. These changes are small, mutually independent, and touch `main.py`/`guardrails.py` only. Nothing else depends on them, so they can't be blocked.
Caveat: the *daily spend cap* tier reads the `runs` table. Either accept a synchronous read here and convert it in step 2, or defer that tier to step 2. Recommend the former — a blocking `SELECT SUM(...)` is what the codebase already does everywhere.

**2. Async-safe data layer + graceful shutdown** — `Database`, WAL, `to_thread` seam, in-flight stream draining in `lifespan`.
Rationale: widest blast radius (`main`, `tools`, `telemetry`, `mcp_server`, `agent`, plus `conftest.py`). Do it before retrieval and dashboard add new call sites, or you refactor them twice. Shutdown draining belongs here because it is a `lifespan`/connection-teardown ordering problem, not a UI one. Add the concurrent-`/process` test here.

**3. Semantic retrieval** — `index_kb.py`, `kb/index.json`, `retrieval.py`, `search_docs` swap, CI staleness test, eval re-run.
Rationale: self-contained inside `tools.py` + two new modules, and it has an objective acceptance gate (the eval suite must not regress) that you want running against a stable substrate. Sits after step 2 so the retriever is written once against the final registry signature.

**4. Dashboard + live feed** — `events.py`, `/events`, Jinja2 template, static assets, `/metrics` SQL aggregation.
Rationale: the only component that *consumes* the others. It should surface rate-limit state, rebound-`ticket_id` events, and retrieval mode — all of which must exist first. It's also the most visible work, so it benefits from landing on a stable base. `/metrics` aggregation is fixed here because that's when polling load actually appears.

**Ordering alternative considered and rejected:** data layer first, security second. Defensible on pure-refactor grounds (fewest rewrites), but it leaves a paid Claude endpoint publicly open for the duration of a broad refactor. Security first wins on risk. The only cost is one small conversion of the spend-cap query in step 2.

**Parallelisable:** step 3 (retrieval) is largely independent of step 1 and touches almost nothing step 2 touches except `build_registry`'s signature. If work is split, retrieval can run alongside steps 1-2 with a single agreed signature.

## Sources

- [Starlette — Middleware](https://starlette.dev/middleware/) and [Deprecating BaseHTTPMiddleware (encode/starlette #1678)](https://github.com/Kludex/starlette/issues/1678) — HIGH: `BaseHTTPMiddleware` vs `StreamingResponse`
- [BaseHTTPMiddleware structure and StreamingResponse (discussion #2801)](https://github.com/Kludex/starlette/discussions/2801) — MEDIUM
- [Voyage AI — Text embedding models](https://docs.voyageai.com/docs/embeddings) — HIGH: model list, dimensions, `input_type`, batch limits
- [Voyage AI — Pricing](https://docs.voyageai.com/docs/pricing) — HIGH: $0.02/M for `voyage-4-lite`, 200M-token free tier
- [aiosqlite](https://github.com/omnilib/aiosqlite) — HIGH: one-thread-per-connection serialized queue model
- [Secure EventSource (SSE) authentication](https://openillumi.com/en/en-eventsource-auth-header-solution/) and [SSE with Authorization header (Yaffle/EventSource #44)](https://github.com/Yaffle/EventSource/issues/44) — MEDIUM: `EventSource` header limitation, fetch+ReadableStream workaround
- [Token bucket rate limiting with FastAPI](https://www.freecodecamp.org/news/token-bucket-rate-limiting-fastapi/) — MEDIUM: in-process bucket suitability for single-instance
- [Uvicorn settings](https://uvicorn.dev/settings/) and [`--timeout-graceful-shutdown` not behaving as expected (uvicorn #2098)](https://github.com/Kludex/uvicorn/discussions/2098) — MEDIUM: SSE streams may not drain on SIGTERM despite the flag
- Codebase: `src/relay/{main,agent,tools,guardrails,db,telemetry}.py`, `Dockerfile`, `fly.toml`, `.planning/codebase/{ARCHITECTURE,CONCERNS,STRUCTURE}.md` — HIGH

---
*Architecture research for: production hardening + retrieval + live dashboard on a FastAPI/SSE agent service*
*Researched: 2026-08-06*

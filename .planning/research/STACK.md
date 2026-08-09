# Stack Research

**Domain:** Hardening an existing single-instance FastAPI + SQLite + Claude API agent service (auth, rate limiting, async-safe DB, embeddings RAG, server-rendered live dashboard)
**Researched:** 2026-08-06
**Confidence:** HIGH (versions verified against PyPI/npm registry APIs and official docs on 2026-08-06; architectural recommendations MEDIUM-HIGH)

---

## Headline Recommendations

| Capability | Recommendation | Confidence |
|------------|----------------|------------|
| API-key auth | `fastapi.security.APIKeyHeader` + `secrets.compare_digest` (**zero new deps**) | HIGH |
| Rate limiting | `limits>=5.8` used directly via a FastAPI dependency (**not** slowapi) | MEDIUM-HIGH |
| Aggregate spend cap | Custom daily-cost guard querying the existing `runs` table + `asyncio.Semaphore` | HIGH |
| Async SQLite | `aiosqlite>=0.22` + WAL + `busy_timeout` + single writer connection | MEDIUM-HIGH |
| Embeddings client | **Raw `httpx` POST** to `api.voyageai.com/v1/embeddings` (**not** the `voyageai` SDK) | HIGH |
| Embedding model | `voyage-4-lite` @ `output_dimension=512` (fall back to `voyage-4` if recall is weak) | MEDIUM |
| Vector storage | `numpy` matrix loaded from a prebuilt `.npz` index file (**not** sqlite-vec) | HIGH |
| Dashboard shell | `jinja2>=3.1.6` via `Jinja2Templates` + vendored static assets | HIGH |
| Charts | Chart.js 4.5.x, vendored (**not** CDN-linked) | MEDIUM-HIGH |
| Live feed | Native `EventSource` + ~60 lines vanilla JS (**not** htmx SSE ext) | MEDIUM-HIGH |
| SSE transport | `sse-starlette>=3.4.8` — also solves graceful shutdown draining | HIGH |
| CSS | `@picocss/pico` 2.1.1 classless, vendored | MEDIUM |

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| `limits` | `>=5.8,<6` (latest 5.8.0, 2026-02-05) | Rate-limiting engine | This is the library slowapi is a thin wrapper around. It ships a first-class async namespace (`limits.aio.storage.MemoryStorage`, `limits.aio.strategies.MovingWindowRateLimiter`) so limits are checked without touching the event loop or a threadpool. Using it directly avoids slowapi's alpha-status decorator machinery and its awkward interaction with streaming responses. |
| `aiosqlite` | `>=0.22,<1` (latest 0.22.1, 2025-12-23) | Async SQLite driver | Each connection gets a dedicated worker thread with a serialized request queue, so no `await` ever blocks the loop. Crucially, it lets you hold a **long-lived, explicitly-owned writer connection** — which is what you need to fix the "shared `sqlite3.Connection`, no locking" concern. `asyncio.to_thread` alone would move blocking off the loop but leaves the shared-connection thread-safety problem unsolved. |
| `httpx` | `>=0.28` (latest 0.28.1) | Voyage embeddings HTTP client | **Already installed transitively** — `anthropic>=0.60` hard-depends on `httpx<1,>=0.25.0`. The Voyage embeddings API is a single POST with a JSON body. Promote httpx from a dev extra to a real runtime dependency and write ~25 lines. See "What NOT to Use" for why the official SDK is the wrong call here. |
| `numpy` | `>=2.3,<3` (do **not** pin `>=2.5`) | In-memory vector index + cosine similarity | The KB is 3 markdown files → on the order of 30-80 chunks. A single `(N, 512)` float32 matrix is ~160 KB. `mat @ q` returns all scores in microseconds. `numpy>=2.5` requires Python 3.12+, which breaks the project's declared `requires-python = ">=3.11"` floor; `2.4.6` is the newest release that supports 3.11. |
| `jinja2` | `>=3.1.6` (latest 3.1.6) | Server-side templating | FastAPI's `Jinja2Templates` requires it and it is not installed by the bare `fastapi` dependency (only by `fastapi[standard]`). Replaces the current `DASHBOARD_HTML` string constant in `main.py` with real templates + partials. No build step. |
| `sse-starlette` | `>=3.4.8` (latest 3.4.8, 2026-08-05) | SSE responses + graceful shutdown | `EventSourceResponse` hooks uvicorn's SIGTERM handler (`AppStatus.handle_exit`) and broadcasts a shutdown signal to every live stream, with a configurable `shutdown_grace_period`. This is a **direct, off-the-shelf solution to the "graceful shutdown draining for in-flight SSE runs" requirement** — do not hand-roll it. |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `fastapi.security.APIKeyHeader` | stdlib of `fastapi>=0.141` | API-key extraction + OpenAPI docs | Always for this milestone. Use as a `Security(...)` dependency so `/docs` shows the auth requirement. Compare with `secrets.compare_digest`, never `==`. |
| Chart.js | `4.5.1` (2025-10-13) | Cost / latency / outcome charts | The dashboard charts. UMD build works from a `<script>` tag with zero tooling; has line, bar, and doughnut out of the box, which covers cost-over-time, latency distribution, and outcome breakdown respectively. |
| `@picocss/pico` | `2.1.1` (classless build) | Dashboard styling | Gives a polished, dark-mode-aware look from semantic HTML alone — no class soup, no Tailwind build step. ~80 KB minified CSS, vendored. |
| `pytest-asyncio` | `>=1.4` (latest 1.4.0) | Async test support | Already in use (`asyncio_mode = "auto"`). Note this is a **major-version jump from the declared `>=0.23` floor** — pin an upper bound; 1.x changed default loop-scope semantics. |
| `ruff` | `>=0.5` | Lint | Unchanged. |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `uv` (or `pip-tools`) | Lockfile generation | CONCERNS.md flags floor-only pins with no lockfile as a live risk. This milestone adds ~5 dependencies — good moment to commit a `uv.lock` / `requirements.txt` so CI and the Fly image are reproducible. `anthropic` is now at **0.120.2** vs the declared `>=0.60` floor; an unpinned CI install already spans 60 minor releases. |
| Index-build script | Precompute Voyage embeddings at build time | A `scripts/build_index.py` that emits `kb/index.npz` (+ `kb/index.json` for chunk text/metadata). Run manually or in CI, commit the artifact. Keeps the container's cold start free of Voyage calls and makes retrieval work offline in tests. |
| `sqlite3` CLI / `PRAGMA integrity_check` | WAL verification | After enabling WAL, confirm `journal_mode=wal` persists on the Fly volume — WAL mode is a persistent database property, but it fails on some network filesystems. Fly volumes are local block devices, so this is fine. |

---

## Installation

```bash
# New runtime dependencies
pip install "limits>=5.8,<6" "aiosqlite>=0.22,<1" "httpx>=0.28,<1" \
            "numpy>=2.3,<3" "jinja2>=3.1.6,<4" "sse-starlette>=3.4.8,<4"
```

`pyproject.toml` additions:

```toml
dependencies = [
    # ... existing ...
    "limits>=5.8,<6",
    "aiosqlite>=0.22,<1",
    "httpx>=0.28,<1",        # promoted from dev extra; already transitive via anthropic
    "numpy>=2.3,<3",         # NOT >=2.5 — that requires Python 3.12+
    "jinja2>=3.1.6,<4",
    "sse-starlette>=3.4.8,<4",
]
```

Vendored frontend assets (no npm, no build step — download once, commit to `src/relay/static/`):

```bash
mkdir -p src/relay/static/vendor
curl -Lo src/relay/static/vendor/chart.umd.min.js \
  https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js
curl -Lo src/relay/static/vendor/pico.classless.min.css \
  https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.classless.min.css
```

Then `app.mount("/static", StaticFiles(directory=...), name="static")`.

---

## Decision Detail

### 1. Rate limiting — `limits` directly, not slowapi

`slowapi` 0.1.10 (2026-06-13) is alive but its own documentation still says: *"this is alpha quality code still, the API may change, and things may fall apart while you try it."* It has been on `0.1.x` since 2020. Its documented constraints are a poor fit here:

- The decorated endpoint **must** accept `request: Request`, and to get `X-RateLimit-*` headers it must also accept and return an explicit `Response` — clumsy when the handler returns an `EventSourceResponse`.
- Decorator ordering (`@app.post` above `@limiter.limit`) is a silent-failure footgun.
- It buys you nothing you can't get in ~30 lines, since `limits` does 100% of the actual work.

Recommended shape:

```python
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

storage = MemoryStorage()
limiter = MovingWindowRateLimiter(storage)
PROCESS_LIMIT = parse("5/minute")
```

as a FastAPI dependency keyed on **the API key first, client IP second**.

**Critical deployment detail:** on Fly.io the TCP peer is the Fly proxy, not the user. Read `Fly-Client-IP` (falling back to `request.client.host`). Keying on the proxy IP would rate-limit the entire internet as one bucket. This is a documented, commonly-hit Fly mistake.

**In-memory storage is correct here** — `min_machines_running = 0` and a single machine means there is no second process to share state with. Do **not** add Redis; it would cost more than the Claude calls you are protecting.

**Per-IP limits do not actually cap aggregate spend.** An attacker rotating IPs walks straight through. The requirement in PROJECT.md is "cap aggregate Claude API spend", so pair the rate limiter with two things that are not libraries:

1. **A global daily-cost guard** — `SELECT SUM(cost_usd) FROM runs WHERE created_at > date('now')`, refuse `/process` above a `RELAY_MAX_DAILY_COST_USD` ceiling. The `runs` table already records per-run cost, so this is nearly free.
2. **A concurrency cap** — a module-level `asyncio.Semaphore(2)` around the agent loop, bounding worst-case in-flight spend and protecting the 512 MB / shared-cpu-1x machine.

### 2. Async SQLite — `aiosqlite` + WAL, and *why* not just `to_thread`

Two separate concerns are conflated in CONCERNS.md:

| Concern | `asyncio.to_thread` | `aiosqlite` + WAL |
|---------|--------------------|-------------------|
| Blocking the event loop | Fixed | Fixed |
| Shared connection used from many threads | **Not fixed** | Fixed (connection is pinned to one owner thread with a serialized queue) |
| `database is locked` under concurrent writes | Not fixed | Fixed by WAL + `busy_timeout` |

`aiosqlite` gives you both for one dependency, and gives the codebase an honest `async def` DB layer instead of `to_thread` wrappers sprinkled through `tools.py`.

Required PRAGMAs on connect (all three matter):

```python
await conn.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer
await conn.execute("PRAGMA busy_timeout=5000")    # wait instead of instantly erroring
await conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, much faster fsync
```

Architecture: **one writer connection + a small read pool.** WAL permits many concurrent readers alongside a single writer; a second writer just produces `SQLITE_BUSY`. Serialize writes through one `aiosqlite.Connection` (or an `asyncio.Lock`) and let reads (dashboard, metrics) use separate connections. `journal_mode=WAL` is persistent on the DB file, so set it once at `init_db`.

Watch the Fly volume: WAL creates `relay.db-wal` and `relay.db-shm` next to `relay.db` in `/data`. That's fine on a Fly volume (local block device) but would break on a network FS.

### 3. Voyage embeddings — raw httpx, not the SDK

The `voyageai` 0.5.0 SDK's dependency list is the deciding evidence:

```
requests, aiohttp, tenacity, numpy, aiolimiter, pillow,
pydantic>=2.7.4, tokenizers>=0.14.0, langchain-text-splitters>=0.3.8
```

For a service that needs **one HTTP POST**, that pulls in a second HTTP stack (`requests` + `aiohttp` alongside the `httpx` `anthropic` already uses), a Rust-compiled `tokenizers` wheel, `pillow`, and — surprisingly — `langchain-text-splitters`. That is tens of megabytes of image size and a much larger CVE surface on a 512 MB scale-to-zero machine, for a project whose stated ethos is "no orchestration framework, the visible hand-written loop is a feature."

The SDK *does* expose `voyageai.AsyncClient` (verified in `voyageai/__init__.py` on `main`), so async is not the differentiator — dependency weight is.

The API is trivial:

```
POST https://api.voyageai.com/v1/embeddings
Authorization: Bearer $VOYAGE_API_KEY
{"input": [...], "model": "voyage-4-lite", "input_type": "query", "output_dimension": 512}
→ {"object":"list","data":[{"embedding":[...],"index":0}],"model":...,"usage":{"total_tokens":N}}
```

**Use `input_type` correctly — this is the single highest-leverage retrieval knob.** Voyage prepends a task-specific prompt: `input_type="document"` when building the index, `input_type="query"` at search time. Getting this wrong silently degrades recall.

**Model choice.** The voyage-4 family is current (voyage-3.x is now labelled legacy):

| Model | $/1M tok | Free tier | Notes |
|-------|----------|-----------|-------|
| `voyage-4-lite` | $0.02 | 200M | **Recommended.** 32K context, Matryoshka dims 256/512/1024/2048 |
| `voyage-4` | $0.06 | 200M | Upgrade path if recall on evals is weak |
| `voyage-4-large` | $0.12 | 200M | Overkill for a 3-file KB |
| `voyage-3.5` | $0.06 | none | Legacy — no free tier, don't pick it |

With a **200M-token free allowance**, this milestone's entire indexing + demo query volume is genuinely $0.00. Note that legacy voyage-3.x models have *no* free tier, so choosing "the model I already know" costs real money for worse quality. `output_dimension=512` (Matryoshka) halves index size and similarity cost with negligible quality loss at this corpus size.

Also note: default `max_retries=0` and `timeout=None` on the API path — set an explicit `httpx` timeout (~10s) and a small retry, and **degrade to the existing keyword scorer** if Voyage is unreachable, so the demo never hard-fails on a third-party outage.

Optional later: `rerank-2.5-lite` ($0.02/1M, 200M free) over the top-10 hits. Not needed for 3 documents.

### 4. Vector storage — numpy in-memory, not sqlite-vec

`sqlite-vec` 0.1.9 (2026-03-31) is maintained and got real DELETE/constraint support in 0.1.7, but it is the wrong tool at this scale, and there is a concrete blocker:

**Verified on this machine:**
```
sqlite version: 3.50.4
has enable_load_extension: False
```

The local Python 3.14.6 interpreter's `sqlite3` module is compiled **without extension-loading support**. `sqlite-vec` cannot load at all in the developer's environment, even though `python:3.12-slim` in the Docker image would allow it. That is a dev/prod parity trap that would make retrieval untestable locally and unverifiable in CI.

Combined with the scale argument — 3 markdown files, well under 100 chunks, a `(80, 512)` float32 matrix at ~160 KB — an ANN index is pure overhead. Brute-force `numpy` is exact, instantaneous, and has no extension-loading, no vtab schema, no migration.

**Recommended shape:** build `kb/index.npz` (L2-normalized float32 matrix) + `kb/index.json` (chunk text, source file, heading) with `scripts/build_index.py`, commit both, load once at startup into `app.state`. Search is `scores = mat @ q_norm; top = np.argsort(-scores)[:k]`.

This also fixes the separate performance concern that `search_docs` re-globs and re-reads every KB file on every tool call.

Revisit sqlite-vec only if the KB exceeds a few thousand chunks or needs incremental per-document updates without a rebuild.

### 5. Dashboard — Jinja2 + vanilla EventSource + vendored Chart.js

**Why not htmx's SSE extension**, despite it being the obvious 2026 answer: `htmx-ext-sse` (2.2.4) works by swapping **HTML fragments** carried in the SSE event data. Relay's SSE stream carries structured JSON (`text`, `tool_use`, `tool_result`, `usage`, `resolution`) and is consumed by programmatic API clients and the eval harness. PROJECT.md constrains the SSE event contract to stay backward compatible. Emitting HTML would either break that contract or force a second parallel stream. Native `EventSource` + `JSON.parse` + a `switch` on event type is ~60 lines, has zero dependencies, keeps one contract, and — for a portfolio piece where the code is read — is arguably more impressive than an attribute that hides the mechanism.

htmx remains a good optional addition for *non-stream* partial refreshes (paginated ticket table, filter controls) if the page grows. It is not needed for the live feed.

**Vendor, don't CDN.** Serving Chart.js and Pico from `/static` (a) keeps the live demo working if jsdelivr has an outage or is blocked, (b) removes third-party requests so a strict CSP is trivial, (c) avoids extra DNS/TLS handshakes on a cold-started machine where first-paint latency is already the weak point. The cost is ~350 KB in the repo.

**Chart.js over uPlot/ApexCharts:** uPlot 1.6.32 is smaller and faster but is a low-level time-series-only library — you'd hand-roll the outcome-breakdown chart. Chart.js 4.5.1 covers line + bar + doughnut with one vendored UMD file and a familiar config object.

**Graceful shutdown comes free with `sse-starlette`.** Set `EventSourceResponse(..., shutdown_grace_period=5.0)` and run uvicorn with `--timeout-graceful-shutdown 10` (grace period must be strictly less than uvicorn's timeout). Also move the `conn.close()` out of `lifespan`'s teardown until after streams drain, and record the `runs` row incrementally rather than only at stream completion, so a severed stream still leaves a row.

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `limits` direct | `slowapi` 0.1.10 | You want decorator ergonomics + automatic `X-RateLimit-*` headers and can live with alpha-status caveats and the `request`/`Response` signature requirements. Perfectly usable; just less clean around `EventSourceResponse`. |
| `limits` in-memory | `fastapi-limiter` 0.2.0 | You add a second machine / Redis. It is Redis-only, so it is a non-starter for a scale-to-zero single machine. |
| `aiosqlite` | `asyncio.to_thread` + WAL + connection-per-request | You want zero new dependencies. Viable, but you must still solve connection ownership yourself; the LOC saved is small. |
| `aiosqlite` | Postgres + `asyncpg` | Multi-machine. Explicitly out of scope per PROJECT.md and would end scale-to-zero economics. |
| Raw `httpx` | `voyageai` SDK 0.5.0 | You need multimodal/contextualized embeddings, built-in tokenizer-aware chunking, or the client-side rate limiter. None apply to a 3-file KB. |
| `voyage-4-lite` | `voyage-4` / `voyage-4-large` | Eval retrieval scores regress after the switch from keyword search. Change is one env var; make the model configurable. |
| numpy in-memory | `sqlite-vec` 0.1.9 | KB grows past a few thousand chunks, or you need incremental upserts. Requires verifying `enable_load_extension` on every target interpreter. |
| Vanilla `EventSource` | `htmx-ext-sse` 2.2.4 | You are willing to emit HTML fragments over SSE and give up a single JSON contract. |
| Vanilla `EventSource` | Datastar | Greenfield hypermedia app driven entirely by server-sent signals. Same HTML-over-SSE contract problem here. |
| Chart.js 4.5.1 | uPlot 1.6.32 | Thousands of points and render performance matters. Not the case for demo-scale metrics. |
| Pico.css 2.1.1 | Tailwind | Never here — Tailwind needs a build step, which PROJECT.md rules out. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `voyageai` SDK | Pulls `requests` + `aiohttp` + `tokenizers` + `pillow` + `langchain-text-splitters` for one POST; duplicates the httpx stack `anthropic` already ships | `httpx` (already transitively installed) |
| `sqlite-vec` | Verified: the local Python 3.14 `sqlite3` has `enable_load_extension = False`, so it cannot load in dev. Also 0.1.x alpha; pure overhead below ~1k vectors | `numpy` matrix from a prebuilt `.npz` |
| Redis (for rate limits or caching) | Single scale-to-zero machine — there is no shared state to coordinate. Would cost more than the Claude spend it guards | `limits.aio.storage.MemoryStorage` |
| Per-IP-only rate limiting as the spend control | Trivially bypassed by IP rotation; does not satisfy "cap **aggregate** spend" | Rate limit + required API key + daily cost ceiling from the `runs` table + `asyncio.Semaphore` |
| `numpy>=2.5` | Requires Python 3.12+; silently breaks the declared `requires-python = ">=3.11"` floor | `numpy>=2.3,<3` (2.4.6 is the newest 3.11-compatible release) |
| `voyage-3.5` / `voyage-3-large` | Legacy generation with **no free-token tier**, superseded by voyage-4 | `voyage-4-lite` (200M free tokens) |
| `==` for API-key comparison | Timing-attack vulnerable | `secrets.compare_digest` |
| `request.client.host` for rate-limit keys on Fly | That is the Fly proxy IP — every user shares one bucket | `Fly-Client-IP` header, falling back to `request.client.host` |
| `journal_mode=DELETE` (SQLite default) | Readers block the writer and vice versa — the direct cause of `database is locked` under overlapping `/process` runs | `PRAGMA journal_mode=WAL` + `busy_timeout` |
| Hand-rolled SSE shutdown draining | `sse-starlette` already hooks uvicorn's SIGTERM and broadcasts to all live streams | `EventSourceResponse(shutdown_grace_period=...)` |
| A CDN `<script src>` for Chart.js/Pico | Third-party runtime dependency on a demo whose selling point is that it stays up; complicates CSP; extra RTT on cold start | Vendor pinned files into `src/relay/static/vendor/` |
| Floor-only `>=` pins with no lockfile | `anthropic` is at 0.120.2 vs a declared `>=0.60` floor; CI can silently pick up a breaking major | Commit a `uv.lock` / compiled `requirements.txt` |

---

## Stack Patterns by Variant

**If retrieval quality after the Voyage switch is worse than keyword search on the eval set:**
- Check `input_type` first (`"document"` at index time, `"query"` at search time) before changing models — this is the most common cause.
- Then raise `output_dimension` 512 → 1024, then `voyage-4-lite` → `voyage-4`.
- Consider hybrid: keep the existing keyword scorer and blend scores. Cheap insurance and it makes the retrieval upgrade a strict superset rather than a replacement.

**If the KB grows past ~1,000 chunks:**
- Revisit `sqlite-vec`, but first verify `sqlite3.Connection.enable_load_extension` exists on every target interpreter (it does not on stock macOS python.org builds).

**If the demo ever runs more than one machine:**
- In-memory rate limits and the single-writer SQLite assumption both break. That is the Postgres + Redis boundary, and it is explicitly out of scope for this milestone.

**If you decide to keep the dependency count at zero-new for the DB:**
- `asyncio.to_thread` + `PRAGMA journal_mode=WAL` + `busy_timeout=5000` + a `contextvars`-scoped connection-per-task is a legitimate alternative. Budget more design time for connection ownership than the `aiosqlite` path costs.

---

## Version Compatibility

| Package | Constraint | Notes |
|---------|-----------|-------|
| `numpy` | `>=2.3,<3` | `numpy>=2.5.0` requires Python `>=3.12`. Project floor is 3.11. Newest 3.11-compatible is 2.4.6. |
| `voyageai` | n/a (not adopted) | Would cap Python at `<3.15`. |
| `httpx` | `>=0.28,<1` | `anthropic` requires `httpx<1,>=0.25.0` — compatible. Promote from dev extra to runtime. |
| `sse-starlette` | `>=3.4.8,<4` | Requires Python `>=3.10`. `shutdown_grace_period` must be **strictly less** than uvicorn's `--timeout-graceful-shutdown`. |
| `limits` | `>=5.8,<6` | Requires Python `>=3.10`. Async namespace is `limits.aio.*`. |
| `aiosqlite` | `>=0.22,<1` | Requires Python `>=3.9`. Still pre-1.0 — pin the upper bound. |
| `pytest-asyncio` | declared `>=0.23`, actual latest `1.4.0` | Major-version drift with changed default loop-scope semantics. Pin an upper bound before it breaks CI. |
| `anthropic` | declared `>=0.60`, actual latest `0.120.2` | 60 minor versions of unpinned drift on an unlocked CI install. |
| `fastapi` / `uvicorn` / `pydantic` | latest 0.141.1 / 0.52.1 / 2.13.4 | No known incompatibilities with any recommendation above. |
| Chart.js | `4.5.1` (2025-10-13) | UMD build, no build tooling. |
| Pico.css | `2.1.1` classless | Pure CSS. |

---

## Sources

- PyPI JSON API (`pypi.org/pypi/<pkg>/json`), queried 2026-08-06 — exact latest versions, `requires_python`, `requires_dist`, upload timestamps for `limits`, `aiosqlite`, `voyageai`, `sqlite-vec`, `slowapi`, `sse-starlette`, `numpy`, `httpx`, `anthropic`, `fastapi`, `jinja2`, `pytest-asyncio`. **HIGH**
- npm registry API (`registry.npmjs.org`), queried 2026-08-06 — Chart.js 4.5.1, uPlot 1.6.32, htmx 2.0.10 (4.0.0-beta6 on `next`), htmx-ext-sse 2.2.4, @picocss/pico 2.1.1. **HIGH**
- https://docs.voyageai.com/docs/embeddings — voyage-4 family, dimensions, context lengths, legacy-model status. **HIGH**
- https://docs.voyageai.com/docs/pricing — per-1M-token pricing and 200M free-token tier per model. **HIGH**
- https://docs.voyageai.com/reference/embeddings-api — request/response shape, `input_type`, `output_dimension`, batch limits (1,000 texts; 320K tok/req for voyage-4). **HIGH**
- https://github.com/voyage-ai/voyageai-python `voyageai/__init__.py` (main branch, pushed 2026-07-10) — confirms `AsyncClient` exists. **HIGH**
- Context7 `/sysid/sse-starlette` — graceful-shutdown API, `AppStatus.handle_exit` behaviour, `shutdown_grace_period` vs `--timeout-graceful-shutdown` ordering. **HIGH**
- https://limits.readthedocs.io/en/stable/async.html — `limits.aio.storage.MemoryStorage`, `limits.aio.strategies.MovingWindowRateLimiter` import paths. **HIGH**
- https://slowapi.readthedocs.io/ — self-declared alpha status, `request`/`Response` signature requirements, decorator ordering, no-websocket limitation. **HIGH**
- https://fly.io/docs/networking/request-headers/ + Fly community threads — `Fly-Client-IP` is the correct client-IP source behind the Fly proxy. **MEDIUM-HIGH**
- Local verification on this machine (Python 3.14.6, SQLite 3.50.4): `hasattr(sqlite3.Connection, 'enable_load_extension')` → `False`. **HIGH** (single environment; `python:3.12-slim` likely differs — that divergence *is* the finding)
- https://htmx.org/extensions/sse/ — SSE extension semantics (HTML-fragment swapping). **MEDIUM**
- WebSearch on aiosqlite vs `to_thread`, WAL best practice — corroborating only, no single authoritative comparison found. **LOW-MEDIUM**; the recommendation rests on the connection-ownership argument, which is verifiable from aiosqlite's own design (one thread per connection, serialized queue).

---
*Stack research for: hardening an existing FastAPI + SQLite + Claude API agent service*
*Researched: 2026-08-06*

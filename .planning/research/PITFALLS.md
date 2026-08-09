# Pitfalls Research

**Domain:** Retrofitting production hardening (auth, rate limiting, async DB, embeddings RAG, live SSE dashboard) into a working FastAPI + SSE agent service
**Researched:** 2026-08-06
**Confidence:** HIGH for codebase-derived pitfalls (read from source), MEDIUM for library/platform behaviours (verified against official docs and maintainer issue threads)

> Scope note: every pitfall below is grounded in the actual Relay source (`src/relay/`), the eval harness (`src/relay/evals.py`, `evals/golden.jsonl`), the test suite (37 tests), and `fly.toml`/CI. Generic web-security advice is deliberately excluded.

## Critical Pitfalls

### Pitfall 1: Async contagion breaks the shared sync `ToolSpec.execute` contract (and takes MCP down with it)

**What goes wrong:**
`ToolSpec.execute` is typed `Callable[..., str]` and is invoked synchronously at `agent.py:_execute_guarded` → `return spec.execute(**validated), False`. That same sync `_execute_guarded` is imported and called by `mcp_server.call_mcp_tool`, which is itself a **sync** function (tested with `pytest.raises`, not `await`). The moment any executor becomes `async` — because you switched to `aiosqlite`, or because `search_docs` now calls the Voyage API — `spec.execute(...)` returns a coroutine object. `_execute_guarded` happily returns it as the "result JSON", so the tool_result content becomes `<coroutine object ...>`, the model receives garbage, `json.loads(result)` in the `tool_result` event raises, and the MCP server silently degrades. Nothing raises at the point of the mistake.

**Why it happens:**
The registry is the single seam between three consumers (agent loop, MCP server, evals) and it was designed sync. "Make SQLite async" and "call an embeddings API" both look like local changes to `tools.py`, but they change a cross-cutting contract. The mistake is invisible in type-checking because `Callable[..., str]` is unchecked at runtime and there is no mypy in CI (only `ruff check`).

**How to avoid:**
Decide the executor contract **before** touching either feature, and pick one:
- **Recommended:** keep `ToolSpec.execute` sync and offload blocking work at the *call site* — `await anyio.to_thread.run_sync(partial(spec.execute, **validated))` inside an `async def _execute_guarded_async`, keeping the existing sync `_execute_guarded` as a thin wrapper for the MCP path. One contract, both consumers keep working, event loop unblocked.
- If you must go async: make `_execute_guarded` async, convert `call_mcp_tool` to async (`_on_call_tool` is already async so it can await), and update `test_mcp.py`'s `pytest.raises` blocks. Do it as one atomic change, not incrementally.

Add a guard test that asserts every registry executor's return value is a `str`, not awaitable:
```python
def test_all_executors_return_str(registry):
    for name, spec in registry.items():
        assert not inspect.iscoroutinefunction(spec.execute), name
```

**Warning signs:**
- `tool_result` event `data.result` fails `json.loads` in a test
- `RuntimeWarning: coroutine ... was never awaited` in pytest output
- `test_mcp.py` passes but MCP tool calls return the literal string `<coroutine object`
- Any diff that adds `async` to a function in `tools.py`

**Phase to address:** Async-safe SQLite phase — this is the *first* thing that phase must settle, before the Voyage phase inherits the decision.

---

### Pitfall 2: `app.state.registry` is a single shared object — binding `ticket_id` into it creates a cross-run race

**What goes wrong:**
The obvious fix for the model-supplied `ticket_id` problem is "bind the run's ticket id into the tool executor." But `build_registry(conn, kb_dir)` is called **once at startup** (`main.py` lifespan) and stored in `app.state.registry`; every concurrent `/tickets/{id}/process` run shares the same dict of the same closures. If you bind by rebuilding-with-ticket-id at startup you can't (no ticket yet); if you bind by mutating registry state per run, two overlapping SSE runs will overwrite each other's ticket id and the agent will write a reply to the *wrong* ticket — the exact bug you set out to fix, now with a race instead of a prompt injection as the trigger.

**Why it happens:**
The registry looks per-request because it is reached through `app.state` in a request handler. It isn't. Concurrency is invisible in the current test suite (`test_api.py` has 3 tests, none concurrent — flagged in CONCERNS.md as a Medium-priority coverage gap).

**How to avoid:**
Bind at **call time**, not build time. Pass the run's ticket id down as a parameter into `_execute_guarded` and enforce it there, after Pydantic validation and before execution:

```python
def _execute_guarded(spec, name, raw_input, policy, *, bound_ticket_id: int | None = None):
    ...
    validated = validate_tool_input(spec.input_model, raw_input)
    if bound_ticket_id is not None and "ticket_id" in validated \
            and validated["ticket_id"] != bound_ticket_id:
        return json.dumps({"error": f"ticket_id must be {bound_ticket_id}",
                           "denied_by": "binding"}), True
```
`run_ticket` already has `ticket["id"]` in scope. The MCP path passes `bound_ticket_id=None` (no run context exists there) and keeps working unchanged.

**Warning signs:**
- Any diff that changes `build_registry`'s signature to take a ticket id
- Any diff that assigns to a captured variable inside a registry lambda
- A new `app.state.current_ticket_id` (or module-level mutable) appearing anywhere

**Phase to address:** Server-side `ticket_id` binding phase. Ship the concurrency test alongside it (CONCERNS.md already flags "no test for concurrent `/tickets/{id}/process`").

---

### Pitfall 3: Silently rewriting `ticket_id` instead of rejecting it turns a security fix into an eval regression

**What goes wrong:**
Two "obvious" implementations of ticket_id binding each break something:

*(a) Silently overwrite* — `validated["ticket_id"] = run_ticket_id`. The `tool_use` SSE event still shows the model's original (wrong) id, so the stream and the dashboard lie about what happened, and `evals.extract_outcome` (which reads `event.data["input"]` from `tool_use` events) records the model's value, not the effective one. Prompt injection becomes invisible rather than prevented — you lose the observability that is the point of the project.

*(b) Reject as `is_error=True`* — correct, but `agent.py` only sets `resolved_via` when `not is_error and block.name in TERMINAL_TOOLS`. So a rejected `send_reply` leaves `resolved_via = None`, the run ends with `stop_reason == "end_turn"`, and the loop yields `error: ended_without_action`. In the eval harness `result.action_ok = outcome["action"] == case["expected_action"]` → **fails**. If the model does this on 3 of 12 golden cases you drop below the 0.8 CI threshold and the evals workflow exits non-zero.

**Why it happens:**
`TERMINAL_TOOLS` resolution semantics are a hardcoded set with an `is_error` coupling (already flagged as fragile in CONCERNS.md). A new *denial* path is a new state transition through code that was written assuming denials only came from dry-run policy — where `ended_without_action` was the intended outcome.

**How to avoid:**
1. Reject (option b), but return an error message the model can *recover* from: `"ticket_id must be {N} for this run — retry with the correct id"`. The model then retries in the same run and resolves normally, so evals are unaffected in the happy path.
2. Emit the **effective** input in the `tool_use` event (or add a `bound` field), so the stream, dashboard, and `extract_outcome` all reflect reality.
3. Add a dedicated telemetry counter / span attribute `relay.tool.binding_violation` so injections show up in `/metrics` rather than as a generic `ended_without_action`.
4. Add the missing test from CONCERNS.md: a `FakeClient` scripting `send_reply` with a mismatched `ticket_id`, asserting no row appears in `replies` for the other ticket *and* that the run still reaches a resolution after the model retries.

**Warning signs:**
- Eval pass rate drops with `action: ended_without_action` on cases that used to pass
- `runs.outcome` in SQLite showing a spike of `error:ended_without_action`
- A binding test that asserts only "no write happened" and not "the run still resolves"

**Phase to address:** `ticket_id` binding phase; re-run the eval suite as the phase's exit gate.

---

### Pitfall 4: API-key auth on the SSE endpoint that a browser can't use — and that breaks CI's health smoke test

**What goes wrong:**
Three failure modes stack up:
1. **Browser `EventSource` cannot set request headers.** There is no API to attach `X-API-Key` or `Authorization`. If the dashboard's live feed (or a demo "run this ticket" button) uses `new EventSource(...)` against an auth-protected endpoint, it fails with an opaque `onerror` and auto-retries every ~3s forever — a silent reconnect loop that also keeps the Fly machine awake (see Pitfall 8).
2. **Blanket middleware kills the health check.** `.github/workflows/ci.yml`'s docker job polls `curl -sf http://127.0.0.1:8000/health` for 15s and fails the build otherwise; Fly's proxy and `/` → `/dashboard` redirect are also unauthenticated paths. An `app.add_middleware(...)` that checks every request breaks the docker CI job and, on deploy, health checks.
3. **Existing tests break.** `test_api.py`'s `test_create_and_fetch_ticket` posts to `/tickets` with no key; `test_observability.py` hits `/metrics` and `/dashboard`.

**Why it happens:**
Auth is naturally expressed as middleware ("protect everything"), and the SSE endpoint is a `POST` that a developer tests with `curl -H` — where headers work fine. The browser constraint only surfaces when the dashboard is wired up, typically in a *later* phase.

**How to avoid:**
- Implement auth as a **FastAPI dependency** on the specific mutating/costly routes (`POST /tickets`, `POST /tickets/{id}/process`), not as global middleware. Explicit allow-list of protected routes beats an explicit deny-list of exempt ones.
- Keep `/health`, `/`, `/metrics`, `/dashboard` public per PROJECT.md, and add a test asserting `/health` returns 200 with **no** credentials so CI's smoke test can never silently regress.
- For any browser-consumed stream, either (a) make it a read-only public feed with no secrets in it, or (b) use `fetch()` + `ReadableStream` instead of `EventSource` so headers work. Do **not** put the key in a query string — it lands in Fly proxy logs, browser history, and referrers.
- Compare keys with `secrets.compare_digest`, and return `401` with `WWW-Authenticate` rather than `403`.
- Update the 3 `test_api.py` tests in the same commit and add the "unauthenticated `POST /tickets` returns 401" test that CONCERNS.md says is missing.

**Warning signs:**
- Docker CI job hangs on the health poll
- Dashboard shows repeated network errors in devtools with no server-side log line
- Any `add_middleware` call in `main.py`
- An API key visible in a URL anywhere

**Phase to address:** Auth phase (dependency-based design + `/health` test); revisit in the dashboard phase for the browser transport decision.

---

### Pitfall 5: `BaseHTTPMiddleware` (the default for auth and rate limiting) degrades SSE

**What goes wrong:**
Starlette's `BaseHTTPMiddleware` — what `@app.middleware("http")` produces, and what many rate-limit snippets use — interposes an anyio stream between the app and the client. With `StreamingResponse` this changes disconnect semantics: `request.is_disconnected()` stops working correctly behind `BaseHTTPMiddleware`, and cancellation/`ClientDisconnect` propagation to the generator is unreliable (Starlette maintainers changed this behaviour again around Starlette 0.45 — no exception is raised on cancel for some streaming cases). Result: the SSE generator in `process_ticket` keeps running (and keeps spending Claude tokens) after the browser tab closed, which is precisely the cost-abuse vector auth was supposed to close.

**Why it happens:**
`@app.middleware("http")` is the first thing any FastAPI auth tutorial shows, and the breakage is invisible in `TestClient` (which drains the whole response) — it only appears with real disconnects in production.

**How to avoid:**
- Prefer route dependencies (auth) and a pure-ASGI middleware (rate limiting) over `BaseHTTPMiddleware`.
- Explicitly wire disconnect detection into the SSE generator rather than relying on cancellation: race the agent loop against a task awaiting `await request.receive()` for `"http.disconnect"`, and break the loop when it fires.
- Add a test that closes the connection mid-stream (`with client.stream(...)` then break early) and asserts the agent loop stopped — this is currently untested.

**Warning signs:**
- `/metrics` `runs` count growing from streams nobody watched
- Claude spend continuing after the demo tab is closed
- Any `@app.middleware("http")` decorator in `main.py`

**Phase to address:** Auth + rate limiting phase (choose middleware style); verified in the graceful-shutdown/SSE-lifecycle phase.

---

### Pitfall 6: `slowapi`'s decorator is incompatible with a `StreamingResponse` endpoint

**What goes wrong:**
`slowapi` — the default FastAPI answer for rate limiting, and the one CONCERNS.md suggests — injects `X-RateLimit-*` headers into the returned response *after* the endpoint returns, via `_inject_headers`, which raises `Exception("parameter 'response' must be an instance of starlette.responses.Response")` when it can't find one. `process_ticket` returns a `StreamingResponse` and takes no `response: Response` parameter. Even where the isinstance check passes, injecting headers into a streaming response post-hoc is wrong: headers must be finalized before the first byte. You get either a 500 on the very endpoint you most needed to protect, or headers that never reach the client.

Related: `@limiter.limit(...)` requires the endpoint to declare `request: Request` — `process_ticket(ticket_id: int, dry_run: bool = False)` currently does not, and forgetting it produces a confusing runtime error rather than a startup error.

**Why it happens:**
Rate-limiting libraries assume the classic buffered request/response cycle. Streaming endpoints are the exception, and the failure is at runtime only.

**How to avoid:**
- Either use `slowapi` **without** header injection (custom handler; add `request: Request` to the signature and verify against the real SSE endpoint, not just `/tickets`), or write a ~30-line token bucket as an ASGI middleware / dependency that runs entirely **before** the stream starts. Given "no orchestration framework, the visible hand-written code is a feature," a hand-rolled bucket is on-brand and avoids the dependency.
- Enforce the limit in the **dependency**, before `StreamingResponse` is constructed — once you've yielded a byte, the status code is locked at 200 and you can no longer return 429. A rate-limit rejection expressed as an in-stream `event: error` is not a rate limit; the client already got a 200.
- Test the limiter against `/tickets/{id}/process` specifically, not just a plain JSON route.

**Warning signs:**
- 500s on `/tickets/{id}/process` while `/tickets` works fine
- 429 logic that lives inside `event_stream()`
- Rate-limit headers absent on the streaming route

**Phase to address:** Rate limiting phase.

---

### Pitfall 7: In-memory rate limiting does not cap spend on a scale-to-zero machine

**What goes wrong:**
`fly.toml` sets `min_machines_running = 0` and `auto_stop_machines = 'stop'`. An in-process token bucket lives in RAM; when the machine stops (idle) and restarts on the next request, **the bucket resets**. An attacker (or a crawler) that paces requests around the idle window gets an unbounded number of full-rate windows. Requirement 2 in PROJECT.md is explicitly "cap **aggregate** Claude API spend" — a per-minute request limit does not do that. `max_run_cost_usd` ($0.50) caps one run; 200 runs is $100.

**Why it happens:**
"Rate limiting" is conflated with "spend cap." They're different controls; only one of them is what the constraint asks for.

**How to avoid:**
Implement **two** independent controls:
1. In-memory per-IP/per-key request throttle (burst protection; resets are acceptable here).
2. A **persistent** rolling spend ceiling read from the existing `runs` table — `SELECT SUM(cost_usd) FROM runs WHERE created_at > datetime('now','-1 day')` — checked in the `process_ticket` dependency, returning 429/503 with a "daily demo budget exhausted" message when over. This survives restarts because it lives on the Fly volume, needs no new table, and doubles as a dashboard metric.
3. Note that `runs` rows are only written *after* a stream completes (`record_run` at the end of `event_stream`), so N concurrent runs are invisible to the ceiling. Either reserve a row up front or cap concurrency with a semaphore — but **not** a semaphore held with `async with` across `yield` boundaries (see Pitfall 12).

**Warning signs:**
- Fly/Anthropic spend rising while `/metrics` shows a healthy `mean_per_run`
- Rate-limit counters that reset to zero in logs after every cold start
- No code path that reads historical `cost_usd`

**Phase to address:** Rate limiting phase. This is the requirement's actual acceptance criterion.

---

### Pitfall 8: A live dashboard SSE connection defeats scale-to-zero and quietly costs money

**What goes wrong:**
The current dashboard polls `/metrics` every 5s over plain `fetch` — short connections, so Fly's proxy sees the machine as idle between them and `auto_stop_machines = 'stop'` works. Replace that with a persistent SSE feed and a single open browser tab (yours, a recruiter's, a crawler's) holds a connection open indefinitely. The machine **never** stops. `min_machines_running = 0` becomes decorative and the "cheap to keep running" core constraint is violated by the very feature meant to showcase the project.

Compounding it: `EventSource` auto-reconnects (~3s) forever, including after the tab is backgrounded and after every deploy — so even a closed laptop lid can hold the machine up, and a deploy triggers a reconnect stampede from every open client.

**Why it happens:**
Scale-to-zero and long-lived connections are fundamentally in tension, and the cost only shows up on the next invoice.

**How to avoid:**
- Put a **server-side max lifetime** on the dashboard stream (e.g. 5 minutes), then close it cleanly with a terminal event. Let the client decide whether to reconnect, and back off / stop reconnecting when `document.hidden`.
- Send SSE comment heartbeats (`: ping\n\n`) at ~15s so intermediaries don't kill idle streams — these are ignored by `EventSource` by spec, but verify any non-browser consumer of the run stream tolerates them before adding heartbeats to `/tickets/{id}/process` (that's an SSE-contract change).
- Consider keeping the dashboard on polling for aggregate metrics and using SSE **only** for the in-flight run feed, which is naturally short-lived. This preserves scale-to-zero for the 99% idle case.
- Set `Cache-Control: no-cache` and `X-Accel-Buffering: no` on all SSE responses.

**Warning signs:**
- `fly machine list` never shows `stopped`
- Fly billing rising with no traffic
- Heartbeat/keepalive added to the *agent run* stream without a contract note

**Phase to address:** Dashboard phase — treat "machine still reaches stopped state when idle" as an explicit acceptance criterion.

---

### Pitfall 9: Naive `asyncio.to_thread` offload of the *shared* connection introduces the race the current code accidentally avoids

**What goes wrong:**
Today a single `sqlite3.Connection` (`check_same_thread=False`) is used from a single-threaded event loop. It blocks the loop — but it is accidentally serialized, so it is *correct*. Wrapping the existing calls in `asyncio.to_thread` / `anyio.to_thread.run_sync` without changing the connection topology hands that one connection to arbitrary worker threads concurrently. sqlite3's implicit transactions and `conn.commit()` are **connection-scoped**: `tools.send_reply` does `INSERT` then `UPDATE` then `commit()`, so a `commit()` issued by another thread mid-sequence commits a partial transaction belonging to a different request. You convert a performance problem into a data-integrity problem, and neither the tests (single-threaded, `:memory:`) nor the demo traffic will show it.

Second, WAL will not save you here: **WAL cannot be enabled on `:memory:` databases** (SQLite leaves the journal mode unchanged and returns the prior mode, because the required shared-memory primitives are unavailable). Every fixture in `conftest.py` uses `connect(":memory:")`, so a `PRAGMA journal_mode=WAL` added to `db.connect()` is a **no-op in every single test** — you will have zero coverage of the behaviour you shipped.

Third, `PRAGMA foreign_keys = ON` in `db.connect()` is per-connection. Any pool or per-thread connection scheme that doesn't re-apply it silently disables FK enforcement on `escalations.ticket_id` and `replies.ticket_id`.

**Why it happens:**
"Wrap the blocking call in `to_thread`" is the canonical async fix and is correct for *stateless* blocking calls. A DB connection is stateful.

**How to avoid:**
- Change the topology, not just the call site. Either: one connection **per operation/thread** (`connect()` inside the offloaded function, cheap for SQLite) with WAL + `PRAGMA busy_timeout=5000` + `PRAGMA foreign_keys=ON` applied in `connect()` for every connection; or a single dedicated writer serialized behind an `asyncio.Lock`.
- Set `PRAGMA busy_timeout` explicitly — the default is 0, which is why "database is locked" fires instantly instead of waiting.
- Point tests at a `tmp_path` file DB (not `:memory:`) for at least the concurrency/WAL tests, and assert `PRAGMA journal_mode` returns `wal`. Keep `:memory:` for the fast unit fixtures but know it proves nothing about locking.
- Add the concurrency test CONCERNS.md asks for: N simultaneous `/tickets/{id}/process` calls with a `FakeClient`, asserting no `OperationalError` and exactly N `runs` rows.
- `evals.run_case` calls `connect(":memory:")` directly and imports `lookup_customer(conn, ...)` directly — any change to `connect()`'s signature or the executor contract must be applied there too or the eval harness breaks with `harness: ...` errors on all 12 cases.

**Warning signs:**
- A test asserting WAL that passes against `:memory:` (it can't be real)
- `sqlite3.OperationalError: database is locked` appearing only under the demo
- `tickets.status = 'resolved'` with no matching row in `replies` (partial commit)
- FK violations no longer raising in tests after a connection refactor

**Phase to address:** Async-safe SQLite phase. Test topology change (`tmp_path` DB) is part of the phase, not a follow-up.

---

### Pitfall 10: Embeddings retrieval makes the eval suite *worse*, not better, on a 381-word knowledge base

**What goes wrong:**
This is the highest-risk change in the milestone and the least intuitive. The entire KB is **3 files, ~2.5 KB, 381 words total** (`account.md` 140w, `billing.md` 130w, `api.md` 111w). Today `search_docs` returns the **full text of every matching file** — the model effectively sees the whole relevant document. Three specific regressions follow from a naive "proper RAG" implementation:

1. **Chunking loses context.** Split those docs into 200-token chunks and return top-3 and the model now sees *less* grounding material than before. The LLM judge grades "is every claim supported by the KB," so less context → more hedging or more invented specifics → `grounded: false` → pass rate falls below the 0.8 CI threshold.
2. **No similarity floor changes agent behaviour.** The keyword scorer returns `{"results": []}` when nothing matches, which is a strong signal that pushes the model to `create_escalation`. Cosine similarity **always** returns a top-k, so an off-topic query gets a plausible-looking but irrelevant chunk. Golden cases like `refund-monthly` (`expected_action: create_escalation`, because `billing.md` says refunds need a human) are exactly the ones at risk: the retriever confidently returns `billing.md`, the model answers instead of escalating, `action_ok` fails.
3. **Asymmetric `input_type` forgotten.** Voyage prepends different instructions for `input_type="query"` vs `"document"`; indexing and querying with the same (or no) value measurably degrades recall. Likewise a mismatch in `model` or `output_dimension` between index build and query time produces either a shape error or, worse, silently meaningless similarities.

**Why it happens:**
"Replace keyword with embeddings" is treated as a drop-in quality upgrade. At this corpus size the keyword scorer is a strong baseline and the full-document return is doing real work. RAG best practices are calibrated for corpora 1000× larger.

**How to avoid:**
- **Do not chunk.** Embed each `.md` file whole (all three are far under Voyage's 32K context) and keep returning full document text, so the tool's *output* contract is byte-for-byte what it is today. Only the *ranking* changes. This isolates the variable.
- **Keep the empty-result path.** Apply a similarity floor and return `{"results": []}` below it, preserving the "docs don't cover this → escalate" signal. Calibrate the threshold against the golden set before merging.
- **Hybrid, not replacement:** union of keyword hits and above-threshold semantic hits. Strictly dominates either alone at this scale, and keeps a working path when Voyage is unreachable.
- Pin `model`, `input_type`, and `output_dimension` in `config.py` and **store them alongside the index**; refuse to use an index whose metadata doesn't match the query config.
- **Run the eval suite before and after** with the same model, and diff per-case. This is the only phase in the milestone where the eval suite is the primary acceptance test rather than a regression guard. Budget for the run cost (12 cases × concurrency 4 against the real API).

**Warning signs:**
- Pass rate drops on escalation-expecting cases specifically
- `invented_claims` appearing in judge output where they previously didn't
- `search_docs` never returning an empty result set for any query
- Index metadata not recorded anywhere

**Phase to address:** Voyage embeddings phase. Gate: eval pass rate ≥ pre-change baseline, not merely ≥ 0.8.

---

### Pitfall 11: The Voyage index goes stale or gets rebuilt on every cold start

**What goes wrong:**
`kb/` is baked into the Docker image; `RELAY_DB_PATH=/data/relay.db` is on a Fly volume. Two bad options are commonly chosen:
- **Build the index at startup** → every cold start (and `min_machines_running = 0` means many) pays Voyage API latency + cost before the first request can be served, and the CI docker smoke test (`curl /health`, 15 attempts, 1s apart, `ANTHROPIC_API_KEY=ci-placeholder`, **no Voyage key**) fails because startup blocks or errors.
- **Persist the index to `/data`** → the volume survives deploys, so editing `kb/billing.md` and redeploying leaves stale embeddings pointing at the old text while `search_docs` returns the *new* file content. Retrieval silently ranks against text that no longer exists.

**Why it happens:**
The image (immutable, versioned) and the volume (mutable, persistent) have different lifecycles, and the KB lives in the image while everything persistent lives in the volume.

**How to avoid:**
- **Build the index at image-build time** and commit/ship it as a JSON or `.npy` artifact next to `kb/`. Three documents × 1024 floats is a few tens of KB. Zero cold-start cost, zero runtime Voyage dependency for indexing, works offline in CI.
- Key the artifact by a **content hash of `kb/`** and verify the hash at startup; fail loudly (or fall back to keyword) on mismatch rather than serving stale vectors.
- Add a `scripts/build_index.py` and a CI check that fails if `kb/` changed without the index being rebuilt — the same class of forcing function `TERMINAL_TOOLS` lacks.
- Never write the index to `/data`.

**Warning signs:**
- Startup time or `/health` latency growing after the retrieval change
- CI docker job flaking or timing out
- An index file in `.gitignore` or on the Fly volume
- No hash/version recorded with the vectors

**Phase to address:** Voyage embeddings phase.

---

### Pitfall 12: Graceful shutdown and concurrency limits implemented *across* `yield` boundaries re-break the OTel span handling

**What goes wrong:**
`agent.py` carries an explicit warning: the run span is started with `tracer.start_span(...)` and parented manually via `trace.set_span_in_context`, **not** made current, because "execution suspends at every yield and a current-span context manager would leak across whatever runs in between." Draining logic naturally wants a context manager (`async with drain_tracker:`, `async with concurrency_semaphore:`, `async with limiter:`) wrapped around the loop — which is exactly a context manager held across yields. With OTel's contextvar-based context, an `async with` current-span manager spanning yields attaches unrelated concurrent work to this run's span, corrupting the trace tree for every overlapping run. The same shape of bug hits any contextvar-based state.

Separately: `record_run` is called at the **end** of `event_stream`, after the last agent event. If the client disconnects or the process gets SIGTERM mid-run, the generator is cancelled and **no `runs` row is written** — the run's cost is spent but invisible to `/metrics`, to the dashboard, and (per Pitfall 7) to any spend ceiling built on the `runs` table. CONCERNS.md flags this; it becomes materially worse once the dashboard's charts are the showpiece.

**Why it happens:**
Generators plus context managers plus contextvars is a genuinely subtle interaction, and it works fine in single-run tests.

**How to avoid:**
- Track in-flight runs with an explicit counter/set incremented and decremented in the existing `try/finally` inside `run_ticket` (which already runs `run_span.end()` correctly), **not** with a new `async with` around the loop.
- Wrap `record_run` in the generator's own `finally` (or a `try/except asyncio.CancelledError`) so a partial run still records `outcome="interrupted"` with the cost spent so far. Then re-raise.
- On shutdown: stop accepting new `/tickets/{id}/process` requests first (a flag checked in the dependency), let in-flight generators observe a shutdown event and emit a terminal `event: error`/`event: done` cooperatively, and only then close the DB connection in the lifespan teardown. Closing `conn` while a generator still holds it produces `ProgrammingError: Cannot operate on a closed database` inside `record_run` — the run vanishes.
- Set uvicorn's `--timeout-graceful-shutdown` and raise Fly's `kill_timeout` (default **5 seconds**, max 300) to something that actually accommodates a 10-step agent run, or accept that runs will be cut and make the interrupted path clean. Note Fly stops machines for `auto_stop_machines` too, not just deploys — so this path fires routinely, not just on deploy.
- Add tests: (a) generator cancelled mid-stream still writes a `runs` row; (b) two overlapping runs produce two sibling `agent.run` spans with correctly-parented children (extend `test_observability.py`).

**Warning signs:**
- `async with` newly appearing around the `for _ in range(settings.max_agent_steps)` loop
- Span trees where one run's `tool.*` spans appear under another run
- `/metrics` `runs` count lower than the number of streams actually started
- `Cannot operate on a closed database` in shutdown logs

**Phase to address:** Graceful shutdown phase — but the OTel constraint applies to *every* phase that touches `agent.py`.

---

### Pitfall 13: `RELAY_` env prefix silently renames the Voyage key

**What goes wrong:**
`Settings` uses `env_prefix="RELAY_"`, with an explicit escape hatch for Anthropic (`validation_alias="ANTHROPIC_API_KEY"`, with a comment explaining why: the SDK does its own lookup). Adding `voyage_api_key: str | None = None` produces a setting read from `RELAY_VOYAGE_API_KEY`, while the `voyageai` SDK reads `VOYAGE_API_KEY` from the environment itself. Set one, and depending on whether you pass the key explicitly or let the SDK find it, you get either an unauthenticated client or a `None` config — with an unhelpful error at first query, not at startup. The identical trap applies to a new `relay_api_key` for auth.

**Why it happens:**
The prefix is invisible at the point of use, and the existing Anthropic alias makes it look like keys "just work."

**How to avoid:**
Mirror the existing pattern exactly: `validation_alias="VOYAGE_API_KEY"` with a comment, pass the key explicitly to the client rather than relying on ambient lookup, and validate presence at startup with a clear error. Set the Fly secret with the same name you read (`fly secrets set VOYAGE_API_KEY=...`) and document it in the README next to `ANTHROPIC_API_KEY`.

**Warning signs:**
- Auth working locally (`.env`) but not on Fly, or vice versa
- 401s from Voyage despite the secret being set
- Two different spellings of the same key across `fly.toml`, README, and `config.py`

**Phase to address:** Auth phase (for the Relay API key) and Voyage phase (for `VOYAGE_API_KEY`).

---

### Pitfall 14: Growing `DASHBOARD_HTML` in place — the f-string/template-literal collision

**What goes wrong:**
The dashboard is a Python string constant containing JS that uses `${...}` template literals and CSS full of `{ }`. The moment anyone converts it to an f-string to inject server-side data (the natural move for "server-rendered"), **every** CSS brace and every JS `${}` becomes a format placeholder and the module fails to import — or worse, `{value}` silently interpolates a Python variable that happens to exist. A "polished dashboard with charts" also means hundreds of lines of HTML/CSS/JS inside a `.py` file with no syntax highlighting, no linting, and `ruff`'s `line-length = 100` fighting every markup line.

**Why it happens:**
The existing pattern is a single constant, and "no build step" gets misread as "no templates and no static files." They are unrelated: Jinja2 and `StaticFiles` require no build step.

**How to avoid:**
Move the dashboard to `src/relay/templates/dashboard.html` + `src/relay/static/` served via `Jinja2Templates` / `StaticFiles` before adding a single feature to it. Still one container, still no npm, still no CORS surface — fully consistent with the "no build step, no SPA" decisions in PROJECT.md. Include the template/static dirs in the hatchling wheel config (`packages = ["src/relay"]` ships them, but verify in the docker CI job — a dashboard that 500s in the container while working locally is the classic packaging miss). Keep charts as hand-written inline SVG rather than a CDN chart library: a CDN dependency adds an external failure point and an unnecessary CSP surface for a page whose data is 20 rows.

**Warning signs:**
- An `f` prefix appearing on `DASHBOARD_HTML`
- `# noqa: E501` accumulating in `main.py`
- `/dashboard` working under `uvicorn` but 500ing under `docker run`
- A `<script src="https://cdn...">` tag

**Phase to address:** Dashboard phase — first task of the phase.

---

### Pitfall 15: The public live run feed leaks customer ticket content

**What goes wrong:**
PROJECT.md keeps `/dashboard` and `/metrics` public read-only. `/metrics` today exposes only aggregates plus `last_runs` rows (ids, tokens, cost, outcome) — safe. A "live run feed" that streams the agent's `text`, `tool_use`, and `tool_result` events to any visitor is a different thing entirely: it broadcasts ticket bodies, `lookup_customer` output (name, plan, email, full ticket history), and draft replies. Since `POST /tickets` will be auth-protected but the dashboard is not, anyone can watch what an authenticated user submits. For a portfolio demo with seeded fake customers this is low *actual* harm, but it is exactly the kind of thing a reviewing engineer notices — and it undercuts the "safe, production-hardened" story the milestone is selling.

**Why it happens:**
The feed is built by reusing the existing `AgentEvent` stream, which was designed for the operator running the ticket, not for the public.

**How to avoid:**
- Define a **separate, redacted** public event projection for the dashboard feed: tool *names* and outcomes, step counts, running cost, latency — never `tool_result.result` contents, never reply bodies, never customer records. Redact at the producer, not in the browser.
- If the full stream is wanted for the demo, gate it behind the same API key and use `fetch` + `ReadableStream` (see Pitfall 4).
- Note this is also a `CONCERNS.md` "no per-event-type schema" problem surfacing: a redacted projection is much easier to get right if `AgentEvent.data` gets per-type models in the same phase.

**Warning signs:**
- Dashboard JS receiving `tool_result` events with a `result` payload
- Customer emails visible in devtools on a logged-out page
- The public feed reusing `run_ticket`'s events verbatim

**Phase to address:** Dashboard phase; consider pairing with per-event-type schemas.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Wrap existing sqlite calls in `to_thread` without changing connection topology | One-line "async-safe" diff, tests still pass | Partial-commit data corruption under concurrency; strictly worse than today's blocking-but-serialized behaviour | **Never** — this is a regression disguised as a fix |
| `PRAGMA journal_mode=WAL` in `connect()` while all tests use `:memory:` | Looks like the box is ticked | WAL is a silent no-op in every test; zero coverage of the shipped behaviour | Only with at least one `tmp_path` file-DB test asserting `journal_mode == 'wal'` |
| Auth as global `BaseHTTPMiddleware` | Ten lines, protects everything | Breaks CI health smoke test and Fly checks; degrades SSE disconnect handling | Never for this app — use route dependencies |
| Silently overwriting the model's `ticket_id` | Simplest possible binding | Injection becomes invisible; SSE events and eval `extract_outcome` disagree with reality | Never — reject with a recoverable error instead |
| Chunking the 3-file KB "because that's what RAG does" | Feels rigorous | Reduces grounding context vs today; risks the 0.8 eval gate | Only if the KB grows past a few thousand words |
| Building the Voyage index at app startup | No build tooling needed | Cold-start latency + cost on a scale-to-zero machine; CI docker job has no Voyage key | Only with a persisted, hash-verified cache and a keyword fallback |
| Growing the dashboard inside `DASHBOARD_HTML` | No new files | f-string collision landmine; unlintable markup; `ruff` line-length friction | Only while the dashboard stays under ~50 lines — it won't |
| In-memory-only rate limiting | Trivial to write | Resets on every cold start; does not satisfy "cap aggregate spend" | Fine as the burst layer, never as the only layer |
| Skipping the eval run because "it costs money" | Saves a few dollars per phase | The retrieval and binding changes are precisely the ones evals exist to catch | Never for the Voyage and ticket_id phases |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Voyage embeddings | Same `input_type` (or none) for indexing and querying | `input_type="document"` when indexing, `"query"` at query time — Voyage prepends different instructions and mismatching measurably hurts recall |
| Voyage embeddings | Model/`output_dimension` drift between index and query producing silently meaningless similarities | Pin `model` + `output_dimension` in `config.py`, store them **in** the index artifact, refuse to query on mismatch |
| Voyage embeddings | Network call in the agent's hot path with no timeout/fallback → new `error` outcomes on every transient blip, including during the 12-case eval run at concurrency 4 | Short timeout + one retry + fall back to the keyword scorer; never let retrieval failure end a run |
| Voyage embeddings | Voyage spend invisible to `RunBudget` and `/metrics` (which only price Claude tokens) | Either add embedding cost to `RunBudget.snapshot()` or explicitly document it as out-of-band; don't let the cost dashboard lie |
| `pydantic-settings` | `env_prefix="RELAY_"` silently renames `VOYAGE_API_KEY` → `RELAY_VOYAGE_API_KEY` | Use `validation_alias`, mirroring the existing `ANTHROPIC_API_KEY` pattern; pass keys explicitly to clients |
| `slowapi` | Decorating a `StreamingResponse` endpoint; missing `request: Request` param | Enforce limits in a dependency before the stream is constructed, or hand-roll a token bucket |
| Fly.io proxy | Assuming `kill_timeout` is generous | Default is **5s** (max 300s); a 10-step agent run will be cut. Raise it or make the interrupted path clean |
| Fly.io proxy | Assuming machines only stop on deploy | `auto_stop_machines = 'stop'` stops them on idle too — the interrupt path is routine, not exceptional |
| Fly.io volumes | Persisting the embedding index next to `relay.db` on `/data` | Ship the index in the image; the volume outlives deploys and will serve vectors for KB text that no longer exists |
| OpenTelemetry | `async with` current-span context manager across the generator's `yield`s | Keep the existing explicit `start_span` + `set_span_in_context` pattern; `agent.py` documents why |
| Browser `EventSource` | Expecting to send an `Authorization`/`X-API-Key` header | Impossible by spec — use a public redacted feed, or `fetch` + `ReadableStream`; never a query-string token |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Per-client SQLite polling behind the live dashboard | Event-loop stalls; SSE for a running ticket goes choppy when tabs are open | One shared in-process pub/sub broadcast fed by run completion, not N pollers | 3+ open dashboard tabs |
| Unbounded per-client broadcast queues | Memory growth on a 512 MB VM; slow client stalls all producers | Bounded queue per subscriber, drop-oldest on overflow, never `await put()` from the agent loop | One slow/backgrounded client |
| Synchronous Voyage query call inside `_execute_guarded` | Every `search_docs` freezes the event loop for the API round-trip — the exact bug the SQLite phase is fixing | Offload via `to_thread` at the call site, or use an async client (see Pitfall 1 first) | Immediately, on any concurrent run |
| Re-reading and re-embedding `kb/` on every `search_docs` call | Voyage bill scaling with tool calls, not documents; latency per step | Load the index once into memory at startup from the shipped artifact | Immediately |
| `run_metrics` doing `SELECT * FROM runs ORDER BY id` and summing in Python on every dashboard tick | `/metrics` latency growing linearly with total runs | Aggregate in SQL; keep `last_runs` as a separate `LIMIT 20` query | A few thousand runs — plausible for a long-lived demo |
| `EventSource` reconnect stampede after deploy or cold start | Burst of connections and DB reads at exactly the moment the machine is cold | Jittered client backoff; stop reconnecting when `document.hidden` | Any deploy with clients connected |
| Long-lived dashboard SSE preventing machine stop | Fly bill grows; machine never `stopped` | Server-side max stream lifetime + client backoff | One always-open tab |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| API key in the SSE URL query string (the `EventSource` workaround) | Key leaks into Fly proxy logs, browser history, `Referer` headers, and any screenshot of the demo | Public redacted feed, or `fetch` + `ReadableStream` with a real header |
| `==` string comparison for the API key | Timing side channel; trivially avoidable | `secrets.compare_digest` |
| Auth check placed inside the SSE generator | Status is already 200 by the first yield — the "401" is just a text event; scanners and clients treat the request as successful | Authenticate in a dependency, before `StreamingResponse` is constructed |
| Exempting paths by prefix (`/tickets` covers `/tickets/{id}/process`, but also anything added later) | New costly routes are unprotected by default | Explicit allow-list of *public* routes; every new route is protected unless named |
| Streaming raw agent events publicly | Ticket bodies, customer records, and draft replies exposed to any visitor | Redacted public projection (Pitfall 15) |
| Trusting model-supplied `ticket_id` after "adding auth" | Auth stops external abuse; prompt injection inside a ticket body is an *internal* trust boundary and is unaffected | Server-side binding in `_execute_guarded` (Pitfall 2) |
| Leaving `mcp_allow_writes: bool = True` | Any connecting stdio client gets `send_reply`/`create_escalation`/`create_ticket` immediately | Default `False`; update `test_mcp.py` fixtures that assume writes are on |
| Logging the API key or full ticket bodies via `JsonFormatter`'s `ctx` passthrough | Secrets/PII in Fly logs, which are less protected than the DB | Never put credentials in `extra={"ctx": ...}`; the formatter serializes everything given to it |
| Rate limiting keyed on `request.client.host` behind Fly's proxy | Every request appears to come from the proxy → one shared bucket, or none | Key on the validated API key where present, and on `Fly-Client-IP` / trusted `X-Forwarded-For` otherwise |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Auth on `POST /tickets` with no public demo path | A visitor to the live demo can read the dashboard but can do **nothing** — the showpiece becomes a screenshot | Keep a public, tightly rate-limited, spend-capped demo path (e.g. a small set of pre-seeded tickets that can be replayed), auth the general-purpose API |
| Rate-limit rejection as a bare 429 with no body | Visitor sees a dead button and assumes the project is broken | 429 with `Retry-After` and a human message ("demo budget for today is spent — here's a recorded run"), rendered by the dashboard |
| Live feed with no state on first load | Empty page on arrival, since nothing is running — worst possible first impression | Server-render the last N runs into the HTML, then attach the stream for updates. This is the whole point of "server-rendered" |
| Silent stream death (shutdown, machine stop, disconnect) | Spinner forever; visitor concludes it hung | Always emit a terminal event (`done` / `error` with a reason) and render it; the graceful-shutdown work is what makes this possible |
| Cold-start delay from `min_machines_running = 0` with no feedback | Several seconds of blank page on first visit | Cheap static shell that renders instantly; show a "waking up" state while data loads |
| Charts that require JS to compute over a growing dataset | Slow, janky page on a 512 MB machine's output | Compute aggregates server-side (SQL) and render inline SVG |

## "Looks Done But Isn't" Checklist

- [ ] **API-key auth:** often missing the negative test — verify `POST /tickets` and `POST /tickets/{id}/process` return 401 with no key **and** that `/health` returns 200 with no key (CI's docker smoke test depends on it)
- [ ] **API-key auth:** often missing the browser story — verify the dashboard's live feed actually works in a browser, not just under `curl -H`
- [ ] **Rate limiting:** often missing the aggregate cap — verify a persistent spend ceiling exists that survives a machine restart, not just an in-memory req/min bucket
- [ ] **Rate limiting:** often only tested on a JSON route — verify the limiter works against `/tickets/{id}/process` (the `StreamingResponse` endpoint) and returns a real 429 status, not an in-stream error event
- [ ] **`ticket_id` binding:** often only tested for "no wrong write" — verify the run still reaches a normal `resolution` after a rejected call, and that the eval suite pass rate is unchanged
- [ ] **`ticket_id` binding:** verify the MCP path (no run context) still works, and that `test_mcp.py` passes unchanged
- [ ] **Async SQLite:** often "done" with a `to_thread` wrapper — verify connection topology changed too, that `PRAGMA busy_timeout` and `PRAGMA foreign_keys=ON` apply to every connection, and that a concurrency test with N overlapping runs produces N `runs` rows and zero `OperationalError`
- [ ] **Async SQLite:** verify `PRAGMA journal_mode` returns `wal` against a **file** DB in a test — it cannot on `:memory:`
- [ ] **Embeddings:** verify the full 37-test suite passes with **no** `VOYAGE_API_KEY` set (CI has none), and that the docker smoke test still comes up
- [ ] **Embeddings:** verify `search_docs` can still return `{"results": []}` for an off-topic query
- [ ] **Embeddings:** verify the index artifact is versioned by a `kb/` content hash and that a stale hash fails loudly
- [ ] **Embeddings:** verify eval pass rate against the **pre-change baseline**, per case, not just against the 0.8 threshold
- [ ] **Dashboard:** verify it renders under `docker run` (packaging of templates/static), not just `uvicorn` locally
- [ ] **Dashboard:** verify the public feed contains no ticket bodies, customer records, or reply text
- [ ] **Dashboard:** verify the Fly machine still reaches `stopped` when idle with no browser attached
- [ ] **Graceful shutdown:** verify an interrupted run still writes a `runs` row (with an `interrupted` outcome) — currently `record_run` only runs on the happy path
- [ ] **Graceful shutdown:** verify the DB connection is closed *after* in-flight generators finish, and that `kill_timeout` in `fly.toml` accommodates the drain window
- [ ] **Graceful shutdown:** verify OTel span parenting is still correct with two overlapping runs

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Async contagion broke the MCP path (Pitfall 1) | LOW if caught in-phase, MEDIUM after | Revert the executor signature; reintroduce as a call-site `to_thread` offload; add the "no coroutine executors" guard test |
| Cross-run `ticket_id` race from registry mutation (Pitfall 2) | MEDIUM | Move binding to `_execute_guarded`; audit `replies`/`escalations` for rows whose `ticket_id` doesn't match any `runs.ticket_id` in the same window |
| Eval pass rate dropped after retrieval swap (Pitfall 10) | MEDIUM — the eval run itself costs money each iteration | Diff per-case results against baseline; first restore full-document returns, then restore the empty-result path, then tune the similarity floor. Change one variable per eval run |
| Partial commits from shared-connection threading (Pitfall 9) | HIGH — silent data corruption | Query for `tickets.status='resolved'` with no `replies` row and `status='escalated'` with no `escalations` row; repair by status reset; then fix topology before re-enabling concurrency |
| Stale index served after a KB edit (Pitfall 11) | LOW | Rebuild and redeploy; add the content-hash check so it can't recur |
| Machine never scaling to zero (Pitfall 8) | LOW if noticed early, MEDIUM as a bill | Add server-side stream max-lifetime and client backoff; confirm `stopped` state in `fly machine list` |
| Auth broke the CI docker smoke test (Pitfall 4) | LOW | Exempt `/health` explicitly; add the regression test asserting it is public |
| Runs missing from `/metrics` after disconnects (Pitfall 12) | LOW (data loss is unrecoverable, fix is cheap) | Move `record_run` into a `finally`; historical gaps cannot be recovered — accept and note |

## Pitfall-to-Phase Mapping

Phases below are suggested groupings matching PROJECT.md's Active requirements; the roadmap may name them differently.

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| 1. Async contagion breaks `ToolSpec.execute` | Async-safe SQLite (decision made here, binds later phases) | `test_mcp.py` passes unchanged; guard test asserts no coroutine executors |
| 2. Shared registry / cross-run ticket_id race | `ticket_id` binding | Concurrency test: N overlapping runs, each writes only to its own ticket |
| 3. Binding regresses `TERMINAL_TOOLS` resolution | `ticket_id` binding | Full eval suite pass rate ≥ baseline; new mismatch test asserts run still resolves |
| 4. Auth breaks browser SSE + CI health check | Auth | `/health` 200 unauthenticated (test); dashboard live feed verified in a real browser |
| 5. `BaseHTTPMiddleware` degrades SSE | Auth + rate limiting | No `add_middleware`/`@app.middleware` in `main.py`; disconnect test stops the agent loop |
| 6. `slowapi` + `StreamingResponse` incompatibility | Rate limiting | Limiter test targets `/tickets/{id}/process`, asserts HTTP 429 status |
| 7. In-memory limits don't cap aggregate spend | Rate limiting | Restart the app, confirm the daily spend ceiling still blocks |
| 8. Dashboard SSE defeats scale-to-zero | Dashboard | `fly machine list` shows `stopped` after idle with no client |
| 9. Naive `to_thread` on a shared connection; WAL no-op on `:memory:` | Async-safe SQLite | File-DB test asserts `journal_mode == 'wal'`; concurrency test yields N `runs` rows, zero `OperationalError`; FK violation still raises |
| 10. Embeddings regress the eval suite | Voyage embeddings | Per-case eval diff vs. baseline; off-topic query returns `{"results": []}` |
| 11. Index staleness / cold-start rebuild | Voyage embeddings | Full test suite green with no `VOYAGE_API_KEY`; index hash matches `kb/`; docker smoke test green |
| 12. Context managers across `yield`; `record_run` skipped on interrupt | Graceful shutdown | Interrupted run writes an `interrupted` `runs` row; overlapping runs produce correctly-parented sibling spans |
| 13. `RELAY_` prefix renames provider keys | Auth / Voyage | Deployed Fly secret name matches the name read in `config.py`; startup fails loudly if absent |
| 14. `DASHBOARD_HTML` f-string collision | Dashboard (first task) | `/dashboard` renders correctly under `docker run` |
| 15. Public feed leaks ticket content | Dashboard | Inspect the public stream: no `tool_result.result`, no reply bodies, no customer records |

**Ordering implication:** the async-executor-contract decision (Pitfall 1) is a prerequisite for both the SQLite and Voyage phases, so the SQLite phase should precede the Voyage phase. The dashboard phase should come after graceful shutdown, since a live feed makes interrupted-run data loss (Pitfall 12) visible to visitors. Auth/rate limiting can go first — it is the highest-risk-if-omitted item on a public deployment — provided it is built as route dependencies rather than middleware.

## Sources

**Codebase (HIGH confidence — read directly, 2026-08-06):**
- `src/relay/agent.py` — `_execute_guarded` sync contract, `TERMINAL_TOOLS`/`is_error` coupling, explicit OTel-across-yields warning comment
- `src/relay/tools.py` — `ToolSpec.execute: Callable[..., str]`, closure over `conn`, `search_docs` returning full document text
- `src/relay/main.py` — startup-built shared `app.state.registry`, `record_run` at end of `event_stream`, `DASHBOARD_HTML` constant, `conn.close()` in lifespan teardown
- `src/relay/db.py` — shared connection, `check_same_thread=False`, per-connection `PRAGMA foreign_keys = ON`, no `busy_timeout`
- `src/relay/mcp_server.py` — **sync** `call_mcp_tool` calling `_execute_guarded`
- `src/relay/config.py` — `env_prefix="RELAY_"` with the `ANTHROPIC_API_KEY` `validation_alias` escape hatch
- `src/relay/evals.py`, `evals/golden.jsonl` — `extract_outcome` reading `tool_use` inputs, `action_ok`/`grounded` gating, judge prompt built from full KB text, `run_case` calling `connect(":memory:")` and `build_registry` directly
- `kb/*.md` — measured: 381 words / ~2.5 KB total across 3 files
- `.github/workflows/ci.yml` — docker job polling `curl -sf /health` with only `ANTHROPIC_API_KEY=ci-placeholder`
- `fly.toml` — `min_machines_running = 0`, `auto_stop_machines = 'stop'`, single volume, 512 MB VM
- `.planning/codebase/CONCERNS.md`, `.planning/codebase/TESTING.md`

**External (MEDIUM–HIGH):**
- Voyage AI embeddings docs — models, 32K context, `input_type` query/document prompt asymmetry, `output_dimension` options, batch limits: https://docs.voyageai.com/docs/embeddings (HIGH — official)
- SQLite WAL / PRAGMA docs — WAL unavailable for in-memory and temp databases; `journal_mode` returns the prior mode on failed conversion: https://www.sqlite.org/wal.html, https://sqlite.org/pragma.html (HIGH — official)
- `slowapi` source, `Limiter._inject_headers` raising `"parameter 'response' must be an instance of starlette.responses.Response"` and post-instantiation header injection: https://github.com/laurentS/slowapi/blob/master/slowapi/extension.py (HIGH — source read)
- Fly.io configuration reference — `kill_timeout` default 5s / max 300s, SIGTERM sequence, autostop applying to idle machines: https://fly.io/docs/reference/configuration/, https://fly.io/docs/reference/fly-proxy-autostop-autostart/, https://fly.io/blog/graceful-vm-exits-some-dials/ (HIGH — official)
- Fly community — streaming responses vs `auto_stop_machines`: https://community.fly.io/t/question-about-streaming-response-and-auto-stop-machines-true/18557 (MEDIUM)
- Starlette — `StreamingResponse` cancellation behaviour change post-0.45; `request.is_disconnected()` unreliable behind `BaseHTTPMiddleware`: https://github.com/Kludex/starlette/discussions/2866, https://github.com/Kludex/starlette/discussions/2094, https://github.com/encode/starlette/issues/297 (MEDIUM — maintainer/issue threads)
- Uvicorn `--timeout-graceful-shutdown` and SSE drain limitations: https://uvicorn.dev/settings/, https://github.com/Kludex/uvicorn/issues/451, https://github.com/sysid/sse-starlette/issues/167 (MEDIUM)
- `EventSource` cannot set custom headers; fetch + `ReadableStream` as the header-capable alternative: https://www.web-developpeur.com/en/blog/sse-fetch-readable-stream-api-key, https://github.com/mpetazzoni/sse.js (MEDIUM — corroborated across sources, consistent with the WHATWG EventSource API surface)

**Gaps / lower confidence:**
- Exact Starlette/FastAPI version pinned in production is unknown (`pyproject.toml` uses `>=` lower bounds only, no lockfile — itself flagged in CONCERNS.md), so the precise disconnect-cancellation semantics in Pitfall 5 should be confirmed empirically with a disconnect test rather than assumed.
- Voyage retrieval quality on a 381-word corpus is untested by anyone publicly; Pitfall 10's regression risk is reasoned from the eval harness's grading logic and corpus size, not from a published benchmark. The empirical answer is one baseline eval run away and should be obtained before the retrieval phase starts.

---
*Pitfalls research for: brownfield hardening of a FastAPI + SSE Claude agent service*
*Researched: 2026-08-06*

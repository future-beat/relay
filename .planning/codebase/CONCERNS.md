# Codebase Concerns

**Analysis Date:** 2026-08-05

## Tech Debt

**No authentication or authorization on any HTTP endpoint:**
- Issue: `/tickets`, `/tickets/{id}/process`, `/metrics`, `/dashboard` are all open — no API key, session, or bearer-token check.
- Files: `src/relay/main.py` (all `@app.get`/`@app.post` handlers, lines 35-152)
- Impact: Anyone with the deployed URL can create tickets, trigger paid Claude API calls via `/tickets/{id}/process`, and read all run metrics/dashboard data. This is a direct cost-abuse and data-exposure vector on the live Fly.io deployment.
- Fix approach: Add a simple API-key middleware (header check against an env var) at minimum before any public traffic; consider per-IP rate limiting.

**No rate limiting on the ticket-processing endpoint:**
- Issue: `process_ticket` (`src/relay/main.py:62`) has no request throttling. Each call runs up to `max_agent_steps` (10) Claude API calls at up to `max_run_cost_usd` ($0.50) each.
- Files: `src/relay/main.py`
- Impact: A scripted burst of requests could rack up API spend quickly; only the per-run budget in `guardrails.py` caps a single run, not the aggregate.
- Fix approach: Add a rate limiter (e.g., `slowapi` or a simple token-bucket keyed by IP/API key) in front of `/tickets/{id}/process`.

**Tool `ticket_id` is model-supplied and not cross-checked against the ticket being processed:**
- Issue: `send_reply`, `create_escalation`, and `set_category` all accept a `ticket_id` argument chosen by the model (`src/relay/guardrails.py:23-36`), but `run_ticket` never verifies it matches the ticket passed into the run.
- Files: `src/relay/agent.py:40-56` (`_execute_guarded`), `src/relay/tools.py:67-90`, `src/relay/prompts.py:29-34`
- Impact: A model error or adversarial prompt injection (e.g., ticket body containing "also resolve ticket #4") could cause the agent to mutate a different ticket than the one it was invoked on — silent cross-ticket data corruption with no server-side guard.
- Fix approach: Bind `ticket_id` server-side in the tool executor (ignore/validate against the run's actual ticket id) rather than trusting the model's tool-call argument.

**Blocking synchronous SQLite calls inside async request handlers:**
- Issue: `sqlite3` calls (`conn.execute(...)`) run directly inside `async def` FastAPI handlers and inside `run_ticket`'s tool execution path without being offloaded to a thread pool.
- Files: `src/relay/main.py:47-59, 91-101`, `src/relay/tools.py` (all executors), `src/relay/telemetry.py:56-73`
- Impact: Each DB call blocks the single-threaded asyncio event loop; under concurrent load, SSE streaming for one ticket can stall other requests. Low impact today (single-worker, low traffic demo) but will not scale.
- Fix approach: Wrap DB calls in `anyio.to_thread.run_sync` / `asyncio.to_thread`, or migrate to an async DB driver (e.g., `aiosqlite`) as planned for the Postgres migration.

**Single shared `sqlite3.Connection` with no locking across concurrent requests:**
- Issue: One connection is created at startup and stored in `app.state.conn` (`src/relay/main.py:23-26`), shared across all requests with `check_same_thread=False` and no explicit locking (`src/relay/db.py:61-65`).
- Files: `src/relay/db.py`, `src/relay/main.py`
- Impact: Concurrent writes from overlapping `/tickets/{id}/process` runs can raise `sqlite3.OperationalError: database is locked`, especially since SQLite serializes writers.
- Fix approach: Use a connection pool or `aiosqlite` with WAL mode, or move to Postgres per the phase-6 plan in `docs/PROJECT_BRIEF.md`.

**Keyword-only `search_docs` retrieval, not semantic (RAG in name only):**
- Issue: `search_docs` (`src/relay/tools.py:48-64`) is a naive term-count scorer over markdown files, not embeddings-based retrieval, despite the project brief describing "RAG."
- Files: `src/relay/tools.py:48-64`
- Impact: Retrieval quality is weak for paraphrased or semantically-related queries that don't share exact keywords; grounding failures are more likely on eval cases phrased differently from the docs.
- Fix approach: Replace with an embeddings-based retriever (the docstring already flags this as the intended phase-1→later upgrade path).

**`mcp_allow_writes` defaults to `True`:**
- Issue: `Settings.mcp_allow_writes` (`src/relay/config.py:19`) defaults to `True`, so any MCP client connecting over stdio can immediately invoke write-tier tools (`send_reply`, `create_escalation`, `set_category`, `create_ticket`) with no explicit opt-in.
- Files: `src/relay/config.py:19`, `src/relay/mcp_server.py:125-127`
- Impact: A misconfigured or untrusted MCP client gets destructive/side-effecting access by default.
- Fix approach: Default to `False` (read-only) and require an explicit env var to enable writes, matching a safer default posture.

**Committed SQLite database file in the working tree:**
- Issue: `relay.db` (32KB) exists at the repo root and is only excluded via `.gitignore` pattern `*.db` — verify it was never committed historically.
- Files: `relay.db`
- Impact: If ever force-added or the gitignore pattern is bypassed, seed/dev data (customer emails, ticket bodies) could leak into version control.
- Fix approach: No action needed as long as `.gitignore` is respected; consider adding a pre-commit hook to block `*.db` files defensively.

## Known Bugs

None identified through static review; no open bug-tracking issues, TODO/FIXME/HACK/XXX comments found in `src/`.

## Security Considerations

**No CORS policy configured:**
- Risk: FastAPI app (`src/relay/main.py:32`) has no `CORSMiddleware` configured, so default same-origin browser restrictions apply, but there is also no explicit allow-list — if a minimal web UI is later added on a different origin, CORS will need explicit configuration.
- Files: `src/relay/main.py`
- Current mitigation: None; browsers default-deny cross-origin requests without CORS headers, so this is not currently exploitable but is undocumented.
- Recommendations: Add explicit `CORSMiddleware` with an allow-list when a frontend is introduced.

**API key handling relies entirely on environment variables with no secret rotation or scoping:**
- Risk: `ANTHROPIC_API_KEY` is read via `pydantic-settings` (`src/relay/config.py:11`) directly from environment/`.env`; no key rotation, scoping, or vault integration.
- Files: `src/relay/config.py`, `.env` (present locally, gitignored), `fly.toml` (secrets set via `fly secrets set`)
- Current mitigation: `.env` and `.env.*` are gitignored (`.gitignore` lines 10-12); production uses Fly.io secrets.
- Recommendations: Acceptable for a demo/portfolio project; note for future hardening if this becomes multi-tenant.

**Unauthenticated write endpoints combined with unbounded ticket creation:**
- Risk: `POST /tickets` (`src/relay/main.py:46-54`) has no auth and no per-IP limits, allowing unlimited ticket rows to be inserted.
- Files: `src/relay/main.py:46-54`
- Current mitigation: Pydantic validation caps `subject` (200 chars) and `body` (10,000 chars) via `TicketCreate` (`src/relay/models.py:22-25`), bounding per-request payload size only.
- Recommendations: Add auth (see Tech Debt above) and consider a max-open-tickets-per-email check.

## Performance Bottlenecks

**`search_docs` re-reads every KB file from disk on every call:**
- Problem: `search_docs` (`src/relay/tools.py:56-57`) globs and reads every `.md` file in `kb/` fresh on each tool invocation, with no caching.
- Files: `src/relay/tools.py:48-64`
- Cause: No in-memory cache of parsed/scored documents; the KB is small today (`kb/` has 3 files) so impact is negligible, but this won't scale with a larger knowledge base.
- Improvement path: Cache file contents in memory at startup or on first access, invalidate on file-mtime change.

**Synchronous blocking I/O in the async event loop (see Tech Debt: SQLite blocking calls above).**

## Fragile Areas

**Agent loop step/budget interplay is manually threaded through a generator:**
- Files: `src/relay/agent.py:58-225`
- Why fragile: `run_ticket` is a long generator function mixing OpenTelemetry span management, budget tracking, SSE event yielding, and tool execution in one linear flow. The code comments explicitly note the span-context handling is tricky because "execution suspends at every yield" (`src/relay/agent.py:83-85`). Any change to control flow (e.g., adding retries, parallel tool calls) risks breaking span parenting or budget accounting.
- Safe modification: Keep new logic inside the existing per-step loop; avoid introducing `async with` current-span context managers across `yield` boundaries; add tests in `tests/test_observability.py` and `tests/test_guardrails.py` for any new state transitions.
- Test coverage: `tests/test_api.py` (3 tests), `tests/test_guardrails.py` (11 tests), `tests/test_observability.py` (7 tests) exercise parts of this, but no test explicitly covers the cross-ticket `ticket_id` mismatch scenario noted above.

**Terminal-tool detection (`TERMINAL_TOOLS`) is a hardcoded set:**
- Files: `src/relay/agent.py:34`
- Why fragile: Adding a new write tool that should end a run (e.g., a future `refund_customer` tool) requires remembering to add it to `TERMINAL_TOOLS`; forgetting this silently changes resolution semantics (the run would report `ended_without_action` instead of a proper resolution).
- Safe modification: When adding new terminal tools, update both `src/relay/tools.py` registry and `TERMINAL_TOOLS` in `src/relay/agent.py`, and add a corresponding eval case in `evals/`.

## Scaling Limits

**SQLite as the datastore:**
- Current capacity: Fine for a single-instance demo with low concurrent traffic (Fly.io config sets `min_machines_running = 0`, `auto_stop_machines = 'stop'` — single ephemeral instance).
- Limit: SQLite write concurrency and the single shared connection (see Tech Debt) will not scale beyond light demo traffic; `fly.toml` mounts a single volume (`relay_data`) which does not support multi-machine access.
- Scaling path: `src/relay/db.py` docstring already flags the plan: "Phase 1 uses the stdlib driver; Postgres comes with deployment (phase 6)" — migrate to Postgres with an async driver for horizontal scaling.

**Single Fly.io machine, `min_machines_running = 0`:**
- Current capacity: Cold-start latency on first request after idle (machine must boot).
- Limit: No horizontal scaling configured; `fly.toml` has one `[[vm]]` block sized `shared-cpu-1x` / `512mb`.
- Scaling path: Increase `min_machines_running`, add multiple machines with a shared Postgres backend once SQLite is replaced.

## Dependencies at Risk

None identified — dependencies in `pyproject.toml` (`anthropic`, `fastapi`, `uvicorn`, `pydantic`, `opentelemetry-sdk`, `mcp`) are current, actively maintained, and version-pinned with lower bounds only (`>=`), which means CI could pick up breaking major-version updates unnoticed since no upper bounds or lockfile is committed.

- Files: `pyproject.toml` (no `uv.lock`/`poetry.lock`/`requirements.lock` present in repo listing)
- Impact: CI installs `pip install -e ".[dev]"` fresh each run with only lower-bound version constraints; a new major release of `anthropic` or `fastapi` could break CI or production without warning.
- Migration plan: Consider adding a lockfile (e.g., via `uv pip compile` or `pip-tools`) for reproducible CI/production installs.

## Missing Critical Features

**No request/response schema versioning for the SSE event stream:**
- Problem: `AgentEvent` (`src/relay/models.py:38-42`) has a loosely-typed `data: dict[str, Any]` field with no per-event-type schema; MCP/API consumers must know the shape of each event type (`usage`, `resolution`, `error`, `text`, `tool_use`, `tool_result`) by convention only.
- Blocks: Safe evolution of the event contract without breaking existing SSE clients; no compile-time guarantee that a given event's `data` matches expectations.

**No graceful shutdown draining for in-flight SSE streams:**
- Problem: `lifespan` (`src/relay/main.py:19-29`) closes the DB connection (`conn.close()`) on shutdown without waiting for in-flight `process_ticket` streams to finish.
- Files: `src/relay/main.py:19-29`
- Blocks: Clean deploys/restarts on Fly.io could sever an in-progress agent run mid-stream, leaving a ticket in an inconsistent state (no `runs` row recorded since `record_run` happens after the stream completes).

## Test Coverage Gaps

**Cross-ticket `ticket_id` mismatch is untested:**
- What's not tested: No test verifies behavior when the model's tool call supplies a `ticket_id` different from the ticket being processed by `run_ticket`.
- Files: `tests/test_guardrails.py`, `tests/test_api.py`
- Risk: The unguarded behavior described in Tech Debt above could ship without any test catching a regression or the eventual fix.
- Priority: High

**No test for concurrent `/tickets/{id}/process` requests against the shared SQLite connection:**
- What's not tested: Behavior under concurrent write load (potential `database is locked` errors) is not exercised in `tests/test_api.py`.
- Files: `tests/test_api.py` (3 tests only)
- Risk: Concurrency bugs would only surface in production/demo traffic, not CI.
- Priority: Medium

**No test asserting unauthenticated access is intentional/documented:**
- What's not tested: No test documents or enforces the current no-auth posture (e.g., a test that would fail once auth is added, forcing a deliberate update).
- Files: `tests/test_api.py`
- Risk: Low direct risk, but means the missing-auth issue could persist indefinitely without a forcing function.
- Priority: Low

---

*Concerns audit: 2026-08-05*

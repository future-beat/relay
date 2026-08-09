# Testing Patterns

**Analysis Date:** 2026-08-05

## Test Framework

**Runner:**
- `pytest` >= 8.0, config in `pyproject.toml` (`[tool.pytest.ini_options]`)
- `testpaths = ["tests"]`, `asyncio_mode = "auto"` (`pyproject.toml:35-37`) — async test functions need no `@pytest.mark.asyncio` decorator, they're detected automatically by `pytest-asyncio`

**Assertion Library:**
- Plain `assert` statements (pytest's rewritten assertions), no separate assertion library (no `unittest`, no `assertpy`)
- `pytest.raises(ExceptionType, match="regex")` for exception assertions
- `pytest.approx(...)` for float comparisons (e.g. cost calculations in `tests/test_guardrails.py:116`)

**Run Commands:**
```bash
pytest -q                    # Run all tests (used in CI, .github/workflows/ci.yml)
pytest tests/test_tools.py   # Run a single file
pytest -k test_dry_run       # Run tests matching a keyword
ruff check src tests         # Lint (run alongside tests in CI)
```
No coverage tool is configured (no `pytest-cov`, no coverage threshold in CI).

## Test File Organization

**Location:**
- All tests live in a top-level `tests/` directory, separate from `src/relay/` (not co-located).

**Naming:**
- One test file per source module, named `test_<module>.py`: `test_api.py` ↔ `src/relay/main.py` (API layer), `test_tools.py` ↔ `src/relay/tools.py`, `test_guardrails.py` ↔ `src/relay/guardrails.py` + `src/relay/agent.py` (guardrail behavior is tested through the agent loop, not in isolation), `test_mcp.py` ↔ `src/relay/mcp_server.py`, `test_observability.py` ↔ `src/relay/telemetry.py`, `test_evals.py` ↔ `src/relay/evals.py`.

**Structure:**
```
tests/
├── conftest.py       # shared fixtures: in-memory sqlite `conn`, `registry`
├── helpers.py        # FakeClient and response-builder test doubles (shared, imported by name)
├── test_api.py        # FastAPI endpoint tests via TestClient
├── test_evals.py       # golden dataset shape + outcome-extraction unit tests
├── test_guardrails.py  # agent loop behavior: validation, dry-run policy, budget, API errors
├── test_mcp.py          # MCP tool registry exposure and call semantics
├── test_observability.py # structured logging, tracing setup, /metrics, dashboard
└── test_tools.py         # individual tool executor functions against sqlite
```
No `__init__.py` in `tests/` — pytest uses rootdir-relative test discovery.

## Test Structure

**Suite Organization:**
Tests are flat functions grouped by section comments within a file, not nested classes:
```python
# tests/test_guardrails.py
# --- input validation ---

def test_validate_rejects_short_reply():
    with pytest.raises(ToolInputError, match="body"):
        validate_tool_input(SendReplyInput, {"ticket_id": 1, "body": "ok"})

# --- write policy / dry-run ---

async def test_dry_run_denies_write_tools_but_allows_reads(registry):
    ...

# --- budget ---
# --- API failure ---
```
Section comment headers (`# --- <concern> ---`) partition a file's tests by behavior, not by which function is under test — this groups tests around the guarantee being verified (e.g. "budget", "dry-run policy") which matches how the agent loop's guardrails are described in `src/relay/agent.py`'s module docstring.

**Patterns:**
- Setup via pytest fixtures (`conn`, `registry` from `tests/conftest.py`), no `setUp`/`tearDown` classes.
- Fixtures use `yield` for teardown: `conn` fixture opens an in-memory sqlite connection, yields it, then closes it (`tests/conftest.py:11-16`).
- Async test functions take no special decorator (relies on `asyncio_mode = "auto"`); a local `collect(gen)` helper drains an async generator into a list for assertion: `async def collect(gen): return [event async for event in gen]` (`tests/test_guardrails.py:52`).

## Mocking

**Framework:** No mocking library (`unittest.mock`, `pytest-mock`) — the codebase uses **hand-written fake objects** built from `types.SimpleNamespace` instead of patching.

**Patterns:**
```python
# tests/helpers.py (shared) and duplicated locally in tests/test_guardrails.py
class FakeClient:
    """Plays back scripted responses in place of the Claude API."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return next(self._responses)
```
`FakeClient` is constructed with a list of scripted `response(...)` objects (built via `text_block`/`tool_use_block`/`usage`/`response` helpers) and played back in order via `messages.create`, exactly mirroring the `AsyncAnthropic` client's shape used in `src/relay/agent.py`. Real sqlite (`:memory:`) is used instead of mocking the DB layer — only the external Claude API boundary is faked.

Note: `tests/helpers.py` and the top of `tests/test_guardrails.py` define near-identical `FakeClient`/`text_block`/`tool_use_block`/`usage`/`response` helpers independently — `test_observability.py` imports from `helpers.py` (`from helpers import FakeClient, response, text_block, tool_use_block`) while `test_guardrails.py` keeps its own private copies (`_text`, `_tool_use`, `_usage`, `_response`). When adding new agent-loop tests, prefer importing from `tests/helpers.py` rather than re-defining local doubles.

**What to Mock:**
- The Anthropic API client only (`AsyncAnthropic` / `app.state.client`), since it's the one true external dependency and the one that costs real money/network calls.
- Environment variables via `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")` (`tests/test_api.py:12`) so app startup doesn't fail without a real key.
- Settings fields via `monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")` to redirect the FastAPI app's persistent DB into a pytest tmp dir.

**What NOT to Mock:**
- SQLite — always exercised for real, either via an in-memory connection (`conn` fixture, unit/integration tests of tools) or a `tmp_path`-scoped file DB (API-level tests through `TestClient`).
- The tool registry / tool executors — real functions from `src/relay/tools.py` run against the real (in-memory) DB in every test.
- Pydantic validation — always run for real (`validate_tool_input`), never bypassed.

## Fixtures and Factories

**Test Data:**
```python
# tests/conftest.py
@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)          # applies schema + seeds SEED_CUSTOMERS
    yield conn
    conn.close()

@pytest.fixture()
def registry(conn):
    return build_registry(conn, KB_DIR)   # KB_DIR points at the real kb/ markdown files
```
Seed data (customers) comes from the production seed list `SEED_CUSTOMERS` in `src/relay/db.py`, reused directly by tests (`tests/test_evals.py` imports `SEED_CUSTOMERS` to validate the golden dataset only references real seeded customers).

Module-level constant fixtures for scenario data, not factory functions: e.g. `TICKET = {...}` dict at the top of `tests/test_guardrails.py:14-19`, reused across many test functions in that file.

**Location:**
- Shared fixtures: `tests/conftest.py` (auto-discovered by pytest, available to every test file without import)
- Shared test doubles: `tests/helpers.py` (must be imported explicitly by name)
- Golden/eval dataset: loaded via `load_golden()` from `src/relay/evals.py`, not stored under `tests/` — it's part of the shipped `relay.evals` module and also drives the separate eval-run CI workflow (`.github/workflows/evals.yml`).

## Coverage

**Requirements:** None enforced — no coverage tool configured, no threshold gate in CI.

**View Coverage:**
Not applicable; would require adding `pytest-cov` (not currently a dependency).

## Test Types

**Unit Tests:**
- Pure logic with no I/O: `test_validate_rejects_short_reply`, `test_budget_accumulates_cost` (`tests/test_guardrails.py`), `test_percentile`-style pieces of `run_metrics`.
- Tool executor functions run in isolation against an in-memory sqlite DB (`tests/test_tools.py`).

**Integration Tests:**
- Agent loop end-to-end against a `FakeClient` and real (in-memory) DB and tool registry — covers guardrail enforcement (validation, dry-run denial, budget abort, API-error handling) as full request→event-stream flows (`tests/test_guardrails.py`).
- FastAPI endpoint tests via `fastapi.testclient.TestClient` against a real (tmp-file) DB and app lifespan, asserting HTTP status codes and response bodies (`tests/test_api.py`).
- MCP tool-call semantics tested through the public `call_mcp_tool`/`list_mcp_tools` surface, not internal functions (`tests/test_mcp.py`).

**E2E Tests:**
- No browser/UI E2E framework. The Docker CI job (`.github/workflows/ci.yml`, `docker` job) is the closest thing to an E2E smoke test: it builds the container image and polls `GET /health` until it responds.
- A separate, on-demand "Evals" GitHub Actions workflow (`.github/workflows/evals.yml`, triggered via `workflow_dispatch`) runs `python -m relay.evals` against the real Claude API and a golden dataset, failing the job if the pass rate drops below a configurable threshold (default 0.8). This is a model/agent-quality eval suite, not a unit/integration test suite, and costs real API tokens — it is intentionally excluded from the on-push `pytest` run.

## Common Patterns

**Async Testing:**
```python
# tests/test_guardrails.py
async def collect(gen):
    return [event async for event in gen]

async def test_usage_events_streamed(registry):
    client = FakeClient([_response([_text("All done.")], stop_reason="end_turn")])
    events = await collect(run_ticket(client, registry, TICKET))
    usage = next(e for e in events if e.type == "usage")
    assert usage.data["input_tokens"] == 1000
```
Async test functions are plain `async def test_...` — no explicit marker needed due to `asyncio_mode = "auto"`.

**Error Testing:**
```python
# tests/test_mcp.py
def test_write_tool_denied_when_read_only(mcp_registry):
    with pytest.raises(RuntimeError, match="dry-run"):
        call_mcp_tool(mcp_registry, ToolPolicy(allow_writes=False), "create_ticket", {
            "customer_email": "ava@acmecorp.com", "subject": "x", "body": "y",
        })
```
```python
# tests/test_guardrails.py — error paths surfaced as data, not exceptions, at the agent-loop level
async def test_api_error_yields_structured_event(registry):
    events = await collect(run_ticket(ErrorClient(), registry, TICKET))
    assert events == [events[0]]
    assert events[0].type == "error"
    assert events[0].data["reason"] == "api_connection_error"
```
Two distinct error-testing idioms depending on the layer: MCP/CLI-facing code that raises exceptions is tested with `pytest.raises(..., match=...)`; the streaming agent loop, which never raises across its public boundary, is tested by asserting on the terminal `AgentEvent(type="error", ...)` in the collected event list instead.

---

*Testing analysis: 2026-08-05*

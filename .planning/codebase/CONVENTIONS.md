# Coding Conventions

**Analysis Date:** 2026-08-05

## Naming Patterns

**Files:**
- Lowercase snake_case module names, one concern per module: `src/relay/agent.py`, `src/relay/guardrails.py`, `src/relay/tools.py`, `src/relay/telemetry.py`, `src/relay/mcp_server.py`
- No `utils.py`/`helpers.py` grab-bag in `src/` — shared code lives in the module that owns the concept (e.g. Pydantic input models live in `src/relay/guardrails.py` next to the validation function that uses them, not in a generic `models.py`)
- Test helper module is explicitly named `tests/helpers.py` (test doubles only, imported by name from tests)

**Functions:**
- snake_case, verb-first for actions: `validate_tool_input`, `build_registry`, `run_ticket`, `record_run`, `run_metrics`
- Private/internal helpers prefixed with a single underscore: `_execute_guarded` (`src/relay/agent.py:40`), `_percentile` (`src/relay/telemetry.py:76`), `_get_ticket` (`src/relay/main.py:146`)
- Test functions are `test_<behavior_described_in_words>`, e.g. `test_dry_run_never_writes_to_db`, `test_normal_run_ending_without_action_is_error` — the name states the expected outcome, not just the function under test

**Variables:**
- snake_case throughout; no Hungarian notation
- Short-lived loop/temp names are terse (`cur`, `row`, `conn`, `exc`), domain variables are descriptive (`resolved_via`, `last_stop_reason`, `tool_results`)

**Types:**
- Pydantic `BaseModel` subclasses in PascalCase suffixed by role: `*Input` for tool argument schemas (`SendReplyInput`, `LookupCustomerInput` in `src/relay/guardrails.py`), plain nouns for domain records (`Ticket`, `AgentEvent` in `src/relay/models.py`)
- `Enum` subclasses use lowercase string values matching the wire format: `TicketCategory.billing = "billing"` (`src/relay/models.py:8`)
- Dataclasses used for lightweight, non-validated structs: `@dataclass(frozen=True) class ToolSpec` (`src/relay/tools.py:26`), `@dataclass class ToolPolicy` (`src/relay/guardrails.py:55`)

## Code Style

**Formatting:**
- No formatter config found (no `.prettierrc`/`black` config); `ruff` is the sole tool for both lint and style enforcement
- Line length capped at 100 (`[tool.ruff] line-length = 100` in `pyproject.toml:40`)
- Trailing commas used consistently in multi-line literals and call args

**Linting:**
- `ruff check src tests` run in CI (`.github/workflows/ci.yml`)
- No custom `select`/`ignore` rule set configured beyond defaults — relies on ruff's default rule set plus `line-length`
- Inline `# noqa` used sparingly and always justified with a comment, e.g. `except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed` (`src/relay/agent.py:54`)

## Import Organization

**Order:**
1. Standard library imports (`json`, `logging`, `time`, `sqlite3`, `re`, `os`)
2. Third-party imports (`anthropic`, `fastapi`, `pydantic`, `opentelemetry`)
3. Local relative imports last, using `.` prefix (`from .config import settings`, `from .models import AgentEvent`)

Each group is separated by a blank line; within a group, imports are alphabetized (isort-style), matching ruff's default import sort.

**Path Aliases:**
- None. All intra-package imports are relative (`from .guardrails import ...`), and tests import the installed package as `relay.*` (`from relay.agent import run_ticket`) since the project is installed in editable mode (`pip install -e ".[dev]"`).

## Error Handling

**Patterns:**
- Domain-specific exceptions instead of bare exceptions: `ToolInputError` (`src/relay/guardrails.py:51`) raised from Pydantic `ValidationError` with a message built for the model to read: `f"invalid tool input — {problems}"`.
- Tool execution never raises into the agent loop — `_execute_guarded` (`src/relay/agent.py:40`) catches `Exception` broadly at the tool boundary only, converting it to a `{"error": ...}` JSON string returned to the model. This is the single sanctioned broad-except in the codebase, explicitly commented.
- API failures from the Anthropic SDK are caught narrowly by type (`anthropic.APIConnectionError`, `anthropic.APIStatusError`) and turned into structured `AgentEvent(type="error", ...)` — never allowed to raise a stack trace to the caller (`src/relay/agent.py:127-139`).
- FastAPI routes raise `HTTPException` with explicit status codes and short messages, no custom exception handler middleware: `raise HTTPException(404, "ticket not found")` (`src/relay/main.py:151`), `raise HTTPException(409, f"ticket is already {ticket.status.value}")` (`src/relay/main.py:72`).
- MCP tool boundary (`src/relay/mcp_server.py`) converts policy denials and validation errors into `RuntimeError` with a matching message, asserted via `pytest.raises(RuntimeError, match="...")` in tests.
- No bare `except:` anywhere in the codebase; every catch names its exception type(s).

## Logging

**Framework:** stdlib `logging`, configured once at startup via `configure_logging()` (`src/relay/telemetry.py:37`) to emit single-line JSON via a custom `JsonFormatter`.

**Patterns:**
- Logger acquired per-module with a dotted name matching the module's role: `logger = logging.getLogger("relay.agent")` (`src/relay/agent.py:36`).
- Log messages are short, dotted event names in past/imperative tense used as the `event` field, not free-form sentences: `"run.start"`, `"model.response"`, `"tool.executed"`, `"run.end"`, `"run.budget_exceeded"`.
- Structured context is always passed via `extra={"ctx": {...}}`, never string-interpolated into the message: `logger.info("tool.executed", extra={"ctx": {"ticket_id": ticket["id"], "tool": block.name, "is_error": is_error}})` (`src/relay/agent.py:165`).
- `uvicorn.access` logger is explicitly quieted to `WARNING` to avoid duplicating request context already captured by the app's own structured logs (`src/relay/telemetry.py:44`).

## Comments

**When to Comment:**
- Module-level docstrings explain *why*, referencing the project phase and design rationale, not just *what*: see the top of `src/relay/agent.py` ("No framework: the loop is a plain request -> tool-execute -> append cycle so the control flow, guardrails, and event stream are fully visible and testable.") and `src/relay/telemetry.py`.
- Inline comments justify non-obvious choices at the exact line they apply to, e.g. explaining why a span is parented explicitly instead of made "current" in a generator (`src/relay/agent.py:83-85`), or why cache-read/write tokens are priced with multipliers (`src/relay/guardrails.py:74-77`).
- No comments restating what the code already says.

**JSDoc/TSDoc:** N/A (Python codebase). Docstrings are triple-quoted, one-paragraph, present on modules and non-trivial functions/classes; simple one-liners (getters, small pure functions) are left undocumented.

## Function Design

**Size:** Small and single-purpose; the largest function is the agent loop itself, `run_ticket` (~165 lines, `src/relay/agent.py:58`), which is long because it's a single sequential state machine — broken into a tight helper (`_execute_guarded`) for the one reusable piece of logic.

**Parameters:**
- Keyword-only parameters used for functions with more than 2-3 args, enforced with `*`: `record_run(conn, *, ticket_id, model, duration_ms, ...)` (`src/relay/telemetry.py:56`).
- Optional collaborators default to `None` and are constructed inline: `policy: ToolPolicy | None = None` then `policy = policy or ToolPolicy()` (`src/relay/agent.py:62,66`) — same pattern for `budget`.
- Type hints are mandatory on all function signatures, using modern `X | None` union syntax (Python 3.11+) rather than `Optional[X]`.

**Return Values:**
- Tool executor functions return JSON-encoded strings (`json.dumps(...)`), not dicts, because that's the wire format the model consumes: see every function in `src/relay/tools.py`.
- Functions that can fail without throwing return a `(result, is_error)` tuple rather than raising, when the failure is an expected, model-facing outcome: `_execute_guarded(...) -> tuple[str, bool]` (`src/relay/agent.py:40`).
- Async generators (`AsyncIterator[AgentEvent]`) are used for streaming multi-step results back to callers, e.g. `run_ticket` yields one `AgentEvent` per step rather than returning a final list.

## Module Design

**Exports:** No `__all__` lists; modules export by convention (everything not underscore-prefixed is public). `src/relay/__init__.py` exports only `__version__`.

**Barrel Files:** None. Callers import directly from the owning module (`from .guardrails import RunBudget, ToolPolicy, validate_tool_input`), keeping import provenance explicit.

**Dependency injection:** Stateful collaborators (`sqlite3.Connection`, `kb_dir: Path`) are passed into factory functions rather than imported as globals, e.g. `build_registry(conn: sqlite3.Connection, kb_dir: Path)` (`src/relay/tools.py:93`) closes over them in per-tool lambdas. This is what makes tools swappable in tests via the `conn`/`registry` fixtures in `tests/conftest.py`.

**Settings:** Centralized in one `pydantic_settings.BaseSettings` singleton, `settings` (`src/relay/config.py:27`), imported wherever config is needed rather than passed through every call chain. Environment variables use a `RELAY_` prefix except where interop with the Anthropic SDK's own env lookup requires the bare name (`ANTHROPIC_API_KEY`, via `validation_alias`).

---

*Convention analysis: 2026-08-05*

---
phase: 02-async-safe-data-layer-graceful-shutdown
plan: 02
subsystem: infra
tags: [asyncio, graceful-shutdown, sse, fastapi, lifespan, pydantic-settings]

# Dependency graph
requires:
  - phase: 01-security-perimeter
    provides: "ratelimit.py's token-identified in-flight reservation tracker (reserve_run/release_run), the analog this registry's token/idempotence shape copies; record_run moved into event_stream's finally (CR-01), which is the code the drain exists to protect"
provides:
  - "src/relay/runs.py — RunRegistry: register/deregister/active/snapshot/drain plus a public `draining` flag"
  - "ActiveRun frozen dataclass (ticket_id, monotonic started_at) — the record Phase 5's live feed renders"
  - "settings.shutdown_drain_seconds (RELAY_SHUTDOWN_DRAIN_SECONDS, default 5.0)"
  - "tests/test_lifecycle.py — the phase's lifecycle suite, opened with DATA-02-b/c/d drain coverage"
affects: [02-04 lifespan and event_stream wiring, 02-05 lifecycle integration tests, phase-5 dashboard live feed]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "asyncio.Event + asyncio.wait_for as the drain primitive (first async coordination primitive in src/)"
    - "Per-app-startup registry instance on app.state rather than module-level globals"

key-files:
  created:
    - src/relay/runs.py
    - tests/test_lifecycle.py
  modified:
    - src/relay/config.py
    - .env.example

key-decisions:
  - "Drain is event-driven (asyncio.wait_for on an Event), never a polling loop — a poll adds its interval to every clean teardown for no benefit"
  - "drain() sets draining = True before the idle fast-path return, so refusing new work holds on both paths"
  - "No TTL on registry entries: register/deregister are balanced because 02-04 registers inside the generator, unlike ratelimit.py's handler-claimed reservations"
  - "except TimeoutError (the 3.11+ builtin), and return False rather than raising into lifespan teardown"
  - "No reset hook and no conftest.py autouse entry — registry state is per-app, not process-wide"

patterns-established:
  - "RunRegistry: token-identified in-flight tracking with idempotent removal, instantiated per app startup"
  - "shutdown.drain_started / drain_complete / drain_timeout structured log events carrying ids and counts only (ASVS V7)"
  - "tests/test_lifecycle.py constructs its own RunRegistry per test so the asyncio.Event binds to the right loop"

requirements-completed: [DATA-02]

# Metrics
duration: 12min
completed: 2026-08-09
---

# Phase 2 Plan 02: In-Flight Run Registry & Shutdown Drain Setting Summary

**`RunRegistry` — an `asyncio.Event`-driven in-flight run tracker whose bounded `drain()` returns False on timeout instead of hanging teardown, plus the `RELAY_SHUTDOWN_DRAIN_SECONDS` budget it is measured against.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-08-09
- **Completed:** 2026-08-09
- **Tasks:** 3
- **Files modified:** 4 (2 created, 2 modified)

## Accomplishments

- `RunRegistry` delivers exactly the interface plan 02-04 and Phase 5 consume — `register`/`deregister`/`active`/`snapshot`/`drain` plus a public `draining` flag — with no module-level singleton and no TTL.
- `drain()` is event-driven and bounded: `asyncio.wait_for(self._idle.wait(), timeout)` wakes on the last deregistration, and a timeout logs `shutdown.drain_timeout` and returns `False` rather than raising into lifespan teardown (T-02-05).
- The idle fast path returns before ever suspending, so an idle machine's shutdown costs nothing — measured at under 50 ms against a 5.0 s timeout.
- `settings.shutdown_drain_seconds` (5.0) is the innermost of the three nested shutdown windows, documented as such in both `config.py` and `.env.example`.
- Three drain tests (DATA-02-b/c/d) green; suite went 110 → 113 with zero edits to any existing test.

## Task Commits

1. **Task 1: `src/relay/runs.py` — the in-flight run registry** — `ecfc6a2` (feat)
2. **Task 2: `shutdown_drain_seconds` setting and its `.env.example` entry** — `6d29f62` (feat)
3. **Task 3: `tests/test_lifecycle.py` — drain semantics (DATA-02-b/c/d)** — `bdfe1ba` (test)

## Files Created/Modified

- `src/relay/runs.py` (new, 114 lines) — `ActiveRun` frozen dataclass and `RunRegistry`. Tokens from `itertools.count()`, `time.monotonic()` timestamps, idempotent `dict.pop(token, None)` removal, an `asyncio.Event` that is set exactly when the map is empty, and a bounded `drain()`.
- `src/relay/config.py` — appended a trailing phase-grouped block with `shutdown_drain_seconds: float = 5.0`. No `Field(...)` alias; `env_prefix="RELAY_"` already yields `RELAY_SHUTDOWN_DRAIN_SECONDS`.
- `.env.example` — appended `RELAY_SHUTDOWN_DRAIN_SECONDS=5.0` under a two-line comment, between the Phase 1 security block and the OpenTelemetry block. Append-only; no existing line touched.
- `tests/test_lifecycle.py` (new, 60 lines) — module docstring scoping the suite to the offload seam, the registry, and drain; a `# --- shutdown drain ---` separator; three async tests.

## Decisions Made

- **Three deliberate divergences from `ratelimit.py`, each commented at the point it applies** (per 02-PATTERNS.md):
  1. `RunRegistry` is a class instantiated per app startup, not module globals — an `asyncio.Event` binds to the loop it is first awaited on, so shared module state would outlive a `TestClient`'s loop. `ratelimit.py` gets away with globals only because `MemoryStorage` constructs safely outside a running loop.
  2. No TTL. `reserve_run()` needs one because it is claimed in the handler and released in the generator; the registry registers inside the generator, so registration is balanced by construction. The comment says so explicitly to stop a future reader "fixing" it.
  3. No `reset_limits()` equivalent, and nothing added to `conftest.py`'s autouse reset.
- **`draining = True` is set before the idle early-return**, not after the wait. Refusing new work is the first half of the drain contract; setting it only on the slow path would silently skip it for the common case.
- **`snapshot()` returns live `ActiveRun` records** (not a serialised projection) — Phase 5's feed shapes them, this module does not guess at the wire format.
- **Log context carries `ticket_id`s and counts only.** `ctx` passes straight through `JsonFormatter` to stdout, so ticket bodies, customer emails and keys are out of scope by construction (T-02-07).

## Deviations from Plan

None — plan executed exactly as written. The verified implementation in 02-RESEARCH.md § Code Examples §3 was followed; the only additions on top of it were the mandated explanatory comments and short docstrings, which the plan's action text required.

## Issues Encountered

- **Worktree arrived on a stale base.** `git merge-base HEAD 4b91681` resolved to `fd57477` (the Phase 1 merge commit), not the required base. Corrected with the `git reset --hard 4b91681` prescribed by the branch check, then confirmed `src/relay/ratelimit.py` was present before doing any work. No content was lost — the worktree had no local changes.

## Verification

| Gate | Result |
|------|--------|
| `pytest -q` | **113 passed, 0 failed** (floor was ≥ 113) |
| `ruff check src tests` | `All checks passed!` |
| `pytest -q tests/test_lifecycle.py` | 3 passed in 0.06 s (budget was < 5 s) |
| Task 1 inline probe (register/deregister idempotence/snapshot/idle drain/draining) | prints `ok` |
| `RELAY_SHUTDOWN_DRAIN_SECONDS=7.5` override | reads back `7.5` with no explicit alias |
| D-03 gate (`mcp_server.py`, `evals.py`, 8 existing test files) | prints nothing — untouched |
| `grep -c 'asyncio.wait_for' src/relay/runs.py` | `1` |
| `grep -c 'while ' src/relay/runs.py` | `0` — event-driven, never polling |
| `grep -c 'asyncio.TimeoutError' src/relay/runs.py` | `0`; `except TimeoutError` at line 96 |
| `grep -c 'expires_at' src/relay/runs.py` | `0`; no `_prune`/`TTL_S` outside comments |
| `grep -o 'shutdown\.[a-z_]*'` | exactly `drain_complete`, `drain_started`, `drain_timeout` |
| `grep -c 'body\|customer_email\|api_key' src/relay/runs.py` | `0` (ASVS V7) |
| `grep -c 'from .main\|import main' src/relay/runs.py` | `0` — testable without a `TestClient` |
| No middleware introduced | `0` matches for `BaseHTTPMiddleware`/`app.middleware` |
| `git diff --name-only 4b91681 HEAD` | exactly the 4 files in `files_modified` |

## Threat Model Coverage

- **T-02-05 (DoS via hanging drain)** — mitigated. `asyncio.wait_for` bounds the wait; `except TimeoutError` returns `False` and never raises into teardown. Proven by `test_drain_times_out_rather_than_hanging_shutdown`.
- **T-02-06 (registry entry leak)** — mitigated by construction. No TTL and no expiry logic exist; `grep -c 'expires_at'` is `0`. The balancing half (registering inside the generator) is plan 02-04's.
- **T-02-07 (log info disclosure)** — mitigated. `ctx` carries an `active` count and a list of `ticket_id`s only.
- **T-02-08 (premature `conn.close()` losing a cost row)** — transferred as planned. The mechanism ships here; the lifespan ordering that actually closes the race is plan 02-04's.
- **T-02-SC (dependency tampering)** — accepted; this plan installed nothing and did not touch `pyproject.toml`.

No new threat flags. This plan adds no network endpoint, no auth path, no file access and no schema change — `runs.py` is pure in-process state with no HTTP surface until 02-04 wires it.

## Known Stubs

None. Every method on `RunRegistry` is fully implemented and directly covered. The registry is not yet reachable from HTTP, but that is the plan's stated boundary ("Nothing in this plan touches the HTTP edge"), not a stub — plan 02-04 owns the `lifespan`/`event_stream` wiring and plan 02-05 owns the integration tests (DATA-02-a/e) that exercise it end to end.

## TDD Gate Compliance

Task 3 carried `tdd="true"`, but the plan deliberately ordered implementation (Task 1) before its tests (Task 3), so there is no failing-RED commit for the drain behaviour. This is the plan's intended sequencing — Task 3's tests are unit tests over a primitive whose interface Task 1 was specified to deliver verbatim from 02-RESEARCH.md — not a skipped gate. Git log shows `feat` (`ecfc6a2`) then `test` (`bdfe1ba`); the RED gate is absent by design.

## User Setup Required

None. `RELAY_SHUTDOWN_DRAIN_SECONDS` has a working default, so an unset environment is fine. No secret changes and no `fly secrets set` needed.

## Next Phase Readiness

- **Ready for plan 02-04:** the exact interface it wires is in place — construct `RunRegistry()` in `lifespan`, store it on `app.state.runs`, `register()`/`deregister()` inside `event_stream`'s generator alongside the existing `record_run`/`release_run` in the `finally`, and `await app.state.runs.drain(timeout=settings.shutdown_drain_seconds)` before `conn.close()`. The `draining` flag is what D-09's 503 checks.
- **Ready for plan 02-05:** `tests/test_lifecycle.py` exists with its module docstring and section separator; DATA-02-a/e integration tests append to it.
- **Ready for Phase 5:** `snapshot()` returns the live `ActiveRun` records DASH-01's feed needs on first connect.
- **Not done here, by design:** `fly.toml`'s `kill_timeout = 30` and the `Dockerfile` CMD's `--timeout-graceful-shutdown 20` (plus the `exec` prefix). Without those two the app-level drain is real but the outer windows do not nest, and Fly's 5 s default would SIGKILL through it. They belong to a later plan in this phase — the drain is not actually effective in production until they land.
- **No blockers.**

## Self-Check: PASSED

- Files verified present and tracked at HEAD: `src/relay/runs.py`, `tests/test_lifecycle.py`, `src/relay/config.py`, `.env.example`, `02-02-SUMMARY.md`
- Commits verified in `git log`: `ecfc6a2`, `6d29f62`, `bdfe1ba`, `88e101f`
- Working tree clean; no unexpected deletions in any task commit
- STATE.md and ROADMAP.md deliberately untouched — the orchestrator owns those writes

---
*Phase: 02-async-safe-data-layer-graceful-shutdown*
*Completed: 2026-08-09*

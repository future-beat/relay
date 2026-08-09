---
phase: 01-security-perimeter
plan: 01
subsystem: infra
tags: [pydantic-settings, limits, rate-limiting, mcp, config]

# Dependency graph
requires: []
provides:
  - "limits>=5.8,<6 as a declared runtime dependency (limits.aio storage + strategies verified importable)"
  - "Settings.api_key / Settings.demo_key — the two API-key tier credentials (D-01)"
  - "Settings.max_daily_cost_usd = 5.0 — global daily spend ceiling (D-03)"
  - "Settings.trust_proxy_header — gate for reading Fly-Client-IP (env RELAY_TRUST_PROXY)"
  - "Six tier/route rate-limit strings: demo/owner x process/create/read, all parseable by limits.parse"
  - "mcp_allow_writes defaults to False — SEC-05 closed"
affects: [01-03-ratelimit, 01-04-auth-wiring, 01-05-demo-key-publication]

# Tech tracking
tech-stack:
  added: ["limits>=5.8,<6"]
  patterns:
    - "Phase-tagged commented Settings groups (# Security perimeter (phase 1))"
    - "Plain typed attributes (not properties) so downstream tests can monkeypatch.setattr(settings, ...)"
    - "Conservative literal defaults for every threshold so a typo'd env var fails safe under extra=ignore"

key-files:
  created: []
  modified:
    - pyproject.toml
    - src/relay/config.py
    - .env.example
    - src/relay/mcp_server.py
    - tests/test_mcp.py

key-decisions:
  - "trust_proxy_header carries an AliasChoices validation alias so the documented RELAY_TRUST_PROXY env var name is actually honored (the attribute-derived RELAY_TRUST_PROXY_HEADER still works)"
  - "README MCP-opt-in wording left to plan 01-05, which explicitly owns that section"
  - "limits keeps an upper bound (<6) unlike every other floor-only dependency, because limits.aio is the API surface plan 03 relies on"

patterns-established:
  - "Security thresholds live on Settings as plain attributes, never hardcoded at the call site"
  - "Documentation locations for a default flip (docstring, .env.example, README) are swept alongside the config change"

requirements-completed: [SEC-05]

# Metrics
duration: 12min
completed: 2026-08-09
---

# Phase 01 Plan 01: Security Config Foundation Summary

**Ten phase-1 security settings on `Settings` (keys, $5/day ceiling, proxy trust, six rate-limit strings), `limits>=5.8,<6` declared as a runtime dependency, and MCP writes flipped to opt-in with tests proving the denial.**

## Performance

- **Duration:** ~12 min
- **Tasks:** 3
- **Files modified:** 5
- **Test suite:** 40 passed (38 baseline + 2 new), ruff clean

## Accomplishments

- `limits>=5.8,<6` is a declared runtime dependency; `limits.aio.storage.MemoryStorage` and `limits.aio.strategies.MovingWindowRateLimiter` verified importable at version 5.8.0
- New `# Security perimeter (phase 1)` group on `Settings` with all ten attributes from the plan's `<interfaces>` contract, at the D-01/D-03/D-04/D-05 values; all six limit strings verified parseable by `limits.parse`
- SEC-05 closed: `mcp_allow_writes` defaults to `False`, with two tests proving a default-configured MCP server denies write tools
- Documentation swept so it matches the new default: module docstring and `.env.example` both corrected

## Task Commits

1. **Task 1: Declare and install the limits dependency** — `f9c49af` (chore)
2. **Task 2: Phase-1 security settings block + MCP write default flip** — `c5118b9` (feat)
3. **Task 3: Prove MCP writes are off by default** — `b23ca38` (test), `24396d8` (docs)

## Files Created/Modified

- `pyproject.toml` — `limits>=5.8,<6` appended to `[project] dependencies` (runtime, not the dev extra)
- `src/relay/config.py` — new phase-1 security settings group; `mcp_allow_writes: bool = True → False`; `AliasChoices` import
- `.env.example` — `RELAY_MCP_ALLOW_WRITES=false` with an opt-in comment, plus dev placeholders for `RELAY_API_KEY`, `RELAY_DEMO_KEY`, `RELAY_MAX_DAILY_COST_USD`, `RELAY_TRUST_PROXY`
- `src/relay/mcp_server.py` — module docstring only; the stale "set `=false` for read-only" instruction replaced with the opt-in wording
- `tests/test_mcp.py` — `test_writes_disabled_by_default`, `test_default_server_is_read_only`; the six existing tests are untouched

## Decisions Made

- **`trust_proxy_header` gets a `validation_alias`.** The plan's `<interfaces>` block documents the env var as `RELAY_TRUST_PROXY`, and Task 2 mandates that exact line in `.env.example` — but `env_prefix="RELAY_"` derives `RELAY_TRUST_PROXY_HEADER` from the attribute name, so the documented variable would have been silently swallowed by `extra="ignore"`. `AliasChoices("RELAY_TRUST_PROXY", "RELAY_TRUST_PROXY_HEADER")` honors both. The plan's own carve-out ("never `Field(...)` unless an alias is needed") sanctions this; it stays a plain instance attribute, so `monkeypatch.setattr(settings, "trust_proxy_header", True)` works for plan 03/04.
- **README left alone.** Pattern 5 lists `README.md:84-85` as a fourth stale location, but plan 01-05 explicitly owns documenting "the MCP write opt-in" in the README. Editing it here would collide with 01-05's rewrite of the same section for no shipped-state benefit (01-05 lands before the phase closes).
- **Inline comment on `RELAY_MCP_ALLOW_WRITES=false` verified safe.** Confirmed `.env.example` still parses correctly as a dotenv file end to end (`Settings(_env_file=<copy of .env.example>)` yields `mcp_allow_writes=False`, `trust_proxy_header=False`, both keys populated) — a `cp .env.example .env` bootstraps a working local dev setup under fail-closed auth.

## Deviations from Plan

None requiring a deviation rule. The `trust_proxy_header` alias (above) resolves an internal inconsistency between the plan's `<interfaces>` env-var name and its "no `Field(...)`" guidance, using the plan's own stated exception.

## Issues Encountered

- **TDD ordering on Task 3 is degenerate by construction.** SEC-05's entire functional change is the `config.py` default, which the plan sequenced into Task 2 — so both new tests pass the moment they are written, and a literal RED gate is impossible without reverting Task 2. Proved the tests are non-vacuous instead: with `RELAY_MCP_ALLOW_WRITES=true` both fail (`test_default_server_is_read_only` fails with the write tool actually executing and hitting a FOREIGN KEY error, confirming it asserts on the denial path and not on an incidental exception), while the six pre-existing tests still pass. Task 3 therefore has a `test(...)` commit followed by a `docs(...)` commit rather than `test` → `feat`.
- **`limits` is not installed in the main checkout's venv.** Verified importability in a throwaway `.wt-venv/` inside the worktree (self-ignored by the `.gitignore` Python's `venv` writes, so nothing was committed). **The orchestrator must run `.venv/bin/python -m pip install -e ".[dev]"` in the main checkout after merge** — plan 01-03 imports `limits` and will fail without it.

## Verification

- `PYTHONPATH=src .venv/bin/python -m pytest -q` → 40 passed, 0 failed
- `.venv/bin/ruff check src tests` → clean
- `Settings(_env_file=None)` → `mcp_allow_writes False`, `max_daily_cost_usd 5.0`, `trust_proxy_header False`, all six limit strings at their D-04/D-05 values, `api_key`/`demo_key` `None`
- `limits.parse` accepts all six limit strings (5/hour, 60/hour, 20/hour, 120/hour, 120/hour, 600/hour)
- `git diff --stat` against the base touches exactly the five planned files
- `git diff src/relay/mcp_server.py` confined to the module docstring; `call_mcp_tool`'s two-value unpack of `_execute_guarded` untouched (plan 01-02 depends on that arity)

## Threat Model Coverage

| Threat ID | Status |
|-----------|--------|
| T-01-01 (EoP: MCP write tier) | Mitigated — `mcp_allow_writes=False`, proven by `test_default_server_is_read_only` |
| T-01-02 (Stale docs inverting the default) | Mitigated in this plan's scope (docstring + `.env.example`); README handoff to plan 01-05 |
| T-01-03 (Key material disclosure) | Mitigated — keys are plain optional attributes; nothing logs `Settings` |
| T-01-04 (DoS via permissive fallbacks) | Mitigated — every threshold has an explicit conservative literal default |
| T-01-SC (Supply chain: `limits`) | Mitigated — RESEARCH.md legitimacy audit `[OK]`, disposition Approved; installed version 5.8.0 matches the audited package |

No new threat surface beyond the register.

## Known Stubs

None.

## Next Phase Readiness

- Plans 01-03 (`ratelimit.py`) and 01-04 (`auth.py`) can now read every threshold from `Settings` — the interface contract is live at the exact names and defaults specified.
- **Blocker for plan 01-03:** `limits` must be installed into the main venv after this merge (see Issues Encountered).
- Plan 01-05 inherits the README MCP-opt-in correction (`README.md:84-85` still says set `=false` for read-only).

---
*Phase: 01-security-perimeter*
*Completed: 2026-08-09*

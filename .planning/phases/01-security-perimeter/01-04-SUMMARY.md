---
phase: 01-security-perimeter
plan: 04
subsystem: security-perimeter
tags: [auth, rate-limiting, budget, routes, sse, fly]
requires:
  - "relay.auth.require_tier — from plan 01-03"
  - "relay.ratelimit.enforce / enforce_daily_budget / reserve_run / release_run — from plan 01-03"
  - "tests/conftest.py shared authed client fixture + autouse limiter reset — from plan 01-03"
provides:
  - "relay.main._gate — per-bucket perimeter dependency composing auth -> daily ceiling -> per-IP window"
  - "relay.main.create_gate / read_gate / process_gate — the three wired gates"
  - "in-flight cost reservation seam bracketing the SSE stream in process_ticket"
  - "RELAY_TRUST_PROXY = 'true' in fly.toml"
affects:
  - "plan 01-05 documents the published demo key, the deploy secrets step and the README threat-model paragraph"
tech-stack:
  patterns:
    - "controls as FastAPI route dependencies, never middleware (SSE locks its status line at 200)"
    - "global condition (budget) checked before per-caller condition (window) so an outage does not burn allowance"
    - "reservation released in a plain try/finally inside the generator, never an async context manager"
    - "gate dependency built from a module-level Depends singleton (ruff B008)"
key-files:
  created: []
  modified:
    - src/relay/main.py
    - fly.toml
    - tests/test_auth.py
    - tests/test_ratelimit.py
decisions:
  - "One gate factory parameterised by bucket rather than three hand-written dependencies — the plan allowed either; the factory keeps the ordering defined in exactly one place"
  - "Integration tests drive the process gate with an unknown ticket id: the gate consumes its unit before the handler's lookup, so the perimeter is exercised end-to-end with no agent loop and no Anthropic stub"
  - "Docker smoke replaced with an equivalent in-process unconfigured-deployment check (docker unavailable in the execution sandbox), plus a live uvicorn curl pass"
metrics:
  duration: ~20 min
  tasks: 3
  commits: 3
  tests_before: 78
  tests_after: 94
  completed: 2026-08-09
---

# Phase 1 Plan 4: Perimeter Wiring Summary

Composed auth, the daily USD ceiling and the per-IP moving window into one ordered route dependency, applied it to the three mutating/paid routes while leaving `/`, `/health`, `/metrics` and `/dashboard` open, and proved the whole perimeter at the HTTP boundary — including through a real uvicorn process.

## What Was Built

### Task 1 — `src/relay/main.py` gate composition + `fly.toml`

`_gate(bucket, *, meter_spend=False)` returns an async dependency that resolves `require_tier("owner", "demo")` as a sub-dependency, then (on the process route only) `enforce_daily_budget(app.state.conn)`, then `await enforce(bucket, tier, request)`. Three instances are built at module level: `create_gate`, `read_gate`, `process_gate`.

Ordering is deliberate and lives in one place: FastAPI resolves sub-dependencies before the dependant, so auth is always first; the budget is a *global* condition, so checking it before the per-IP window means a budget outage does not also consume the caller's allowance. `test_rate_limit_and_budget_ordering` pins both halves of that claim.

Placement is the whole point of the plan. Every control is a route dependency — `grep -rc 'app.middleware\|BaseHTTPMiddleware' src/relay/` is 0 across all fourteen modules. A `StreamingResponse` locks its status line at 200 the moment the generator yields, so a rejection raised any later could only ever appear as an in-stream `event: error` on an otherwise successful response.

Route map as wired (D-07):

| Route | Gate | Bucket |
|-------|------|--------|
| `GET /`, `GET /health`, `GET /metrics`, `GET /dashboard` | none | — |
| `POST /tickets` | auth → window | `create` |
| `GET /tickets/{id}` | auth → window | `read` |
| `POST /tickets/{id}/process` | auth → daily ceiling → window | `process` |

The in-flight reservation calls `reserve_run()` in the handler once the gate has admitted the request, and `release_run()` in a `finally` **inside** `event_stream`, placed after `record_run` so the durable row and the reservation are never both missing in the same instant. Not an `async with`: `run_ticket` suspends at every yield, and anything held across a yield leaks into whatever coroutine runs in between and corrupts the OTel span tree (`agent.py` documents this at its run-span setup). `grep -c 'async with' src/relay/main.py` is 0.

`fly.toml` gains `RELAY_TRUST_PROXY = 'true'` in `[env]`, so `Fly-Client-IP` is authoritative behind the Fly proxy and nowhere else — off-proxy the header is fully client-controlled and trusting it would be a rate-limit bypass.

- Commit: `a1bb746`

### Task 2 — `tests/test_auth.py` route section

Eight integration tests under `# --- routes ---`. A `without_key` context manager pops the fixture's default `X-API-Key` for one block and restores it, so unauthenticated cases genuinely send no header rather than an empty one.

The 401 assertions check the `WWW-Authenticate: APIKey` header value, not just the status — SEC-01 names the challenge, and a 401 without it is the exact RFC violation the requirement exists to prevent. `test_auth_not_configured_fails_closed` asserts 503 on a protected route **and** 200 on `/health` in the same test: the pairing is the point, since a fail-closed that also takes health down kills the container `HEALTHCHECK` and the CI smoke job.

`tests/test_api.py` was not touched; `test_get_missing_ticket_404` still asserts 404 under the authed fixture, and the new `test_unauthenticated_get_ticket_401` covers the complementary ordering (dependency before handler, so 401 precedes 404).

- Commit: `6757494`

### Task 3 — `tests/test_ratelimit.py` route section

Eight integration tests. The process-gate tests post to `/tickets/9999/process`: the gate consumes its unit before the handler's ticket lookup, so the full perimeter runs and the request then stops at 404 — no agent loop, no Anthropic call, no stub to maintain, and no flaky concurrency harness.

`test_demo_process_limit_429` exercises the real D-04 value (5 allowed, 6th refused) and asserts all four headers plus the D-08 body. `test_demo_create_limit_429` patches the limit down to `2/hour` instead of issuing 21 requests, which doubles as the standing proof that plan 03's lazy item construction has not regressed; `test_demo_tier_defaults_match_d04` asserts the shipped defaults separately.

`test_budget_survives_restart` opens a second `TestClient` over the fixture's `tmp_path`-backed database file — the ceiling has to come from durable state, not process memory. `test_in_flight_reservation` drives `reserve_run()` directly until the reservation alone crosses the ceiling and asserts `SELECT COUNT(*) FROM runs` is still 0 when the 503 fires.

- Commit: `dd825c3`

## Verification

| Check | Result |
|-------|--------|
| `pytest -q` | 94 passed (baseline 78) |
| `ruff check src tests` | clean |
| Route gate map (`len(r.dependencies)+len(r.dependant.dependencies)`) | `/`, `/health`, `/metrics`, `/dashboard` = 0; the three protected routes = 2 each |
| `grep -rc 'app.middleware\|BaseHTTPMiddleware' src/relay/` | 0 in all 14 modules |
| `grep -c 'async with' src/relay/main.py` | 0 |
| Call sites: `enforce_daily_budget(` / `reserve_run()` / `release_run()` | 1 each |
| `grep -c "RELAY_TRUST_PROXY = 'true'" fly.toml` | 1 |
| Mutation: gate removed from `POST /tickets` | 3 tests in `test_auth.py` fail (criterion: ≥3) |
| Mutation: `enforce_daily_budget` neutered | `test_daily_budget_503`, `test_budget_survives_restart`, `test_in_flight_reservation` all fail (plus 3 more) |
| Order independence (5 files forward and reversed) | 67 passed both ways |
| Live uvicorn: `/health`, `/metrics`, `/dashboard`, `/` unauthenticated | 200, 200, 200, 307 |
| Live uvicorn: `POST /tickets` no key | `401` with `www-authenticate: APIKey` on the wire |
| Live uvicorn: 6th demo `POST /process` | `429` with `retry-after`, `x-ratelimit-limit: 5`, `x-ratelimit-remaining: 0`, `x-ratelimit-reset` and the D-08 body |
| Unconfigured deployment (no keys set) | `/health` 200, `POST /tickets` 503 |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] ruff B008 rejected `Depends(require_tier(...))` in an argument default**
- **Found during:** Task 1
- **Issue:** Writing the gate exactly as the plan sketched it (`tier: Tier = Depends(require_tier("owner", "demo"))` in the inner dependency signature) fails `ruff check`, which is a CI gate. B008 fires twice — once for `Depends`, once for `require_tier`.
- **Fix:** Hoisted the dependency to a module-level singleton `_ANY_TIER = Depends(require_tier("owner", "demo"))`, which is exactly the pattern `auth.py` already uses for `_HEADER`. Behaviour is identical; D-07 permits both tiers on all three surfaces, so one shared instance is correct.
- **Files modified:** `src/relay/main.py`
- **Commit:** `a1bb746`

### Adjustments

**2. Docker smoke replaced with an equivalent in-process check**
- **Found during:** Task 1
- **Issue:** `docker info` fails in the execution sandbox, so the container acceptance criterion could not be run as written.
- **Fix:** Ran the invariant the criterion protects instead — app booted with `api_key`/`demo_key` unset, asserting `/health` 200 and `POST /tickets` 503 — and additionally ran a real uvicorn process with curl against all seven routes. `Dockerfile` and the CI smoke job are unmodified by this plan, and `/health` carries zero dependencies (asserted programmatically), so the `HEALTHCHECK` path is unchanged. The docker build itself should be re-confirmed in CI.
- **Files modified:** none

**3. `grep -c` acceptance counts include the import line**
- **Found during:** Task 1
- **Issue:** The criteria expect `grep -c 'enforce_daily_budget' src/relay/main.py` to be 1, but the `from .ratelimit import ...` line also matches, so the bare-name count is 2 for each imported symbol. Same class of prose/import pollution recorded as deviation 1 in plan 03.
- **Fix:** Verified the structural intent — exactly one call site each — with `grep -c 'enforce_daily_budget('`, `grep -c 'reserve_run()'`, `grep -c 'release_run()'`, all returning 1. `require_tier` is 2 (import + singleton), satisfying its "at least 1".
- **Files modified:** none

**4. Reworded a comment that polluted the `:memory:` grep**
- **Found during:** Task 3
- **Issue:** A comment in `test_budget_survives_restart` explaining *why* the test avoids an in-memory database contained the literal token, so `grep -c ':memory:'` matched the very test the criterion requires to be free of it.
- **Fix:** Reworded to "a real tmp_path file rather than an in-memory database". Count is now 0.
- **Files modified:** `tests/test_ratelimit.py`

No Rule 4 architectural decisions arose. No authentication gates were hit.

## Threat Model Coverage

| Threat ID | Status | Where |
|-----------|--------|-------|
| T-01-20 (unauthenticated protected routes) | mitigated | `_ANY_TIER` resolved as a sub-dependency of all three gates; `test_missing_key_returns_401_with_challenge`, `test_unauthenticated_get_ticket_401`, `test_process_requires_key` |
| T-01-21 (controls placed where they cannot take effect) | mitigated | Route dependencies only; middleware grep 0, `async with` grep 0, gate map asserted programmatically |
| T-01-22 (global auth killing `/health`) | mitigated | Allowlist of three protected routes, not a denylist; `test_public_routes_need_no_key`, `test_auth_not_configured_fails_closed`, live curl pass |
| T-01-23 (cost amplification via process) | mitigated | `process_gate` runs the ceiling then the window, both before any model call; `test_daily_budget_503`, `test_demo_process_limit_429` |
| T-01-24 (`Fly-Client-IP` trusted off-proxy) | mitigated | `RELAY_TRUST_PROXY = 'true'` present only in `fly.toml [env]`; `settings.trust_proxy_header` still defaults false |
| T-01-25 (reservation leak) | mitigated | `release_run()` in a `finally` inside `event_stream` after `record_run` — runs on completion, error and generator close |
| T-01-26 (demo holders reading ticket bodies) | accepted | Unchanged from the plan: fictional seed data, `read` bucket blunts scraping, documented in plan 05's README paragraph |
| T-01-27 (deploy without `RELAY_API_KEY`) | mitigated | Fail-closed 503 verified against a live unconfigured app; the ops step is plan 05's README deploy note |

### Residual note (not a new threat, carried to plan 05)

`reserve_run()` fires in the handler, and its release lives in the generator's `finally`. If a client disconnects between the handler returning and Starlette starting to iterate the body, the generator is collected without ever running, and one `max_run_cost_usd` reservation leaks until the process restarts. The window is sub-millisecond and in-process only, the daily ceiling still fails safe (too strict, never too loose), and any alternative placement would have to hold state across the agent loop's yields — which is the thing the plan explicitly forbids. Worth a sentence in the README's limits section.

## Known Stubs

None. Every route in the D-07 table is wired to its final gate, and every behaviour listed in the plan's `<behavior>` blocks has a passing test.

## Self-Check: PASSED

All four modified files exist on disk (`src/relay/main.py`, `fly.toml`, `tests/test_auth.py`, `tests/test_ratelimit.py`) and all three commit hashes (`a1bb746`, `6757494`, `dd825c3`) are present in the branch history.

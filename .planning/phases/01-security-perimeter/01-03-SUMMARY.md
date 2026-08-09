---
phase: 01-security-perimeter
plan: 03
subsystem: security-perimeter
tags: [auth, rate-limiting, budget, test-infrastructure]
requires:
  - "relay.config.settings (api_key, demo_key, max_daily_cost_usd, trust_proxy_header, the six limit strings, max_run_cost_usd) — from plan 01-01"
  - "runs table (cost_usd, created_at) — pre-existing schema in relay.db"
provides:
  - "relay.auth.resolve_tier / relay.auth.require_tier — tier resolution and route-dependency gating"
  - "relay.ratelimit.enforce / client_ip / spent_today / enforce_daily_budget / next_utc_midnight / reserve_run / release_run / reset_limits"
  - "tests/conftest.py shared authed client fixture + autouse limiter reset"
affects:
  - "plan 01-04 wires these dependencies onto POST /tickets, POST /tickets/{id}/process, GET /tickets/{id}"
tech-stack:
  added:
    - "limits 5.8.0 — MovingWindowRateLimiter over MemoryStorage (aio API)"
  patterns:
    - "auth/limits as FastAPI route dependencies, never middleware (SSE status-line safety)"
    - "lazy settings reads at call time so monkeypatch works in tests"
    - "constant-time byte comparison via secrets.compare_digest, both tiers evaluated unconditionally"
key-files:
  created:
    - src/relay/auth.py
    - src/relay/ratelimit.py
    - tests/test_auth.py
    - tests/test_ratelimit.py
  modified:
    - tests/conftest.py
    - tests/test_api.py
    - tests/test_observability.py
decisions:
  - "Fail closed (503) when neither RELAY_API_KEY nor RELAY_DEMO_KEY is configured, raised per request rather than at startup"
  - "Rate-limit items are parsed lazily and memoized, with the cache cleared by reset_limits(), so tests can monkeypatch thresholds"
  - "429/503 bodies use a dict HTTPException detail — a deliberate divergence from the codebase's short-string convention, required by D-08"
  - "Test suite authenticates as the owner tier so loose limits keep the suite from fighting the limiter"
metrics:
  duration: ~25 min
  tasks: 3
  commits: 5
  tests_before: 44
  tests_after: 78
  completed: 2026-08-09
---

# Phase 1 Plan 3: Auth and Rate-Limit Modules Summary

Built `auth.py` (constant-time API-key tier resolution + fail-closed gating dependency) and `ratelimit.py` (per-tier moving window, proxy-aware client IP, UTC-day spend ceiling with in-flight reservation) as pure units, and consolidated the test harness so limiter state can never leak across the pytest session.

## What Was Built

### Task 1 — `src/relay/auth.py` (TDD)

`resolve_tier(presented)` is pure and non-raising. It encodes the presented value to bytes before comparison, because Starlette decodes headers as latin-1 and `secrets.compare_digest` raises `TypeError` on `str` arguments containing a codepoint above 127 — which would turn a malformed key into an unhandled 500 on the auth path. Both `is_owner` and `is_demo` are computed unconditionally and the branch happens on the resulting booleans, so timing does not reveal which configured key was closer.

`require_tier(*allowed)` returns a FastAPI dependency in a fixed order: 503 if no key is configured at all, then 401 with `WWW-Authenticate: APIKey` for an unresolvable key, then 403 for a valid key on a surface it is not permitted to use, then the tier. The 503 is raised per request, never at import or startup, so `/health` stays public and the container still boots without secrets.

Settings are read inside the functions, never captured at import, so `monkeypatch.setattr(settings, ...)` takes effect. Rejection logs carry only `outcome` and (where known) `tier`; no key material reaches `ctx`, which `JsonFormatter` flattens straight to stdout.

- Commits: `b3df1e5` (RED, 12 failing tests), `f26137c` (GREEN)

### Task 2 — `src/relay/ratelimit.py` (TDD)

One `MovingWindowRateLimiter` over a module-level `MemoryStorage`, with a `RateLimitItem` per `(bucket, tier)` hit with identifiers `(bucket, tier, ip)`. Items are parsed **lazily** into a memoized dict rather than at import — an import-time `parse(settings.demo_process_limit)` would freeze the value before any test could monkeypatch it. `reset_limits()` clears that cache along with the storage.

`client_ip` reads `fly-client-ip` only when `settings.trust_proxy_header` is true, falling back to `request.client.host` and then the literal `"unknown"`. Unconditional trust would be a rate-limit bypass: off the Fly proxy the header is fully client-controlled.

The daily ceiling sums `runs.cost_usd` where `created_at >= datetime('now', 'start of day')` — both sides are UTC by construction, so there is no timezone seam. `reserve_run()`/`release_run()` add and remove `settings.max_run_cost_usd` from a module-level float, closing the window in which concurrent `/process` requests all read the same stale SUM (because `record_run` only fires once the SSE generator finishes). These are plain calls, not a context manager, because nothing may be held across the agent loop's yields.

429 and 503 both carry the D-08 friendly dict detail naming the limit hit, its reset time, and the cost-control rationale; 429 additionally carries `Retry-After` and the three `X-RateLimit-*` headers.

- Commits: `653134e` (RED, 22 failing tests), `30bc2d9` (GREEN)

**Research assumption A7 resolved:** the first test in `test_ratelimit.py` exhausts a bucket, calls `reset_limits()`, and asserts the next `enforce` is allowed. `MemoryStorage.reset()` does clear buckets outright rather than only pruning expired entries — the rest of the suite's order-independence rests on this.

### Task 3 — Test fixture consolidation

The `client` fixture, duplicated verbatim in `test_api.py` and `test_observability.py`, now lives once in `tests/conftest.py`. It additionally patches `settings.api_key`/`settings.demo_key` and puts the owner key on `TestClient`'s default headers, which is what keeps every existing test body byte-identical — the diffs for both files are pure deletions. Owner tier is deliberate: owner limits are loose, so the suite never fights the limiter, while demo-tier limit behaviour is exercised directly in `test_ratelimit.py`.

An autouse async `_reset_limits` fixture awaits `relay.ratelimit.reset_limits()` before every test. Verified via `--setup-show` that it runs for synchronous tests too under `asyncio_mode = "auto"`, and that no un-awaited-coroutine `RuntimeWarning` is emitted (`pytest -W error::RuntimeWarning` is green).

- Commit: `8430ce8`

## Verification

| Check | Result |
|-------|--------|
| `pytest -q` | 78 passed (baseline 44) |
| `ruff check src tests` | clean |
| Order independence: `test_ratelimit.py test_observability.py` and reversed | both green |
| Order independence: `test_api.py test_observability.py` and reversed | both green |
| `grep -rc 'BaseHTTPMiddleware\|app.middleware' src/relay/` | 0 in every module |
| `grep -c compare_digest src/relay/auth.py` | 2 |
| `grep -c 'elif.*compare_digest' src/relay/auth.py` | 0 |
| `grep -c 'start of day' src/relay/ratelimit.py` | 1 |
| `grep -c 'Retry-After' src/relay/ratelimit.py` | 2 (429 + 503) |
| `grep -c 'async with' src/relay/ratelimit.py` | 0 |
| Key material in log `ctx` | none — no `ctx` line references `presented`, `api_key`, or `demo_key` |
| `git diff tests/test_api.py` additions | 0 (deletions only) |

## Deviations from Plan

### Adjustments

**1. Comment wording adjusted to satisfy grep-based acceptance criteria**
- **Found during:** Task 1
- **Issue:** Explanatory comments that used the literal tokens `compare_digest` and `auto_error=False` inflated `grep -c` counts past the plan's expected values (3 vs 2, and 2 vs 1). The criteria are structural assertions about the *code*, and prose was polluting them.
- **Fix:** Reworded both comments to convey the same rationale without repeating the token ("the constant-time check", "Errors are raised here, not by the extractor").
- **Files modified:** `src/relay/auth.py`
- **Commit:** `f26137c`

**2. Task 2's autouse reset fixture written locally, then moved in Task 3**
- **Found during:** Task 2
- **Issue:** `test_ratelimit.py` needs per-test limiter isolation, but the plan places the autouse fixture in `conftest.py` during Task 3. Task 2's tests could not be order-independent without it.
- **Fix:** Added the fixture locally in `test_ratelimit.py` for Task 2, then moved it to `conftest.py` and deleted the local copy in Task 3 — consistent with Task 3's stated consolidation intent and with its `grep -c 'autouse=True' tests/conftest.py` returning exactly 1.
- **Files modified:** `tests/test_ratelimit.py`, `tests/conftest.py`
- **Commit:** `8430ce8`

No Rule 4 architectural decisions arose. No authentication gates were hit.

## Threat Model Coverage

| Threat ID | Status | Where |
|-----------|--------|-------|
| T-01-11 (timing leak in tier comparison) | mitigated | `auth.resolve_tier` — bytes `compare_digest`, both tiers unconditional; grep-asserted no `elif` |
| T-01-12 (non-ASCII key → 500) | mitigated | `.encode()` before comparison; `test_non_ascii_key_is_rejected_cleanly`, `test_non_ascii_key_is_401_not_500` |
| T-01-13 (fail-open on unset key) | mitigated | `require_tier` 503 branch; `test_unconfigured_deployment_fails_closed_with_503` |
| T-01-14 (forged `Fly-Client-IP`) | mitigated | `client_ip` gated on `trust_proxy_header`; `test_proxy_header_ignored_when_untrusted` |
| T-01-15 (cost amplification) | mitigated | `enforce` moving window + `enforce_daily_budget` |
| T-01-16 (in-flight runs invisible to ceiling) | mitigated | `reserve_run`/`release_run`; `test_reservations_alone_can_exhaust_the_daily_budget` |
| T-01-17 (IP rotation / cold-start bucket reset) | accepted | Documented in the module docstring as the reason the persistent daily ceiling exists separately |
| T-01-18 (credential leakage via logs) | mitigated | Only `outcome`/`tier`/`ip` in `ctx`; grep-asserted |
| T-01-19 (SQLi in the daily-spend query) | mitigated | `DAILY_SPEND_SQL` is a static constant with zero parameters and zero user input |

No new threat surface was introduced beyond the register — no route is wired in this plan.

## Known Stubs

None. Every export listed in the plan's `<interfaces>` is fully implemented and unit-tested. No route is gated yet, which is by design: plan 01-04 composes these dependencies onto the three protected routes.

## Notes for Plan 04

- The `client` fixture's `X-API-Key` header is currently inert; it becomes load-bearing the moment the gate is wired.
- `tests/test_api.py::test_get_missing_ticket_404` still asserts 404 and will keep passing because the fixture authenticates. The separate unauthenticated-401 test belongs to plan 04 (research Pitfall 2).
- `reserve_run()`/`release_run()` must be called from the handler's `try/finally` in `main.py`, never wrapped around the agent loop with `async with`.

## Self-Check: PASSED

All six claimed files exist on disk; all six claimed commit hashes (`b3df1e5`, `f26137c`, `653134e`, `30bc2d9`, `8430ce8`, `5d4ad15`) are present in the branch history.

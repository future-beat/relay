---
phase: 01-security-perimeter
verified: 2026-08-09T13:44:54Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
deferred:
  - truth: "WR-01 — no TOCTOU lock between budget check and reservation"
    addressed_in: "Gap-closure phase (per 01-DEFERRED.md, user decision 2026-08-09)"
    evidence: "01-DEFERRED.md: 'bounded overshoot of roughly one run's cost on a $5/day demo budget... recommend accepting permanently.' Does not break SEC-03 (ceiling still holds within ~$0.50 tolerance, verified enforced and cold-start-durable)."
  - truth: "WR-02 — exhausted budget short-circuits the rate limiter on /process"
    addressed_in: "Gap-closure phase (per 01-DEFERRED.md, user decision 2026-08-09)"
    evidence: "01-DEFERRED.md: partially mitigated by CR-03 — the anon auth bucket (60/min) now bounds the outage-path request rate even though the process-tier limiter is bypassed. Locked in by test_rate_limit_and_budget_ordering."
  - truth: "WR-04 — bound_ticket_id defaults to None (fail-open) on the MCP call path"
    addressed_in: "Gap-closure phase (per 01-DEFERRED.md, user decision 2026-08-09)"
    evidence: "MCP tool calls are not part of a bound 'run' the way HTTP /process runs are — every HTTP path that matters for SEC-04 passes bound_ticket_id=ticket['id'] explicitly (agent.py:196) and this is verified with a real cross-ticket-write test. The fail-open default only affects the MCP surface, which has no persistent per-ticket run concept to bind against."
  - truth: "WR-06 — README's hardcoded demo key literal has no automated pairing check against fly secrets"
    addressed_in: "Gap-closure phase (per 01-DEFERRED.md, user decision 2026-08-09)"
    evidence: "SEC-06 requires a published, working demo key with tiered limits — verified via test_demo_key_is_accepted, test_dashboard_demo_key_is_sourced_not_hardcoded, and demo.sh's CR-03 fix (bbc40a8) that fails loudly rather than silently mis-authenticating. The literal-pairing gap is a deploy-time documentation risk, not a code defect that breaks SEC-06 as implemented."
---

# Phase 1: Security Perimeter Verification Report

**Phase Goal:** The live demo can no longer be abused into unbounded Claude spend or cross-ticket writes
**Verified:** 2026-08-09T13:44:54Z
**Status:** passed
**Re-verification:** No — initial verification of this VERIFICATION.md, but phase underwent a code-review-and-fix cycle first (01-REVIEW.md → 3 Criticals fixed + mutation-checked, 2 Warnings fixed, 4 Warnings + 8 Infos deliberately deferred per 01-DEFERRED.md). This report verifies the FINAL state after that cycle, independently, against the code — not against SUMMARY or REVIEW claims.

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria, verified against code)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `POST /tickets` / `POST /tickets/{id}/process` without valid `X-API-Key` → 401 with `WWW-Authenticate`; valid key succeeds; dashboard/metrics/health stay public with no credentials | ✓ VERIFIED | `auth.py:69` raises `HTTPException(401, ..., headers=_UNAUTHENTICATED)`; `main.py` gates only `create_gate`/`read_gate`/`process_gate` on the three mutating/costly routes; `/`, `/health`, `/metrics`, `/dashboard` have no `Depends(...)`. Tests: `test_missing_key_returns_401_with_challenge`, `test_valid_key_allows`, `test_public_routes_need_no_key` all pass against a real `TestClient` hitting the real app (not mocked). |
| 2 | Exceeding the per-key/IP limit → 429 with `Retry-After` + rate-limit headers; demo key limited more tightly than owner key | ✓ VERIFIED | `ratelimit.py:120-139` sets `Retry-After`, `X-RateLimit-Limit/Remaining/Reset` on every 429. `config.py`: `demo_process_limit="5/hour"` vs `owner_process_limit="60/hour"` (D-04/D-05). Tests: `test_rejection_carries_retry_after_and_ratelimit_headers`, `test_demo_tier_defaults_match_d04`, `test_owner_tier_looser`, `test_demo_process_limit_429`. |
| 3 | Daily USD ceiling (from `runs.cost_usd`) → 503 with reset message once reached; stays enforced across a cold start | ✓ VERIFIED | `enforce_daily_budget` reads `SUM(cost_usd) FROM runs WHERE created_at >= start of day` (SQL, not memory) plus in-flight `reserved_usd()`. CR-01 fix moved `record_run()` into the SSE generator's `finally` (`main.py:154-180`) so a mid-stream disconnect still writes its partial cost — regression-tested by `test_mid_stream_disconnect_still_records_the_spend`, which asserts a real row lands and `spent_today()` reflects it. CR-02 fix made reservations token-identified and self-expiring (`ratelimit.py:176-194`, TTL 300s) so an un-started generator's leaked claim cannot permanently pin the ceiling — regression-tested by `test_a_stream_that_never_starts_leaks_only_until_the_ttl` and `test_unreleased_reservation_expires_after_its_ttl`. Cold-start durability is directly tested: `test_budget_survives_restart` exhausts the budget, tears down and re-creates a fresh `TestClient`/app lifespan against the same on-disk SQLite file, and asserts the 503 still fires. |
| 4 | A ticket body instructing the agent to act on a different ticket produces a visible denial event in the run stream (model retries in-run, no crash) and the write lands on the correct ticket | ✓ VERIFIED | `agent.py:66-83` compares `validated.get("ticket_id")` against `bound_ticket_id` (bound per-call from `run_ticket`'s own `ticket["id"]`, never stored on the shared registry — `agent.py:192-197`); on mismatch returns a `denied_by: ticket_binding` error and the run continues (no `return`/crash). `agent.py:210-226` emits a `guardrail` SSE event with `guard: ticket_binding`, `expected/supplied_ticket_id`, `action: denied`, ordered before the `tool_result` event. Regression tests seed *real* ticket rows (`_seed_tickets`) so an unguarded write would actually land: `test_mismatched_ticket_id_is_denied` asserts the victim ticket gets 0 replies; `test_run_recovers_after_binding_denial` drives a denial then a corrected retry and asserts the reply lands only on the correct ticket (`_reply_ticket_ids(conn) == [TICKET["id"]]`) with a `resolution` event, proving the run neither crashes nor terminates on denial; `test_concurrent_runs_do_not_cross_bind` runs two concurrent tickets against one shared registry and asserts zero guardrail events and correct per-ticket writes, closing the registry-binding-race pitfall called out in research. |
| 5 | A freshly started MCP server refuses write tools unless `RELAY_MCP_ALLOW_WRITES=true` is explicitly set | ✓ VERIFIED | `config.py:19` `mcp_allow_writes: bool = False`; `mcp_server.py:128` `ToolPolicy(allow_writes=settings.mcp_allow_writes)`. Tests: `test_writes_disabled_by_default` asserts a fresh `Settings(_env_file=None)` has `mcp_allow_writes is False`; `test_write_tool_denied_when_read_only` drives an actual denied write call through `call_mcp_tool`; `test_default_server_is_read_only` exercises the real default-constructed policy. |
| 6 (SEC-06, additional) | A published, tightly-limited demo key lets visitors run the agent; tiers distinguish it from the owner key | ✓ VERIFIED | Demo key surfaced on `/dashboard` from `settings.demo_key` (not a literal — `main.py:241`, tested by `test_dashboard_demo_key_is_sourced_not_hardcoded`); `resolve_tier` distinguishes owner/demo via two independent `compare_digest` calls (`auth.py:42-51`); demo tier capped at 5/hr process vs owner's 60/hr. `demo.sh` (post `bbc40a8` fix) fails loudly if `RELAY_DEMO_KEY` is unset rather than silently sending a stale placeholder. |

**Score:** 6/6 truths verified (5 ROADMAP success criteria + SEC-06 demo-key truth, all with passing, non-tautological regression tests)

### Deferred Items

Four Warning-level findings and 8 Info findings from `01-REVIEW.md` were explicitly deferred by user decision on 2026-08-09 (recorded in `01-DEFERRED.md`), destined for a gap-closure phase (`/gsd:plan-phase 1 --gaps`). Independently assessed below — none of them falsifies a SEC-01..SEC-06 requirement or a ROADMAP success criterion as currently scoped:

| # | Item | Addressed In | Evidence |
|---|------|---------------|----------|
| 1 | WR-01 (TOCTOU between budget check and reservation) | Gap-closure phase | Bounded overshoot of ~1 run's cost (~$0.50) on a $5/day ceiling; does not defeat SEC-03's "stays enforced" claim, which was verified for the single-request and cold-start cases this review directly tested. |
| 2 | WR-02 (exhausted budget bypasses the per-IP limiter on `/process`) | Gap-closure phase | Partially mitigated by the CR-03 fix: the anon auth bucket (60/min) now caps outage-path request volume even though the process-tier bucket is skipped. `test_rate_limit_and_budget_ordering` locks in the current ordering by test. |
| 3 | WR-04 (`bound_ticket_id` fail-open default, live on the MCP path) | Gap-closure phase | Confirmed still present (`agent.py:51`, `mcp_server.py:120`). Does not affect SEC-04 as verified: every HTTP `/process` run (the only path the ROADMAP success criterion and the phase's threat model describe) explicitly passes `bound_ticket_id=ticket["id"]`. MCP tool calls have no persistent "current ticket" concept to bind against, so this is a hardening gap on an adjacent surface, not a failure of the verified truth. |
| 4 | WR-06 (README demo-key literal has no automated pairing check against `fly secrets`) | Gap-closure phase | SEC-06 as implemented (published, working, tiered demo key) is verified by test and by the `demo.sh` loud-failure fix; the unchecked literal is a deploy-time documentation risk explicitly called out as "known-unverified, deploy-time" in `01-DEFERRED.md`, not a code defect. |
| 5 | 8 Info findings (IN-01..IN-08) | Gap-closure phase | Cosmetic/hardening (unbounded `_storage.events` dict, `/metrics` unbounded row scan, brittle test assertion, etc.) — none maps to a SEC-01..SEC-06 requirement. |

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/relay/auth.py` | Constant-time tier resolution, 401/403/503 semantics | ✓ VERIFIED | Both `compare_digest` calls unconditional (no early-exit leak); fail-closed 503 when no keys configured, raised per-request not at import (`/health` stays public — confirmed by test). |
| `src/relay/ratelimit.py` | Moving-window per-IP/tier limiter + persistent daily spend ceiling + self-expiring reservations | ✓ VERIFIED | `limits.aio` `MovingWindowRateLimiter`/`MemoryStorage`; `DAILY_SPEND_SQL` reads `runs` table; token-identified TTL'd reservations (post-CR-02). |
| `src/relay/agent.py` (`_execute_guarded`, `run_ticket`) | Server-side ticket_id binding, `guardrail` event emission | ✓ VERIFIED | Binding compares validated int against per-call `bound_ticket_id`; denial does not `return`/terminate the loop; `guardrail` event precedes `tool_result` in the stream. |
| `src/relay/main.py` (`_gate`, route wiring) | Auth + rate-limit + budget composed as route dependencies (not middleware), CR-01/CR-02/CR-03 fixes applied | ✓ VERIFIED | `record_run()` moved into generator `finally` (CR-01); reservation `token` threaded from handler to `finally` (CR-02); anon bucket metered before tier resolves (CR-03, `main.py:65-69`). Zero `add_middleware`/`BaseHTTPMiddleware` usage (`grep` confirms). |
| `src/relay/config.py` | All phase-1 settings with documented defaults, `mcp_allow_writes` flipped to `False` | ✓ VERIFIED | All 4 limit strings, `max_daily_cost_usd`, `trust_proxy_header` (dual alias), `anon_auth_limit`, `api_key`/`demo_key` present with sane defaults. |
| `src/relay/mcp_server.py` | MCP writes gated by `settings.mcp_allow_writes` | ✓ VERIFIED | `ToolPolicy(allow_writes=settings.mcp_allow_writes)` at server construction. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `main.py` routes | `auth.py`/`ratelimit.py` | `Depends(create_gate/read_gate/process_gate)` | WIRED | All three protected routes carry the dependency; public routes (`/`, `/health`, `/metrics`, `/dashboard`) carry none. |
| `main.py:_gate._dependency` | `ratelimit.enforce("auth","anon",...)` | called before `_ANY_TIER(presented)` resolves | WIRED | Confirmed by reading `main.py:64-70` and by `test_repeated_wrong_keys_are_rate_limited` (Nth wrong key returns 429, not 401) — this is the CR-03 fix, independently re-verified here rather than trusted from REVIEW.md. |
| `main.py:event_stream` `finally` | `telemetry.record_run` | unconditional write of partial usage before `release_run` | WIRED | Confirmed by reading `main.py:154-180` and by driving the handler directly and calling `.aclose()` after one event (`test_mid_stream_disconnect_still_records_the_spend`) — this is the CR-01 fix, independently re-verified. |
| `main.py:process_ticket` reservation `token` | `ratelimit.release_run(token)` | handler captures `reserve_run()` return, generator `finally` releases the same token | WIRED | `main.py:132`, `180`; `test_a_stream_that_never_starts_leaks_only_until_the_ttl` proves an un-started generator still bounds the leak to the TTL rather than forever. |
| `agent.py:run_ticket` | `agent._execute_guarded(..., bound_ticket_id=ticket["id"])` | per-call keyword argument, not stored on shared `registry` | WIRED | `agent.py:194-197`; `test_concurrent_runs_do_not_cross_bind` proves no cross-contamination under real concurrency. |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|-----------------|--------------|--------|----------|
| SEC-01 | 01-03, 01-04 | API-key auth, constant-time compare, 401/403 semantics | ✓ SATISFIED | `auth.py`, tests in `test_auth.py` |
| SEC-02 | 01-04 | Per-key/IP rate limiting with 429 + headers | ✓ SATISFIED | `ratelimit.py`, tests in `test_ratelimit.py` |
| SEC-03 | 01-04 | Persistent daily USD spend circuit breaker | ✓ SATISFIED | `ratelimit.py` + `main.py` CR-01/CR-02 fixes; `test_budget_survives_restart`, `test_mid_stream_disconnect_still_records_the_spend` |
| SEC-04 | 01-02 | Server-side ticket_id binding + denial event | ✓ SATISFIED | `agent.py`, `test_guardrails.py` (mismatched/recovers/concurrent tests) |
| SEC-05 | 01-01 | MCP writes off by default | ✓ SATISFIED | `config.py`, `mcp_server.py`, `test_mcp.py` |
| SEC-06 | 01-05 | Published, tiered demo key | ✓ SATISFIED | `main.py:dashboard`, `README.md`, `demo.sh`; demo-tier vs owner-tier limit tests |

No orphaned requirement IDs — REQUIREMENTS.md lists exactly SEC-01..SEC-06 for Phase 1 and all six appear in at least one plan's `requirements:` frontmatter.

### Anti-Patterns Found

None in phase-modified files (`src/relay/main.py`, `ratelimit.py`, `auth.py`, `agent.py`, `config.py`, `mcp_server.py`). No `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` markers found by grep. `ruff check src tests` — all checks passed.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite | `python -m pytest tests/ -q` | `110 passed in 0.78s` | ✓ PASS (matches stated baseline, independently run, not trusted from SUMMARY) |
| Lint | `ruff check src tests` | `All checks passed!` | ✓ PASS |
| SEC-01: unauth request denied | read via `test_missing_key_returns_401_with_challenge` against real `TestClient` | 401 + `WWW-Authenticate: APIKey` | ✓ PASS |
| SEC-03: cold-start durability | `test_budget_survives_restart` — exhausts budget, tears down/recreates app against same on-disk sqlite file | 503 persists post-restart | ✓ PASS |
| SEC-04: cross-ticket write denial + correct-ticket landing | `test_mismatched_ticket_id_is_denied` + `test_run_recovers_after_binding_denial` — real seeded rows, real reply-table assertions | denial observed, victim gets 0 replies, retry lands correctly | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention exists in this project and none is referenced by any Phase 1 PLAN/SUMMARY. Step 7c: SKIPPED — no probes declared or discoverable. Verification instead relies on the pytest regression suite above, which is a stronger, first-class evidence source for this codebase's conventions.

### Human Verification Required

None. All observable truths were verifiable programmatically against the codebase (code reading + reproduction via the existing regression suite, independently executed). The two items flagged as "known-unverified, deploy-time" in `01-DEFERRED.md` (live Fly proxy `Fly-Client-IP` behavior from two networks; whether the deployed `fly secrets set RELAY_DEMO_KEY` value matches the README literal) are explicitly out of CI/verifier scope per the task brief and do not block phase closure — they are deploy-time operational checks, not code defects.

### Gaps Summary

No gaps. All three Critical findings from `01-REVIEW.md` (CR-01 mid-stream spend loss, CR-02 permanent reservation leak/DoS, CR-03 unmetered auth guessing) were fixed and are independently re-verified here by reading the code and re-running the load-bearing regression tests directly (not by trusting REVIEW.md or SUMMARY.md claims). WR-03 (proxy IP validation) and the documentation half of WR-05 (README's defended-claim narrowing) were also fixed and verified. The four remaining Warnings and all Info findings were deliberately deferred by explicit user decision, are recorded in `01-DEFERRED.md`, and — independently assessed in this report — none of them falsifies a SEC-01..SEC-06 requirement or a ROADMAP Phase 1 success criterion. The full 110-test suite passes and ruff is clean, both independently re-run rather than taken on faith.

---

_Verified: 2026-08-09T13:44:54Z_
_Verifier: Claude (gsd-verifier)_

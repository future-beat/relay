---
phase: 01-security-perimeter
plan: 05
subsystem: security-perimeter
tags: [auth, docs, dashboard, demo-key, readme, fly]
status: complete
requires:
  - "relay.config.settings.demo_key — from plan 01-01"
  - "relay.main._gate / create_gate / read_gate / process_gate — from plan 01-04"
  - "guardrail SSE event for ticket_id binding — from plan 01-02"
provides:
  - "dashboard block publishing the demo key, substituted from settings.demo_key at serve time"
  - "authenticated scripts/demo.sh — both curls carry X-API-Key with an env-with-default"
  - "README 'Security & limits' section: published key, surface split, tier table, $5/day ceiling, threat model, MCP opt-in, pre-deploy Fly secrets step"
affects:
  - "phase 6 dashboard rework inherits the demo block when DASHBOARD_HTML becomes a real template"
  - "any future phase that adds a tier or a limit must update the README table, which is now the operator-facing source"
tech-stack:
  added: []
  patterns:
    - "public credential rendered from settings at serve time via .replace() on a placeholder token, never a literal in the HTML constant"
    - "html.escape applied to a config value before it reaches the page"
    - "shell env-with-default (${RELAY_DEMO_KEY:-dev-demo-key}) so a fresh clone and production both work unchanged"
key-files:
  created: []
  modified:
    - src/relay/main.py
    - scripts/demo.sh
    - README.md
    - tests/test_auth.py
key-decisions:
  - ".replace() on a __RELAY_DEMO_KEY__ placeholder rather than an f-string — DASHBOARD_HTML's inline JS is full of ${...} template literals that an f-string would try to interpret"
  - "Unset demo_key renders the neutral string '(not configured)' rather than 'None' — /dashboard is the public landing surface and must never print a falsy value as if it were a credential"
  - "README publishes the production value 'relay-demo-2026' once; .env.example and demo.sh's default stay 'dev-demo-key' — the local dev key and the deployed demo key are deliberately different values"
  - "REQUIREMENTS.md deliberately NOT updated: the plan's blocking human checkpoint has not run, so no requirement in this plan's frontmatter can honestly be marked complete yet"
patterns-established:
  - "Single-source published credential: dashboard reads settings, README carries exactly one greppable literal, deploy note ties them together"
requirements-completed: [SEC-01, SEC-02, SEC-03, SEC-06]  # marked after Task 4 human verification passed 2026-08-09
metrics:
  duration: ~5 min (autonomous tasks only; checkpoint pending)
  tasks: 3 of 4
  commits: 4
  tests_before: 94
  tests_after: 97
  completed: null
---

# Phase 1 Plan 5: Published Demo Key & Perimeter Documentation Summary

**The demo key is now published on the dashboard from `settings.demo_key` and once in the README, `scripts/demo.sh` authenticates both of its calls, and the README documents every tier, limit, accepted risk and the pre-deploy secrets step — but the plan's blocking human end-to-end verification has not run, so this plan is NOT complete.**

## Status: COMPLETE — human verification passed 2026-08-09

Tasks 1-3 were committed autonomously. **Task 4 (`checkpoint:human-verify`, `gate="blocking"`) passed on 2026-08-09.**

### Task 4 verification record

**Eval suite** — run by the human, report `eval_results/eval-20260809T095909Z.json`:

| Metric | Result |
|--------|--------|
| Cases | 3 |
| Passed | 3 (pass_rate 1.0) |
| Mean quality | 5.0 |
| Total cost | $0.075 |
| Model | claude-sonnet-5 |

Per-case: `rate-limits-pro` → `send_reply` (grounded), `refund-monthly` → `create_escalation` (grounded), `password-reset` → `send_reply` (grounded).

**The signal the checkpoint was watching for:** zero `ended_without_action` outcomes, and both terminal paths exercised (reply and escalation). The plan flagged that a pass-rate drop concentrated in `ended_without_action` would mean the SEC-04 binding denial was worded too unrecoverably for the model to self-correct. That did not occur — the denial is recoverable, and grounding held on every case.

**Perimeter checks** — run by the orchestrator against a live local uvicorn on an isolated database (`RELAY_DB_PATH` pointed at scratch; the working `relay.db` was never touched):

| Check | Result |
|-------|--------|
| `/`, `/health`, `/metrics`, `/dashboard` without a key | public (307/200/200/200) |
| `POST /tickets` with no key | 401 + `WWW-Authenticate: APIKey` |
| `POST /tickets` with a wrong key | 401 |
| `POST /tickets` with the demo key | 201 |
| Demo key rendered on `/dashboard` | present, matches `settings.demo_key` |
| Demo rate limit (20/hr on create) | 429 at request 21 |
| 429 payload | friendly JSON + `Retry-After` + `X-RateLimit-Limit/Remaining/Reset` |
| Owner tier during demo lockout | 201 — independent bucket confirmed |
| Fail-closed (no keys configured) | `/health` 200, `POST /tickets` 503 |

That last row closes Task 2's deferred criterion and confirms the Docker `HEALTHCHECK` and CI `curl -sf` smoke job survive the fail-closed default.

**Still not verified (deploy-time, non-blocking):** the live Fly proxy's `Fly-Client-IP` behaviour from two networks, and that the README's `relay-demo-2026` literal matches what `fly secrets set RELAY_DEMO_KEY=...` actually sets — no code or test asserts that pairing.

### Continuation note

The original executor agent for this plan died mid-stream on an API error after committing Task 3 (`55e1770`) but before writing this file. This SUMMARY was written by a continuation agent whose scope was: verify the existing four commits, write and commit this file, and return the checkpoint. **No task work was redone** — all four commits are the original agent's, verified present on `worktree-agent-a765aeefbbe24c479` and unmodified.

## Performance

- **Duration:** ~5 min of autonomous execution (17:01:29 → 17:05:58 +0800), plus continuation overhead
- **Started:** 2026-08-09T17:01:29+08:00 (first task commit)
- **Completed:** 2026-08-09 (Task 4 human verification passed)
- **Tasks:** 4 of 4 complete
- **Files modified:** 4

## Accomplishments

- `GET /dashboard` publishes the demo key inside a visible "Try it yourself" block, substituted from `settings.demo_key` at request time, so the page can never advertise a key the service would reject
- `scripts/demo.sh` works unchanged against the now-closed perimeter — both curls carry `X-API-Key`, defaulting to the `.env.example` dev value and overridable by exporting `RELAY_DEMO_KEY` for production
- README gained a full "Security & limits" section (+162 lines): the published key with a runnable curl, the public/protected surface split with the reason `/health` stays open, the tier table, the $5/day UTC-reset ceiling and its SQLite derivation, the `ticket_id` binding `guardrail` event, an explicit accepted-risk list, and the blocking pre-deploy `fly secrets set` step
- Corrected inverted MCP wording in the README: writes are off unless `RELAY_MCP_ALLOW_WRITES=true` (no line now instructs setting it to `false` for a read-only surface)

## Task Commits

1. **Task 1 (RED): failing tests for the published demo key** — `e0804d5` (test) — 3 tests added to `tests/test_auth.py`
2. **Task 1 (GREEN): publish the demo key on the dashboard from settings** — `63d8145` (feat) — `src/relay/main.py`
3. **Task 2: send the published demo key from `scripts/demo.sh`** — `ea54e24` (feat)
4. **Task 3: document the security perimeter in the README** — `55e1770` (docs)
5. **Task 4: end-to-end human verification** — **PASSED** 2026-08-09 (no code commit; evidence recorded in the Status section — eval report `eval_results/eval-20260809T095909Z.json`, 3/3 pass, plus the live perimeter check table)

TDD gate compliance for Task 1: `test(...)` at `e0804d5` precedes `feat(...)` at `63d8145`. No REFACTOR commit — none was needed.

## Files Created/Modified

- `src/relay/main.py` — `dashboard()` now returns `DASHBOARD_HTML.replace("__RELAY_DEMO_KEY__", published)` where `published = escape(settings.demo_key) if settings.demo_key else "(not configured)"`; added a `.demo` CSS block and the announcement markup
- `scripts/demo.sh` — `-H "X-API-Key: ${RELAY_DEMO_KEY:-dev-demo-key}"` on both curls, plus a header comment pointing at the README/dashboard
- `README.md` — new "Security & limits" section; MCP wording corrected; deploy notes now order `fly secrets set` before `fly deploy`
- `tests/test_auth.py` — `test_dashboard_publishes_the_demo_key`, `test_dashboard_demo_key_is_sourced_not_hardcoded`, `test_dashboard_without_a_demo_key_does_not_render_none`

## Verification Results

Full suite and lint re-run by the continuation agent in this worktree:

- `PYTHONPATH=src python -m pytest -q` → **97 passed** (94 before this plan)
- `ruff check src tests` → **All checks passed!**

Acceptance criteria spot-checked non-destructively, all holding:

| Criterion | Expected | Actual |
|---|---|---|
| `grep -c 'settings.demo_key' src/relay/main.py` | ≥1 | 1 |
| Demo key literal inside `DASHBOARD_HTML` | absent | absent — only the `__RELAY_DEMO_KEY__` placeholder |
| Monkeypatch sentinel appears in `/dashboard` body | pass | covered by `test_dashboard_demo_key_is_sourced_not_hardcoded` |
| `/dashboard` with `demo_key=None` → 200, no `>None<` | pass | covered by `test_dashboard_without_a_demo_key_does_not_render_none` |
| `grep -c 'X-API-Key' scripts/demo.sh` | 2 | 2 |
| `grep -c 'RELAY_DEMO_KEY:-' scripts/demo.sh` | 2 | 2 |
| `bash -n scripts/demo.sh` | exit 0 | exit 0 |
| `test -x scripts/demo.sh` | pass | pass |
| `grep -c 'X-API-Key' README.md` | ≥1 | 2 |
| `grep -c 'RELAY_MCP_ALLOW_WRITES=true' README.md` | ≥1 | 1 |
| `RELAY_MCP_ALLOW_WRITES=false` instruction present | 0 | 0 |
| `grep -ci 'fly secrets set' README.md` | ≥1 | 3 |
| `grep -c 'RELAY_TRUST_PROXY' README.md` | ≥1 | 2 |
| `grep -c '5/hour' README.md` | ≥1 | 1 |
| `grep -c 'guardrail' README.md` | ≥1 | 6 |
| `grep -c '00:00 UTC' README.md` | ≥1 | 1 |
| Demo key literal appears exactly once in README | 1 | 1 (`relay-demo-2026`, line 87; no other tracked file contains it) |

**Not verified — deferred to the Task 4 checkpoint by the plan's own wording:**

- Task 2's last criterion, "against a locally running server, a POST to `/tickets` with a deliberately wrong `X-API-Key` returns 401". No server was started; the plan explicitly permits deferring this to Task 4 (step 5 covers it).
- Every Task 4 criterion: a real streamed run, the injection `guardrail` frame observed live, and the eval pass rate.

## Deviations from Plan

### Auto-fixed issues

None. Tasks 1-3 executed as written.

### Observations worth recording (no code changed)

**1. The README's published key is a manually-maintained literal, by design but not by enforcement.**
`README.md` publishes `relay-demo-2026`; `.env.example` and `scripts/demo.sh`'s fallback use `dev-demo-key`. That split is intentional — the local dev key and the deployed demo key are different values, and `demo.sh` defaulting to the `.env.example` value is exactly what Task 2 asked for. The dashboard↔service pair genuinely cannot drift (both read `settings.demo_key`), which is what threat T-01-29 required. But the **README↔Fly-secret** pair has no such guarantee: nothing in code or tests asserts that `relay-demo-2026` is what `fly secrets set RELAY_DEMO_KEY=...` actually sets. This is checkpoint step 8's job and is called out here so it is not mistaken for an automated invariant.

**2. Out-of-scope discovery logged, not fixed.**
`README.md`'s Deployment section says "CI runs lint, the 37-test suite, and a Docker build"; the suite is now 97 tests. The count was already stale before this phase. Recorded in `.planning/phases/01-security-perimeter/deferred-items.md` rather than fixed — unrelated to the perimeter, and Task 3's acceptance criteria pin the README diff to security content.

## Authentication Gates

None encountered during Tasks 1-3.

## Requirements status

This plan's frontmatter claims `requirements: [SEC-06, SEC-01, SEC-02, SEC-03, SEC-05]`. **None were marked complete by this plan**, deliberately:

- SEC-06's implementation half is shipped and tested; its "a visitor can actually run the agent with the published key" half is precisely what checkpoint step 3 verifies.
- SEC-01/02/03 were built in plans 01-01 through 01-04 and are covered by the unit suite, but this plan is the phase's end-to-end gate for them.
- SEC-05 is already `Complete` in `REQUIREMENTS.md` from an earlier plan; untouched here.

The orchestrator should mark SEC-01, SEC-02, SEC-03 and SEC-06 complete **after** the human approves Task 4, not before. `STATE.md` and `ROADMAP.md` were intentionally not touched by this agent.

## Known Stubs

None. No placeholder values, empty returns, or unwired data paths were introduced. The `"(not configured)"` string in `dashboard()` is deliberate degraded-mode copy for an unconfigured deployment (threat T-01-31), not a stub.

## Threat Flags

None. This plan introduced no new network endpoint, auth path, file access pattern or schema change. It publishes an existing credential on an existing public route — covered by T-01-28 (accept, per D-02) and T-01-29/30/31 (mitigated: serve-time substitution, `demo_key` only, neutral placeholder when unset), all of which the shipped code and the three new tests satisfy.

## Next Steps

1. Human works through Task 4's `<how-to-verify>` (steps 1-8 pre-deploy; step 9 post-deploy, non-blocking).
2. On approval: record the eval pass rate from step 7 verbatim in this file, mark SEC-01/02/03/06 complete, advance `STATE.md`.
3. On failure: record the failure verbatim here — the plan forbids fixing inside the checkpoint; a follow-up plan handles it.
4. Before any `fly deploy`: `fly secrets set RELAY_API_KEY=... RELAY_DEMO_KEY=relay-demo-2026`. Auth fails closed, so deploying first returns 503 on every protected route.

## Self-Check: PASSED

All four task commits (`e0804d5`, `63d8145`, `ea54e24`, `55e1770`) plus the metadata commit (`80f7a65`) exist on `worktree-agent-a765aeefbbe24c479`. All claimed files exist on disk. The metadata commit deleted no tracked files. `STATE.md`, `ROADMAP.md` and `REQUIREMENTS.md` are unmodified across the whole plan diff, as intended.

---
phase: 1
slug: security-perimeter
status: planned
nyquist_compliant: true
wave_0_complete: false
created: 2026-08-06
updated: 2026-08-06
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_auth.py tests/test_ratelimit.py -q` |
| **Full suite command** | `.venv/bin/python -m pytest -q` |
| **Estimated runtime** | ~2 seconds (baseline: 38 passed in 1.76s) |
| **Lint gate** | `ruff check src tests` |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (or the test file touched by the task)
- **After every plan wave:** Run `.venv/bin/python -m pytest -q && ruff check src tests`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-T1 | 01 | 1 | SEC-02 (enabler) | T-01-SC | `limits` installed from PyPI, slopcheck `[OK]` | smoke | `.venv/bin/python -c "from limits.aio.storage import MemoryStorage"` | n/a | ⬜ pending |
| 01-01-T2 | 01 | 1 | SEC-01/02/03/05 | T-01-04 | Conservative literal defaults for every threshold; no fail-open knob | unit | `.venv/bin/python -c "from relay.config import Settings; ..."` | ✅ config.py | ⬜ pending |
| 01-01-T3 | 01 | 1 | SEC-05 | T-01-01, T-01-02 | MCP write tools denied unless explicitly opted in | unit | `pytest tests/test_mcp.py -q` | ✅ | ⬜ pending |
| 01-02-T1 | 02 | 1 | SEC-04 | T-01-05, T-01-10 | Model-supplied `ticket_id` rejected between validation and execution | unit | `pytest tests/test_guardrails.py tests/test_mcp.py -q` | ✅ | ⬜ pending |
| 01-02-T2 | 02 | 1 | SEC-04 | T-01-07, T-01-09 | Denial observable as a `guardrail` event + warning log + span attr | integration | `pytest -q` | ✅ | ⬜ pending |
| 01-02-T3 | 02 | 1 | SEC-04 | T-01-06, T-01-08 | No cross-write, run recovers, concurrent runs never cross-bind | integration | `pytest tests/test_guardrails.py -q` | ✅ file, ❌ tests (created by task) | ⬜ pending |
| 01-03-T1 | 03 | 2 | SEC-01, SEC-06 | T-01-11, T-01-12, T-01-13, T-01-18 | Constant-time tier resolution; fail closed; 401 challenge; 403 tier gate | unit | `pytest tests/test_auth.py -q` | ❌ W0 (created by task) | ⬜ pending |
| 01-03-T2 | 03 | 2 | SEC-02, SEC-03 | T-01-14..T-01-17, T-01-19 | Moving window with correct headers; UTC-day spend sum; in-flight reservation | unit | `pytest tests/test_ratelimit.py -q` | ❌ W0 (created by task) | ⬜ pending |
| 01-03-T3 | 03 | 2 | SEC-01..06 (harness) | — | Limiter state reset per test; single authed client fixture | integration | `pytest -q` (and reversed file order) | ✅ conftest.py | ⬜ pending |
| 01-04-T1 | 04 | 3 | SEC-01, SEC-02, SEC-03 | T-01-20..T-01-25 | Controls as route dependencies, resolved before the 200 status line locks | integration | `pytest -q` + route-dependency introspection one-liner | ✅ | ⬜ pending |
| 01-04-T2 | 04 | 3 | SEC-01, SEC-06 | T-01-20, T-01-22, T-01-27 | 401 + `WWW-Authenticate`; public routes open; fail-closed 503 spares `/health` | integration | `pytest tests/test_auth.py tests/test_api.py -q` | ✅ | ⬜ pending |
| 01-04-T3 | 04 | 3 | SEC-02, SEC-03 | T-01-23, T-01-25, T-01-26 | Tiered 429 headers; 503 ceiling across restart and against in-flight runs | integration | `pytest tests/test_ratelimit.py -q` | ✅ | ⬜ pending |
| 01-05-T1 | 05 | 4 | SEC-06 | T-01-28..T-01-31 | Published key rendered from settings, cannot drift, never 500s | integration | `pytest tests/test_auth.py -q` | ✅ | ⬜ pending |
| 01-05-T2 | 05 | 4 | SEC-06 | T-01-28 | Demo script authenticates | smoke | `bash -n scripts/demo.sh` + grep gate | ✅ | ⬜ pending |
| 01-05-T3 | 05 | 4 | SEC-01/02/03/05/06 | T-01-26, T-01-32 | Threat model, limits, ceiling and deploy ordering documented | doc gate | grep assertions on README.md | ✅ | ⬜ pending |
| 01-05-T4 | 05 | 4 | SEC-01..06 | T-01-33 | Real run, injection guard visible, eval pass rate not regressed | human + suite | `pytest -q && ruff check src tests`, then `python -m relay.evals --limit 3` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Wave 0 gaps are closed inside the plans that consume them (each test file is created in the
same task as the module it exercises, so no task is left without an automated verify):

- [ ] `tests/test_auth.py` — created by plan 03 task 1 (SEC-01/SEC-06 unit), extended by plan 04 task 2 and plan 05 task 1 (integration)
- [ ] `tests/test_ratelimit.py` — created by plan 03 task 2 (SEC-02/SEC-03 unit), extended by plan 04 task 3 (integration)
- [ ] `tests/conftest.py` — plan 03 task 3 adds the autouse `MemoryStorage`-reset fixture and the shared authed `client` fixture, and retires the two duplicated local fixtures
- [ ] No framework install needed — pytest, pytest-asyncio and httpx are already present

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Fly-Client-IP keying behind the real Fly proxy | SEC-02 | Proxy header only present in production | After deploy: two curls from different networks with the demo key; confirm independent 429 windows (plan 05 task 4 step 9) |
| Spend ceiling survives a real cold start | SEC-03 | Requires machine stop/start on the Fly volume | Exhaust the budget in staging, `fly machine stop/start`, confirm the 503 persists. (A same-file second `TestClient` covers the durable-state half automatically in plan 04 task 3) |
| Eval pass rate after the binding denial | SEC-04 | Costs real Claude spend; the unit suite cannot observe eval outcomes | `python -m relay.evals --limit 3`, compare to baseline (plan 05 task 4 step 7) |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** planned 2026-08-06 — pending execution

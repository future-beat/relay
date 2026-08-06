---
phase: 1
slug: security-perimeter
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-08-06
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

---

## Sampling Rate

- **After every task commit:** Run the quick run command (or the test file touched by the task)
- **After every plan wave:** Run `.venv/bin/python -m pytest -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

(Filled by planner — every task must map to an automated command below or a Wave 0 stub.)

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| — | — | — | SEC-01..06 | — | — | unit/integration | `pytest -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_auth.py` — stubs for SEC-01 (401/403/WWW-Authenticate, constant-time tiers), SEC-06 (demo vs owner tier)
- [ ] `tests/test_ratelimit.py` — stubs for SEC-02 (429 + Retry-After + X-RateLimit-*), SEC-03 (spend ceiling 503), with an autouse MemoryStorage-reset fixture (research landmine: module-level limiter state leaks across tests)
- [ ] Existing `tests/conftest.py` — extend fixtures to inject `RELAY_API_KEY`/`RELAY_DEMO_KEY` test values so the 5 known-breaking tests are fixed deliberately, not silently

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Fly-Client-IP keying behind the real Fly proxy | SEC-02 | Proxy header only present in production | After deploy: two curls from different networks with demo key; confirm independent 429 windows |
| Spend ceiling survives cold start | SEC-03 | Requires machine stop/start on Fly volume | Exhaust budget in staging, `fly machine stop/start`, confirm 503 persists |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending

---
phase: 2
slug: async-safe-data-layer-graceful-shutdown
status: planned
nyquist_compliant: true
wave_0_complete: true
created: 2026-08-09
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `02-RESEARCH.md` § Validation Architecture. Every row below was
> **written and executed green** against a shadow implementation (122 passed),
> so these are handed-over shapes, not speculative names.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `.venv/bin/python -m pytest -q tests/test_db.py tests/test_lifecycle.py` |
| **Full suite command** | `.venv/bin/python -m pytest -q` |
| **Baseline** | 110 passed; `ruff check src tests` clean (measured at `8842c87`) |
| **Lint gate** | `ruff check src tests` |

---

## Sampling Rate

- **After every task commit:** `pytest -q tests/test_db.py tests/test_lifecycle.py` (< 2s)
- **After every plan wave:** `pytest -q && ruff check src tests` — must show ≥110 passing, zero lint errors
- **Phase gate:** full suite green, **plus the concurrency test run 5 consecutive times**. This is the one test whose failure mode is flaky rather than deterministic — research measured the naive wrapper failing 4 of 5 runs, so a single green run proves nothing.
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Req ID | Behavior | Test Type | Automated Command | File Exists | Status |
|--------|----------|-----------|-------------------|-------------|--------|
| DATA-01-a | WAL, `busy_timeout=5000`, `foreign_keys=ON` on a file DB | unit | `pytest tests/test_db.py::test_wal_is_enabled_on_a_file_database -x` | ❌ W0 | ⬜ pending |
| DATA-01-b | WAL is a silent no-op on `:memory:` (the trap, written down) | unit | `pytest tests/test_db.py::test_wal_is_a_silent_no_op_on_memory_databases -x` | ❌ W0 | ⬜ pending |
| DATA-01-c | FK enforcement survives the wrapper | unit | `pytest tests/test_db.py::test_foreign_keys_still_enforced -x` | ❌ W0 | ⬜ pending |
| DATA-01-d | A failed write leaves no partial row when another thread commits concurrently (barrier-driven, deterministic) | unit | `pytest tests/test_db.py::test_a_failed_write_does_not_leave_a_partial_row_when_another_thread_commits -x` | ❌ W0 | ⬜ pending |
| DATA-01-e | **D-02:** no registered executor (agent *or* MCP registry) is a coroutine function | unit | `pytest tests/test_lifecycle.py::test_no_registered_executor_is_a_coroutine_function -x` | ❌ W0 | ⬜ pending |
| DATA-01-f | Tool execution runs off the event loop (proves the offload, not just that it still works) | unit | `pytest tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop -x` | ❌ W0 | ⬜ pending |
| DATA-01-g | 6 overlapping `/process` runs → 6 `runs` rows, 6 replies, 6 resolved tickets, no errors | integration | `pytest tests/test_lifecycle.py::test_overlapping_runs_all_record_without_locking_errors -x` | ❌ W0 | ⬜ pending |
| DATA-02-a | **D-06:** registry is empty after a run completes (scale-to-zero guard) | integration | `pytest tests/test_lifecycle.py::test_registry_is_empty_after_a_run_completes -x` | ❌ W0 | ⬜ pending |
| DATA-02-b | Drain returns immediately (<50ms) when idle | unit | `pytest tests/test_lifecycle.py::test_drain_returns_immediately_when_idle -x` | ❌ W0 | ⬜ pending |
| DATA-02-c | Drain waits for an in-flight run, then returns True | unit | `pytest tests/test_lifecycle.py::test_drain_waits_for_an_in_flight_run_then_returns -x` | ❌ W0 | ⬜ pending |
| DATA-02-d | Drain returns False on timeout rather than hanging shutdown | unit | `pytest tests/test_lifecycle.py::test_drain_times_out_rather_than_hanging_shutdown -x` | ❌ W0 | ⬜ pending |
| DATA-02-e | A stream cancelled before its generator starts registers nothing (the CR-02 asymmetry) | integration | `pytest tests/test_lifecycle.py::test_a_stream_that_never_starts_registers_nothing -x` | ❌ W0 | ⬜ pending |
| DATA-02-f | **D-07 regression — already exists, must stay green:** mid-stream disconnect still records the spend | integration | `pytest tests/test_observability.py::test_mid_stream_disconnect_still_records_the_spend -x` | ✅ exists | ⬜ pending |
| D-09 | `POST /tickets/{id}/process` returns 503 while draining | integration | `pytest tests/test_lifecycle.py -k draining -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Critical note on DATA-01-d and DATA-01-g:** research proved that a wrapper which returns a live
`sqlite3.Cursor` (stepped by the caller after the lock releases) fails in a way that does **not**
raise `OperationalError` — it yields `Ticket` rows with `customer_email=None` and `status=''`, plus
spurious 404s. A test asserting only "no `OperationalError`" would pass while the app feeds the
model a null customer record. These tests must assert on **row contents**, not just absence of
exceptions.

---

## Wave 0 Requirements

- [ ] `tests/test_db.py` — new file: connection/pragma/wrapper behaviour (DATA-01-a..d)
- [ ] `tests/test_lifecycle.py` — new file: offload seam, registry, drain (DATA-01-e..g, DATA-02-a..e, D-09)
- [ ] New **file-backed** `conn` fixture for WAL assertions. Note: `conftest.py`'s existing `client`
      fixture is already file-backed at `tmp_path/"test.db"` — only the `conn` fixture is `:memory:`,
      so **no existing fixture changes**. Research measured the full change set landing with zero
      edits to any existing test.
- [ ] No framework install needed — no new dependencies in this phase at all

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `kill_timeout = 30` on the live machine | DATA-02 / D-05 | Platform config, not observable from the app | `fly config show`, then `fly deploy` during an active run and confirm the drain log line instead of a truncated stream |
| Machine still reaches `stopped` when idle | DATA-02 / D-06 | Fly autostop behaviour, post-deploy only | `fly machine list` after this phase — the real-world counterpart to the registry-empty test |

**Recommended addition to the CI `docker` job** (cheap, and the only automatable end-to-end check of
the signal path): `docker stop --time=35 relay`, then assert exit code 0 and `shutdown.drain_complete`
present in `docker logs`.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] Concurrency test green 5 consecutive runs
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** covered by plans 02-01..02-05 (2026-08-09)

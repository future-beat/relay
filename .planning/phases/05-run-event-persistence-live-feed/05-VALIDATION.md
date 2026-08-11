---
phase: 5
slug: run-event-persistence-live-feed
status: planned
nyquist_compliant: true
wave_0_complete: true
created: 2026-08-11
---

# Phase 5 — Validation Strategy

> Derived from `05-RESEARCH.md` § Validation Architecture. Two tests are load-bearing:
> the D-04 atomicity test (a step's write and its event row commit together) and the
> SC-3 redaction leak test — both must be mutation-checked, per this project's recurring
> "unfalsifiable check" failure mode.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_run_events.py -x -q` |
| **Full suite** | `.venv/bin/python -m pytest -q` (288 passing at Phase 4 close — must not regress) |
| **CI path** | `.github/workflows/ci.yml` `test` job (free, no keys); `conftest._no_outbound_http` blocks accidental API calls |
| **Lint gate** | `ruff check src tests` |

---

## Sampling Rate

- **After every task commit:** `pytest tests/test_run_events.py -x -q`
- **After every wave:** `pytest -q && ruff check src tests` — the existing `test_lifecycle.py` drain/registry tests must stay green (they guard against the broker reintroducing a CR-01/CR-02 leak)
- **Phase gate:** full suite green before verify
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Req / SC | Behavior | Type | Automated command | File | Status |
|----------|----------|------|-------------------|------|--------|
| DATA-03 / SC-1 | A completed run persists one `run_events` row per yielded event, in `seq` order, joined to `runs` via `run_uid` | integration | `pytest tests/test_run_events.py::test_a_run_persists_its_full_event_sequence -x` | ❌ W0 | ⬜ |
| DATA-03 / SC-1 | **Atomicity (load-bearing):** a `send_reply` commits its reply row + event row together; forcing the event insert to raise rolls back the reply too (D-04) | integration | `pytest tests/test_run_events.py::test_send_reply_and_its_event_row_commit_atomically -x` | ❌ W0 | ⬜ |
| DATA-03 | Guarded `ALTER TABLE runs ADD run_uid` is idempotent; second `init_db` on a populated DB doesn't raise; legacy rows keep `run_uid=NULL` (D-13) | unit | `pytest tests/test_run_events.py::test_run_uid_migration_is_idempotent -x` | ❌ W0 | ⬜ |
| DASH-01 / SC-2 | **`/events` smoke:** subscribe, run a ticket, assert projected frames arrive live (proves SC-2 without the Phase 6 UI) | integration | `pytest tests/test_run_events.py::test_events_delivers_a_live_run -x` | ❌ W0 | ⬜ |
| DASH-01 / SC-3 | **Redaction leak test (load-bearing):** seed customer email + ticket body + fake key; assert none appear in any `/events` frame. Mutation-checked — spreading a raw field flips it red (D-07/D-08) | integration | `pytest tests/test_run_events.py::test_no_projection_leaks_sensitive_data -x` | ❌ W0 | ⬜ |
| DASH-01 / SC-4 | **Fire-and-forget:** a stalled subscriber's full queue drops its oldest frame; `publish` never blocks or raises; the paid run completes (D-10) | unit | `pytest tests/test_run_events.py::test_publish_drops_oldest_and_never_blocks -x` | ❌ W0 | ⬜ |
| DASH-01 | **No leaked subscriber:** an `/events` stream that disconnects (or never starts) leaves the broker with zero subscribers — the CR-02 asymmetry guard | integration | `pytest tests/test_run_events.py::test_events_disconnect_unsubscribes -x` | ❌ W0 | ⬜ |
| D-06 | Publish strictly after commit — a frame reaches a subscriber only after its `run_events` row is durable | integration | `pytest tests/test_run_events.py::test_broker_never_leads_the_database -x` | ❌ W0 | ⬜ |
| D-09 / SC-4 | `/events` emits a heartbeat during a quiet period and idle-closes after the ceiling (short ceiling in test) | integration | `pytest tests/test_run_events.py::test_events_heartbeats_then_idle_closes -x` | ❌ W0 | ⬜ |
| SC-4 | Scale-to-zero: an open `/events` subscriber does NOT register an agent run (`RunRegistry.active` unaffected); `lifespan broker.close()` ends open streams (D-12) | integration | `pytest tests/test_run_events.py::test_events_viewer_is_not_a_registered_run -x` | ❌ W0 | ⬜ |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**The two that cannot be allowed to pass vacuously:**
- **Atomicity** must actually force the event insert to raise mid-transaction (monkeypatch the recorder's insert) and assert the *reply* row is absent — not merely that both rows exist on the happy path. Mirror `test_lifecycle.py`'s closed-database pattern.
- **Redaction** must capture *every* frame from a real run seeded with known-sensitive strings and assert absence, then be mutation-checked by spreading a raw field into the projection. An allowlist that silently leaks is this project's most-repeated failure.

---

## Wave 0 Requirements

- [ ] `tests/test_run_events.py` — new file, rows above (DATA-03 + DASH-01 + SC-1..4)
- [ ] `tests/conftest.py` — a `broker` fixture + a helper driving `event_stream`/`/events` and capturing published frames (reuse `helpers.FakeClient`/`TicketAwareFakeClient` for the agent side)
- [ ] Atomicity harness — monkeypatch the recorder's `_insert_event` to raise mid-transaction (mirrors `test_lifecycle.py`'s closed-DB pattern)
- [ ] No framework install — pytest/pytest-asyncio already present

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Open `/events` tab does not hold the Fly machine awake | SC-4 / D-09 | Fly autostop keys on active inbound connections; only observable post-deploy | After deploy: open the dashboard, leave it idle past the ceiling, `fly machine list` shows `stopped` |
| `run_uid` column present on the live volume after deploy | DATA-03 / D-13 | The `ALTER TABLE` runs against the existing prod DB, not a fresh one | After deploy: `sqlite3 /data/relay.db '.schema runs'` shows `run_uid` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] Atomicity + redaction tests mutation-checked
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** covered by plans 05-01..05-04 (2026-08-11); plan-check 2 blockers + 2 warnings resolved in-plan.

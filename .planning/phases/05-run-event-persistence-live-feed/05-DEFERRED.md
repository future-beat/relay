# Phase 5 — Deferred Items

**Decided:** 2026-08-12
**Source:** `05-REVIEW.md` — 3 CRITICALs and 12 WARNINGs raised; 3 + 8 fixed in-phase, 4 deferred here by explicit user decision.

Fixed in-phase: CR-01, CR-02, CR-03, WR-01, WR-02, WR-04, WR-05, WR-06, WR-07, WR-09, WR-10.

---

## WR-03 — `RunEventBroker.closed` is write-only; `subscribe()` ignores it and `close()` never clears `_subs`

**File:** `src/relay/events.py`

`close()` sets `closed` and pushes the sentinel to every subscriber, but never clears `_subs`, and `subscribe()` does not consult `closed`. A subscriber arriving after shutdown begins gets a live queue that nobody will ever publish to, and the set retains entries for streams that have already ended.

**Why deferred:** shutdown is a single short-lived window in a single-process deployment, and the lifespan order (drain → `broker.close()` → `conn.close()`) means no run is still publishing by then. The observable symptom is a queue that is garbage-collected moments later.

**Risk if left:** low. A subscribe/close race leaves a stream hanging until its idle ceiling rather than closing promptly.

---

## WR-08 — the guarded `ALTER TABLE` is check-then-act and races two processes booting against the same volume

**File:** `src/relay/db.py` (`init_db`)

`PRAGMA table_info(runs)` then a conditional `ALTER TABLE runs ADD COLUMN run_uid TEXT` is not atomic. Two processes booting simultaneously against one volume can both read the column as absent and both attempt the `ALTER`; the loser raises `duplicate column name`.

**Why deferred:** the deployment is explicitly single-machine, single-writer (`fly.toml`, one volume, `min_machines_running=0`). The race needs two concurrent boots against the same volume, which this topology does not produce.

**Risk if left:** low today, higher the moment a second machine or a blue-green deploy is introduced. Revisit before any multi-instance change.

**Fix when taken up:** catch the `sqlite3.OperationalError` for the duplicate-column case and treat it as success, making the migration idempotent under concurrency rather than only under repetition.

---

## WR-11 — `events.py` breaks the project's signature conventions on its most safety-critical function

**File:** `src/relay/events.py`

CLAUDE.md requires keyword-only parameters (`*`) past 2–3 arguments. `execute_and_record` takes several positionally.

**Why deferred:** cosmetic. No behavioural risk.

**Risk if left:** a future caller can transpose positional arguments at the one seam where D-04 atomicity lives.

---

## WR-12 — `test_events_heartbeats_then_idle_closes` has a thin CI timing margin

**File:** `tests/test_run_events.py`

The heartbeat/idle test uses short injected intervals and could flake on a loaded CI runner.

**Why deferred:** it is not flaky locally (the D-09 mutation reds it reliably, and the unmutated stream returns in ~0.21s), and widening the margin trades CI time for stability that has not yet been shown to be needed.

**Risk if left:** an intermittent red build. If it flakes once, widen the margin rather than weakening the assertion — the test is load-bearing for scale-to-zero.

---

## Loose ends carried from earlier phases (still open)

- Phase 4's real-model recovery probe exists only as a SUMMARY transcript, not a persisted JSON artifact (~$0.03 to re-run properly).
- Phase 4's `IN-05` workflow-input hardening is untested.
- Phase 3's WR-06: the `conftest` outbound-HTTP guard's `AssertionError` is swallowed by `_embed_query`'s broad `except Exception`.
- The `client` fixture builds the tool registry during lifespan, before a test body can pin `settings.voyage_api_key = None`. No paid call is reachable today, but a future test using that fixture *and* driving retrieval cannot rely on a test-body pin alone.

---

*Phase: 05-run-event-persistence-live-feed*

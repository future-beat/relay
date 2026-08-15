---
phase: 05-run-event-persistence-live-feed
verified: 2026-08-12T05:05:00Z
status: human_needed
score: 4/6 must-haves verified, 2 deferred to Phase 6, 0 failed
overrides_applied: 0
re_verification:
  previous_status: none
  previous_score: n/a
  gaps_closed: []
  gaps_remaining: []
  regressions: []
deferred:
  - truth: "SC-2 — an open dashboard tab shows runs appearing in real time over /events with no polling"
    addressed_in: "Phase 6"
    evidence: "ROADMAP Phase 5 UI hint: 'scoped to the thinnest /events smoke that proves SC-2; the designed dashboard is Phase 6'. Phase 6 SC-4: 'A visitor can submit a prefilled example ticket from the page with the demo key and watch that run stream live'. The transport half is fully verified in this phase; DASHBOARD_HTML still polls /metrics every 5s and contains no EventSource."
  - truth: "DASH-01 — 'The dashboard RECEIVES a live run feed ... (no polling)'"
    addressed_in: "Phase 6 (substantively — but see WARNING-4: no phase formally OWNS this clause in REQUIREMENTS.md)"
    evidence: "Phase 6 SC-4 covers the browser-side consumption; Phase 6's requirements list is DASH-02..05, so DASH-01 has no owning phase after this one."
human_verification:
  - test: "After deploy, open the dashboard, leave it idle past events_idle_seconds (300s), then `fly machine list`"
    expected: "The machine reaches `stopped` — a forgotten tab does not pin it awake"
    why_human: "Fly autostop keys on active inbound connections at the proxy; only observable post-deploy (carried from 05-VALIDATION Manual-Only row 1)"
  - test: "After deploy, `sqlite3 /data/relay.db '.schema runs'` and `.schema run_events`"
    expected: "`run_uid` present on `runs`; `run_events` table and `idx_run_events_run_uid` present; boot did not raise"
    why_human: "The guarded ALTER TABLE runs against the pre-existing prod volume, not a fresh DB (carried from 05-VALIDATION Manual-Only row 2). WR-08's check-then-act race is deferred, so a clean boot is the only evidence."
  - test: "Adversarial wakefulness: from one IP, hold an /events stream and reconnect inside every 300s ceiling for ~20 minutes; watch `fly machine list` and the Fly bill"
    expected: "Decide whether a deliberately-held public stream keeping the machine at `started` is acceptable, or whether an aggregate connection-time budget / fly.toml concurrency limit is needed"
    why_human: "SC-4's literal claim ('streams are capped') is met, but nothing bounds total connection-holding time; this is a cost/policy judgement, not a code fact (see WARNING-3)"
  - test: "Read src/relay/events.py attribute_to_run's docstring against src/relay/telemetry.py _PUBLIC_RUN_COLUMNS and decide the run_uid disclosure posture"
    expected: "Either run_uid is public (then /metrics may carry it again and the WR-10 test's rationale needs restating) or it is not (then /events must stop stamping it, or Phase 6 must never build an unauthenticated run_uid→run_events lookup)"
    why_human: "The phase made two opposite disclosure decisions about the same value; which one is intended is a product/security call (see WARNING-1)"
  - test: "Decide the intended behaviour when a run_events INSERT fails mid-run (see WARNING-2)"
    expected: "Either fail-closed is confirmed as the contract (and gets a test + an `error` event so the client is not left with a truncated stream), or persistence becomes best-effort"
    why_human: "A deliberate durability-vs-availability trade-off; the code currently picks one silently and nothing tests it"
---

# Phase 5: Run Event Persistence & Live Feed — Verification Report

**Phase Goal:** Every agent step is durably recorded and visitors watch runs happen live
**Requirements:** DATA-03, DASH-01
**Verified:** 2026-08-12
**Status:** human_needed
**Re-verification:** No — initial verification

**Baseline confirmed independently:** `.venv/bin/python -m pytest -q` → **338 passed** (2.7s); `.venv/bin/ruff check src tests` → **All checks passed!**; working tree clean at start and at finish.

---

## Method

This was not a read-the-SUMMARY pass. Three independent lines of evidence were used:

1. **Source tracing** — every `yield` site in `agent.py`, every `publish`/`project`/`snapshot_frame` call site in `src/`, the whole of `events.py`, the `/events` and `event_stream` generators, `db.py`'s DDL + migration + sweep, and `telemetry.py`'s public column list.
2. **An independent SC-3 leak probe I wrote myself** (not the shipped test): nine sentinels through nine distinct fields — customer email, customer name, ticket subject, ticket body, fake API key, reply body, escalation reason, a fabricated citation, and an invalid category — driven through one real run that also includes a citation-guard denial, a Pydantic validation failure, and a hallucinated tool name.
3. **25 mutations applied to the source, each run against the test that claims to catch it, then restored.** Every one turned red. Details below.

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC-1 | After a run completes, its full step sequence is queryable from `run_events` | ✓ VERIFIED | Every one of the 15 `yield` sites in `agent.py` is `yield await _persisted(...)`, except the `tool_result` yield which is persisted either inside the tool's own transaction (`execute_and_record`) or by `_persisted` — never neither, never twice. Verified by grep, not by claim. Rows join `runs` via `run_uid` (`main.py:281,357`; `telemetry.record_run(run_uid=...)`). All five early-return paths (`api_connection_error`, `api_error`, `model_refusal`, `budget_exceeded`, `step_limit_reached`) and the guardrail-denial path each have a test asserting the **whole** `(seq, type)` sequence. Mutations M2, M3, M4, M22, M24 all red. |
| SC-2 | An open dashboard tab shows runs appearing in real time over `/events` with no polling | ⚠️ PARTIAL — transport verified, browser half deferred | **Transport: verified.** `test_events_delivers_a_live_run` stamps each chunk with whether the run had already finished; at least one frame must arrive before it did. I mutated the publish into a 1s-delayed batch (M20) and the test went red — the liveness claim is falsifiable, not decorative. **Browser: absent.** `DASHBOARD_HTML` (`main.py:474-510`) contains no `EventSource`; it still runs `refresh(); setInterval(refresh, 5000)` against `/metrics`. No dashboard consumer of `/events` exists. See "Deferred Items". |
| SC-3 | The public feed contains no ticket bodies, customer data, or API keys — only redacted projections | ✓ VERIFIED (independently) | My own 9-sentinel probe: all nine confirmed present in the run's own owner-facing SSE stream (vacuity guard), **zero** present in any of the 23 published frames. `project()` is an allowlist with no spread anywhere; `attribute_to_run` adds two fields to an already-projected frame; `snapshot_frame` is a two-field allowlist over `ActiveRun`. There is exactly **one** `broker.publish` call site in `src/` and exactly one `project()` call site, both in `event_stream`. `/events` yields only those two serialisers' output plus SSE comments — pinned by tag-tracking, and mutation M21 (a second `event: raw` re-emission of the same frame) reds it. Mutations M1, M16, M17 red. |
| SC-4 | A slow or abandoned tab never stalls or delays a paid run, and streams are capped so the machine can still scale to zero | ✓ VERIFIED (with a residual, see WARNING-3) | `publish` is a plain `def` (asserted on the function object), bounded, drop-oldest, and cannot raise — including the double-failure path, which is exercised with a hostile queue and is now counted rather than swallowed. The heartbeat **never** touches `idle_deadline`; I added that reset (M5) and the heartbeat-only stream became non-terminating and the test failed on its read deadline. Viewers never enter `RunRegistry` — registering one (M9) makes the drain wait out its timeout and the test reds. Subscriber cap (M6), per-IP connect meter (M15), in-stream refusal on a lost race, and `lifespan broker.close()` all verified. |
| DATA-03 | A `run_events` table persists per-run step events written during the stream, enabling per-run drill-down | ✓ SATISFIED | Table + `idx_run_events_run_uid`; raw full-fidelity payload (drill-down has something to drill into); `seq` monotonic per run and now in **causal** order on the denial path; D-04 atomicity holds under a forced mid-transaction failure (the reply, the ticket status change, and the event row all roll back together); guarded idempotent `ALTER TABLE runs ADD run_uid`; 30-day retention sweep wired into `lifespan`, not merely available. |
| DASH-01 | The dashboard receives a live run feed over a public, projection-only SSE `/events` endpoint (no polling; no sensitive data) | ⚠️ PARTIALLY SATISFIED | The endpoint clause is fully met: public, keyless, projection-only, metered, capped, heartbeated, idle-closing, snapshot-on-connect. The **"the dashboard receives"** clause is not met — nothing in the browser consumes it and the dashboard still polls. Deferred to Phase 6 by the roadmap's own UI hint, but see WARNING-4 for the traceability hole. |

**Score:** 4/6 verified · 2 partial-and-deferred · 0 failed

---

### Deferred Items

| # | Item | Addressed In | Evidence |
|---|------|--------------|----------|
| 1 | SC-2's browser half — a dashboard tab that renders the live feed | Phase 6 | ROADMAP Phase 5 **UI hint**: *"scoped to the thinnest `/events` smoke that proves SC-2; the designed dashboard is Phase 6"*. Phase 6 SC-4: *"A visitor can submit a prefilled example ticket from the page with the demo key and watch that run stream live."* 05-CONTEXT's Phase Boundary says the same. |
| 2 | DASH-01's "the dashboard receives ... no polling" clause | Phase 6 (substantively) | Same evidence. **Caveat:** Phase 6's `Requirements:` line is `DASH-02, DASH-03, DASH-04, DASH-05` — DASH-01 is not listed, so no phase formally owns this clause once Phase 5 closes (WARNING-4). |

Deferred items do not count as gaps and do not change the status.

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/relay/events.py` (456 ln) | Broker + `project()` + `snapshot_frame()` + `attribute_to_run()` + `RunRecorder` | ✓ VERIFIED | All five present, substantive, wired, and data flows through all of them in a real run. |
| `src/relay/db.py` | `run_events` DDL, index, guarded `run_uid` ALTER, `purge_expired_run_events` | ✓ VERIFIED | Column set asserted exactly; migration idempotent under re-run (M22 reds without the PRAGMA guard); sweep deletes at 31 days and keeps at 2, and leaves `runs` untouched. |
| `src/relay/agent.py` | Optional `recorder`; `_persisted` at every yield; write-tool atomic seam | ✓ VERIFIED | Optional-collaborator contract pinned behaviourally by copied `evals.py`/`mcp_server.py` call shapes (M18b reds it). `evals.py` and `mcp_server.py` are genuinely untouched — a recorder-less run writes **zero** `run_events` rows. |
| `src/relay/main.py` | `event_stream` persist+publish, public `GET /events`, lifespan broker + sweep | ✓ VERIFIED | Single publish site, post-commit by construction, identity-stamped, gated, capped. |
| `src/relay/telemetry.py` | `record_run(run_uid=...)`, named public columns | ✓ VERIFIED | `_PUBLIC_RUN_COLUMNS` replaces `SELECT *`; exact `/metrics` key set pinned (M8 reds it). |
| `tests/test_run_events.py` (1979 ln) | The 10 mapped rows + the review's closure tests | ✓ VERIFIED | Not a stub in any sense — see the falsifiability audit. |
| `DASHBOARD_HTML` consumer of `/events` | A minimal live-feed consumer (05-CONTEXT: *"Enough of a minimal dashboard consumer to prove SC-2 is in scope"*) | ✗ ABSENT | No `EventSource`; still `setInterval(refresh, 5000)`. Superseded by the ROADMAP UI hint, which re-scoped the SC-2 proof to the `/events` smoke test. Deferred, not counted as a gap. |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `agent.run_ticket` | `run_events` | `_persisted` → `to_thread(recorder.record)` | ✓ WIRED | Awaited, so publish can never lead the write (M12 — swapping the await for a `create_task` reds `test_broker_never_leads_the_database`). |
| write tool | `run_events` | `RunRecorder.execute_and_record` inside one `transaction()` | ✓ WIRED | Forced mid-transaction failure rolls back the reply **and** the ticket status (M2). |
| guardrail denial | `run_events` | deferred row, written after the `guardrail` event | ✓ WIRED | Cause precedes effect in the durable record and in the feed (M3). |
| `event_stream` | broker | `attribute_to_run(project(event), ...)` | ✓ WIRED | Two genuinely interleaved concurrent runs stay 1:1 uid↔ticket (M10 reds it). |
| broker | `/events` client | bounded queue → `stream()` | ✓ WIRED | Live, mid-run (M20). |
| `RunRegistry` | `/events` connect frame | `snapshot_frame(app.state.runs.snapshot())` | ✓ WIRED | Ordering is a fact, not a scheduling accident (M14). |
| `lifespan` | retention | `to_thread(purge_expired_run_events)` | ✓ WIRED | Driven through the real TestClient lifespan against a pre-seeded DB (M7). |
| `lifespan` | open streams | `broker.close()` sentinel | ✓ WIRED | Stream ends on the sentinel and the sentinel is never serialised out. |
| `runs.run_uid` | `run_events.run_uid` | soft join key | ✓ WIRED | Re-read from a **reopened** connection, so an uncommitted write cannot pass. |

---

### Data-Flow Trace (Level 4)

| Artifact | Data | Source | Real data? | Status |
|----------|------|--------|-----------|--------|
| `/events` frames | projected run events | live `RunEventBroker` fed by a real run | Yes — 23 frames from my probe carry real tool names, real costs, real outcomes | ✓ FLOWING |
| `/events` connect frame | in-flight runs | `RunRegistry.snapshot()` | Yes — a registered run appears with elapsed ms | ✓ FLOWING |
| `run_events` rows | raw event payloads | `RunRecorder` inside the run | Yes — all nine of my sentinels are present in the raw rows (D-01 full fidelity confirmed) | ✓ FLOWING |
| `/metrics` `last_runs` | `runs` rows | named columns | Yes, minus `run_uid` by design | ✓ FLOWING |
| Dashboard HTML | run feed | — | **No source** — the page never opens `/events` | ✗ DISCONNECTED (deferred to Phase 6) |

---

### Falsifiability Audit — 25 mutations applied, run, and restored

This project's stated recurring failure mode is tests that pass while proving nothing. Every mutation below was written into the source, run against the single test that claims to catch it, and reverted with `git checkout`.

| # | Mutation | Test | Result |
|---|----------|------|--------|
| M1 | spread `**d` into `project()`'s `tool_use` frame | `test_no_projection_leaks_sensitive_data` | RED |
| M2 | move `_insert_event` out of the tool's transaction | `test_send_reply_and_its_event_row_commit_atomically` | RED |
| M3 | delete the guardrail-denial early return | `test_a_denied_write_tool_persists_cause_before_effect` | RED |
| M4 | drop `_persisted` from the `budget_exceeded` yield | `test_budget_exceeded_run_persists_its_terminating_row` | RED |
| M5 | let the heartbeat renew `idle_deadline` | `test_events_heartbeats_then_idle_closes` | RED |
| M6 | delete the subscriber-cap refusal | `test_events_refuses_a_viewer_past_the_subscriber_cap` | RED |
| M7 | remove the retention sweep from lifespan | `test_retention_sweep_runs_at_startup` | RED |
| M8 | restore `SELECT * FROM runs` | `test_metrics_does_not_publish_run_uid` | RED |
| M9 | register the `/events` viewer as a run | `test_events_viewer_is_not_a_registered_run` | RED |
| M10 | publish an unattributed frame | `test_concurrent_runs_are_attributable_in_the_feed` | RED |
| M11 | stop counting dropped frames | `test_dropped_frames_are_counted_and_logged` | RED |
| M12 | publish without awaiting the write | `test_broker_never_leads_the_database` | RED |
| M13 | subscribe in the handler, not the generator body | `test_events_disconnect_unsubscribes` | RED |
| M14 | delete the connect-snapshot yield | `test_events_sends_initial_snapshot_on_connect` | RED |
| M15 | drop `events_gate` from the route | `test_events_connects_are_rate_limited_per_ip` | RED |
| M16 | forward `notice.results` unconstrained | `test_project_notice_publishes_a_count_and_never_the_results` | RED |
| M17 | dispatch on tool name before the error branch | `test_the_feed_distinguishes_a_denied_write_from_a_successful_one` | RED |
| M18b | make `recorder` a required collaborator | `test_the_frozen_callers_call_shapes_still_work_with_no_recorder` | RED |
| M19 | widen the retention window to never delete | `test_retention_sweep_deletes_old_run_events_and_keeps_recent_ones` | RED |
| M20 | batch the feed (publish 1s late) | `test_events_delivers_a_live_run` | RED |
| M21 | add a third serialisation path to `/events` | `test_events_output_comes_only_from_two_serialisers` | RED |
| M22 | unguarded `ALTER TABLE` | `test_run_uid_migration_is_idempotent` | RED |
| M23 | bare `put_nowait` (publish can block/raise) | `test_publish_drops_oldest_and_never_blocks` | RED |
| M24 | drop `_persisted` from the `usage` yield | `test_a_run_persists_its_full_event_sequence` | RED |

**25/25 red.** No vacuous test found in this phase. The review's WR-06 (`test_recorder_untouched_files`, which could not fail) is genuinely gone — `grep -rn "git diff" tests/` returns nothing — and its replacement asserts the real contract behaviourally.

---

### 05-VALIDATION.md Per-Requirement Map — every row checked

| Row | Named test | Exists | Passes | Non-vacuous? |
|-----|-----------|--------|--------|--------------|
| DATA-03/SC-1 full sequence | `test_a_run_persists_its_full_event_sequence` | ✓ | ✓ | ✓ M24. Asserts the whole type list **and** the `seq` range against the stream, re-read from a reopened connection. |
| DATA-03/SC-1 atomicity (load-bearing) | `test_send_reply_and_its_event_row_commit_atomically` | ✓ | ✓ | ✓ M2. Forces a real mid-transaction raise and asserts `replies == 0` **and** `tickets.status == 'open'` — the rollback, not co-presence. |
| DATA-03 migration | `test_run_uid_migration_is_idempotent` | ✓ | ✓ | ✓ M22. Runs `init_db` twice on a **populated** DB and checks legacy `run_uid IS NULL`. |
| DASH-01/SC-2 `/events` smoke | `test_events_delivers_a_live_run` | ✓ | ✓ | ✓ M20. Asserts *live*, not merely *delivered*. Also asserts safe fields present, so an allowlist that leaks nothing by publishing nothing fails. |
| DASH-01/SC-3 redaction (load-bearing) | `test_no_projection_leaks_sensitive_data` | ✓ | ✓ | ✓ M1, plus my independent 9-vector probe. Proves presence in the run twice (SSE stream + raw rows) before asserting absence; iterates every frame × every sentinel. |
| DASH-01/SC-4 fire-and-forget | `test_publish_drops_oldest_and_never_blocks` | ✓ | ✓ | ✓ M23. Asserts the **oldest** goes, `publish` returns `None`, and a hostile double-failing subscriber still cannot raise. |
| DASH-01 no leaked subscriber | `test_events_disconnect_unsubscribes` | ✓ | ✓ | ✓ M13. Covers both the never-started and the mid-stream-disconnect halves. |
| D-06 publish-after-commit | `test_broker_never_leads_the_database` | ✓ | ✓ | ✓ M12. Samples `COUNT(*)` inside `publish`; asserts `committed >= k` **and** `committed[-1] == len(...)`. |
| D-09/SC-4 heartbeat + idle close | `test_events_heartbeats_then_idle_closes` | ✓ | ✓ | ✓ M5. The heartbeat-**only** case is the one asserted, which is the case that matters. (Timing margin is deferred as WR-12.) |
| SC-4 viewer is not a run | `test_events_viewer_is_not_a_registered_run` | ✓ | ✓ | ✓ M9. Asserts the *consequence* (`drain` returns True inside 0.05s), not just `active == 0`, and that `close()` ends the stream. |

**10/10 rows: real, passing, non-vacuous.** All ten also pass when run as an isolated node-id selection (no inter-test ordering dependency).

---

### LOCKED Decisions D-01..D-14

| D | Verdict | Evidence |
|---|---------|----------|
| D-01 two decoupled paths | ✓ honoured (one tension — WARNING-2) | DB raw + source of truth; broker lossy mirror; public projection allowlisted. |
| D-02 in-process broker | ✓ | `RunEventBroker` on `app.state`, built in lifespan (not at import). |
| D-03 no history replay | ✓ | Only the connect snapshot is sent; no backfill. |
| D-04 event row in the tool's transaction | ✓ | Proven by forced rollback. |
| D-05 `to_thread` seam | ✓ | Both `record` and `execute_and_record` are sync and offloaded. |
| D-06 publish only after commit | ✓ | Structural (persist happens before the yield), not an ordering convention. |
| D-07 allowlist, not denylist | ✓ | Every branch names its fields; unknown type → `None`; unknown tool → name + flag. |
| D-08 mutation-checked leak test | ✓ | M1, plus my independent probe. |
| D-09 heartbeat + idle close | ✓ | Verified; heartbeat cannot renew the deadline. |
| D-10 bounded drop-oldest, no backpressure | ✓ + improved | Now also counted and rate-limit-logged (WR-09). |
| D-11 `/events` public, projection-only | ✓ knowingly amended | Still keyless; CR-01 added an anon per-IP meter + subscriber cap. The amendment tightens D-11 without contradicting it. |
| D-12 viewers not in `RunRegistry` | ✓ | Drain unaffected; asserted by consequence. |
| D-13 guarded idempotent `ALTER TABLE` | ✓ (WR-08 race deferred) | Sequential re-run proven; concurrent boot is the deferred hole. |
| D-14 connect snapshot | ✓ | First frame, allowlisted, elapsed-ms not raw monotonic. |

---

### Review Closure — the 3 CRITICALs and 8 WARNINGs, verified in code (not taken on trust)

| ID | Claimed fixed | Verified in code | Mutation |
|----|---------------|------------------|----------|
| CR-01 | ✓ | `events_gate` dependency + `at_capacity` 503 + in-stream refusal on the lost race + `BrokerUnavailable` | M6, M15 |
| CR-02 | ✓ | `execute_and_record` returns `recorded=False` on `denied_by`; caller writes the row after the `guardrail` event | M3 |
| CR-03 | ✓ | `attribute_to_run` stamps identity **last**; two-run interleaving test asserts the 1:1 mapping, not mere presence | M10 |
| WR-01 | ✓ | error branch is first, before tool dispatch; `is_error` coerced to bool on every branch | M17 |
| WR-02 | ✓ | `result_count`, coerced; non-int dropped | M16 |
| WR-04 | ✓ | `snapshot_frame` now lives in `events.py`; the "no third path" test tracks tags **and** re-checks the event name and key set | M21 |
| WR-05 | ✓ | `purge_expired_run_events` + wired into lifespan + `runs` explicitly spared | M7, M19 |
| WR-06 | ✓ | The `git diff` test is gone (`grep -rn "git diff" tests/` → nothing); replaced by the real optional-collaborator contract | M18b |
| WR-07 | ✓ | Five terminal-path tests + the denial path; each asserts the whole sequence, not just the last row | M4 |
| WR-09 | ✓ | `dropped` counter, structured `events.frame_dropped` log, rate-limited, and the double-failure branch counts too | M11 |
| WR-10 | ✓ | `_PUBLIC_RUN_COLUMNS`; exact key-set assertion pins the next column too | M8 |

**Deliberately deferred (user decision, recorded in 05-DEFERRED.md — NOT counted as verification failures here):** WR-03, WR-08, WR-11, WR-12.

---

## Findings Requiring a Decision

None are BLOCKERs. All four are distinct from the deferred WR-03/08/11/12.

### WARNING-1 — the phase made two opposite disclosure decisions about `run_uid`, and a docstring now states a falsehood

`attribute_to_run` (`events.py:330-333`) justifies stamping `run_uid` on every public frame with:

> *"Neither field is a new disclosure: ... and `run_uid` by the same /metrics rows."*

That was true when CR-03 landed (`64cfeca`) and became **false** three commits later when WR-10 (`c01c372`) removed `run_uid` from `/metrics` — on the explicit reasoning that it is *"the key into `run_events`, a table deliberately filled with unredacted customer data, whose access model phase 6 has not decided yet."* The suite now contains `test_metrics_does_not_publish_run_uid` asserting the uid is **not** public, alongside `test_concurrent_runs_are_attributable_in_the_feed` asserting it **is** public on every `/events` frame.

**Risk:** none today — the value is a random hex and no endpoint accepts it. It becomes real the moment Phase 6 builds a `run_uid` → `run_events` drill-down: the key is already broadcast to any anonymous listener, so that endpoint must be authenticated or itself redacted. **Recommendation:** pick one posture, fix the docstring either way, and record it as a Phase 6 input.

### WARNING-2 — a single failed `run_events` INSERT now kills the paid run mid-stream, and nothing tests it

`_persisted` awaits `recorder.record`; a `sqlite3.OperationalError` propagates out of `run_ticket` and out of the SSE generator. I confirmed this empirically: injecting a transient *"database is locked"* on the second event produced

```
CHUNKS: ['event: usage']
RAISED OUT OF THE SSE STREAM: OperationalError: database is locked
runs rows written: 1        # outcome "incomplete"
```

The client gets a truncated stream with **no `event: error`** explaining it; the money already spent with Anthropic is lost. Before this phase, a DB hiccup could not abort a run in progress. `busy_timeout` is 5s and `CLAUDE.md`/`ARCHITECTURE.md` document the MCP server running against the same file, so a cross-process lock is a real path to this.

Fail-closed is defensible (D-01 makes the DB the source of truth), but it is currently implicit, untested, and produces a worse client experience than the codebase's own error-handling convention ("never allowed to raise a stack trace to the caller"). **Recommendation:** make it an explicit decision and cover it — either catch-and-log-and-continue, or keep fail-closed but yield a structured `error` event first.

### WARNING-3 — nothing bounds total connection-holding time, so SC-4's cost guarantee is defended against accidents, not adversaries

The three bounds shipped (30 connects/min/IP, 50 concurrent subscribers, 300s idle ceiling) all hold. But a single IP can keep up to 50 streams open indefinitely by reconnecting inside each 300s ceiling, well within the connect meter — which keeps the Fly machine at `started` and defeats `min_machines_running=0`, the milestone's own budget constraint. CR-01's proposed `[http_service.concurrency]` block was **not** added to `fly.toml` (confirmed: no `concurrency` key present).

SC-4 as written ("streams are capped so the machine can still scale to zero") **is** met, and no app-level control can fully close this on a public streaming endpoint. Flagged so the residual is a decision rather than a surprise on the bill. Routed to human verification.

### WARNING-4 — DASH-01 has no owning phase after this one

`REQUIREMENTS.md` maps DASH-01 → Phase 5 only. Phase 5 delivers the endpoint but not the dashboard-side consumption that DASH-01's own wording requires. Phase 6's `Requirements:` line is `DASH-02..05`. Phase 6 SC-4 covers the behaviour substantively, but the requirement ID is orphaned. **Recommendation:** add DASH-01 to Phase 6's requirements (or split it), and do **not** flip DASH-01 to Complete on Phase 5 alone.

### INFO-1 — the tool name is the only unbounded model-controlled string on the public feed

Confirmed empirically: a `tool_use` block named `PROBE_HALLUCINATED_TOOL_NAME` was published verbatim as `{"type":"tool_use","tool":"PROBE_HALLUCINATED_TOOL_NAME"}` and again on its `tool_result`. Every other field `project()` publishes is a number, an enum, a literal, or a KB filename. `json.dumps` escaping means this is not a frame-splitting vector (IN-03 stays closed), and the Anthropic API constrains tool names in practice — but under prompt injection this is the one channel through which model-chosen text reaches an unauthenticated endpoint. A one-line clamp (`tool if tool in registry else "unknown"`, or a length/charset limit) would close the class.

### INFO-2 — smaller notes

- The retention sweep runs **at startup only**. On a machine that stays warm under sustained traffic, raw payloads can outlive the 30-day window. Deliberate and documented (scale-to-zero makes a boot the reliable moment); worth revisiting if Phase 6 traffic keeps the machine up.
- If `record_run` fails in `event_stream`'s finally (caught and logged), the run's `run_events` rows survive with no `runs` row to join from — invisible to a Phase 6 drill-down that starts from `runs`, and retained until the sweep.
- The five early-return tests drive `run_ticket` directly, so they verify the `run_events` rows but not the `runs` join on those paths. The join is structurally guaranteed by `event_stream`'s finally and is covered on the happy and denial paths.
- No debt markers (`TODO`/`FIXME`/`TBD`/`XXX`/`HACK`/`PLACEHOLDER`) anywhere in `src/` or `tests/`.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/relay/events.py` | 330-333 | Docstring states a fact that a later commit falsified | ⚠️ Warning | WARNING-1 |
| `src/relay/main.py` | 474-510 | Dashboard still polls `/metrics` every 5s; no `/events` consumer | ℹ️ Info | Deferred to Phase 6 |
| `src/relay/agent.py` | 243-245 | Unguarded persistence failure aborts a paid run | ⚠️ Warning | WARNING-2 |
| — | — | Stubs, empty handlers, hardcoded empty returns, debt markers | ✓ None found | — |

---

### Behavioural Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full suite | `.venv/bin/python -m pytest -q` | `338 passed` | ✓ PASS |
| Lint | `.venv/bin/ruff check src tests` | `All checks passed!` | ✓ PASS |
| All 10 mapped tests, isolated | `pytest <10 node ids>` | `10 passed` | ✓ PASS |
| Independent 9-sentinel SC-3 probe | custom probe under `tests/` | 9/9 present in the run, 0/9 in 23 published frames | ✓ PASS |
| Recorder-failure propagation | custom probe | Raises out of the SSE stream (see WARNING-2) | ⚠️ Behaviour confirmed |
| Publish/project call-site uniqueness | `grep -rn "\.publish(\|project(" src/` | exactly one of each in `event_stream` | ✓ PASS |
| Frozen-file `git diff` test removed | `grep -rn "git diff" tests/` | no matches | ✓ PASS |
| Working tree after 25 mutations | `git status --short` | clean | ✓ PASS |

---

### Requirements Coverage

| Requirement | Source Plan | Status | Evidence |
|-------------|-------------|--------|----------|
| DATA-03 | 05-01, 05-02, 05-03 | ✓ **SATISFIED — safe to mark complete** | Table, index, migration, atomicity, causal ordering, every terminal path, retention, join key. |
| DASH-01 | 05-02, 05-03, 05-04 | ⚠️ **PARTIALLY SATISFIED — do NOT mark complete on Phase 5 alone** | Endpoint clause fully met and hardened. "The dashboard receives ... no polling" not met; deferred to Phase 6, which does not currently list the requirement (WARNING-4). |

---

## Verdict

**DATA-03: complete.** Durability, ordering, atomicity, migration safety, and retention are all real, wired, and falsifiable.

**DASH-01: complete for the endpoint, not for the dashboard.** The public projection-only SSE feed exists and is the strongest-built surface in the codebase; the browser that DASH-01 names does not consume it yet.

**Phase goal — "Every agent step is durably recorded and visitors watch runs happen live":** the first half is achieved and provable; the second half is achieved at the wire and deferred at the glass, by the roadmap's own scoping.

The `/events` redaction boundary survived an independent probe wider than the one shipped with the phase, and 25 mutations found no test in this phase that passes while proving nothing. Given this project's nine-and-counting history of unfalsifiable checks, that is the finding I most want on the record.

---

_Verified: 2026-08-12_
_Verifier: Claude (gsd-verifier), goal-backward, adversarial stance_
_Baseline at start and finish: 338 passed, ruff clean, working tree clean_

---

## Manual verifications closed in production (2026-08-15)

Both rows carried from `05-VALIDATION.md` § Manual-Only Verifications, unverifiable in-suite
by construction. Deployed at `relay-agent.fly.dev`, image built from `main` @ `07bf209`.

| Behavior | Requirement | Result |
|---|---|---|
| `run_uid` present on the live volume after deploy | DATA-03 / D-13 | **Confirmed.** The guarded `ALTER` ran against the pre-existing week-old volume. The legacy run (`runs.id=1`, 2026-08-03) survived with `run_uid = NULL` and reads as legacy rather than crashing — the exact case the fail-closed handling was written for. `/metrics` `last_runs` now carries `run_uid`. |
| An open `/events` tab does not hold the Fly machine awake | SC-4 / D-09 | **Confirmed by the user.** Dashboard left open and idle past the ceiling; `fly status` subsequently reported `stopped`. The heartbeat does not reset its own idle deadline in production, so `min_machines_running = 0` still reaches scale-to-zero with a live viewer attached. |

This closes every verification item in the milestone. D-09 was the one property whose failure
mode was silent and expensive — a forgotten tab quietly holding a paid machine awake — and it
is the reason the idle-deadline mutation was treated as load-bearing during execution.

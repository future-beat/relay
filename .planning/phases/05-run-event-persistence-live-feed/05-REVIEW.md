---
phase: 05-run-event-persistence-live-feed
reviewed: 2026-08-12T00:00:00Z
depth: deep
files_reviewed: 8
files_reviewed_list:
  - src/relay/events.py
  - src/relay/agent.py
  - src/relay/main.py
  - src/relay/db.py
  - src/relay/config.py
  - src/relay/telemetry.py
  - tests/test_run_events.py
  - tests/conftest.py
findings:
  critical: 3
  warning: 12
  info: 4
  total: 19
status: issues_found
---

# Phase 5: Code Review Report

**Reviewed:** 2026-08-12
**Depth:** deep (cross-file: publish paths, transaction nesting across the `to_thread` seam, migration, test falsifiability)
**Files Reviewed:** 8
**Status:** issues_found
**Baseline at review start and finish:** `315 passed`, `ruff check src tests` clean, working tree clean. Only this file was added.

## Summary

The redaction boundary itself is the strongest part of this phase and it holds up under adversarial tracing — see the explicit verdict below. The defects are elsewhere: the new public endpoint has no perimeter at all on a service whose headline constraint is "cheap to keep running"; the durable record the next phase is built on is provably mis-ordered on the guardrail-denial path with no way to recover the true order; and the published frames carry no run identity, so the feed conflates concurrent runs. One shipped test (`test_recorder_untouched_files`) is structurally incapable of failing for the thing it names.

### Verdict 1 — is `project()` closed?

**Yes, against every event type `run_ticket` yields today, and yes against a future one.** Traced all nine yield sites in `agent.py` (`error` ×4 reasons, `usage`, `text`, `tool_use`, `guardrail` ×2 guards, `notice`, `tool_result`, `resolution`) against `events.py:157-201`. Every branch names its fields; there is no `{**d}` anywhere; `text` publishes the type only; `tool_use` publishes the name only; the `guardrail` branch drops `missing_citations`/`retrieved_ids`/`supplied_ticket_id`; `_project_tool_result`'s `lookup_customer` lands on the fall-through and loses the customer row wholesale. Unknown event type → `None` (drop). Unknown tool → `{type, tool, is_error}`. Non-dict result → `{type, tool, is_error}`. I drove a real run with a ticket-binding denial through `project()` and confirmed no ticket body, email, reply text or tool argument reaches a frame.

Two caveats, both filed below: `project()` has exactly one forward of unconstrained shape (`notice.results`, WR-02), and the `error` branch republishes an upstream-controlled value (`d.get("type")` is the Anthropic error body's `error.type`) to an unauthenticated endpoint with no test (WR-07).

**Second serialisation path: yes, one exists** — `_snapshot_frame()` (`main.py:331-352`), D-14. It is a correct two-field allowlist and its test pins the exact key set, so it is not a leak today. It is filed as WR-04 because it lives in a different module from the boundary it belongs to, and `events.py`'s own docstring claims `project()` is "the only path".

### Verdict 2 — the disclosed seq inversion

**Independently confirmed, and the disclosed mitigation (a code comment) is not adequate.** Driving a `send_reply` with a mismatched `ticket_id`:

```
SSE order:        usage, tool_use, guardrail, tool_result, usage, text, error
run_events order: (1,usage) (2,tool_use) (3,tool_result) (4,guardrail) (5,usage) (6,text) (7,error)
```

Classified BLOCKER (CR-02) — reasoning in the finding.

---

## Critical Issues

### CR-01: `/events` is the first public endpoint with no perimeter, no subscriber cap, and an O(subscribers) publish on the run's event loop

**File:** `src/relay/main.py:355-411`, `src/relay/events.py:55-99`, `src/relay/config.py:114-122`, `fly.toml`
**Severity:** BLOCKER (cost-DoS / availability; defeats the phase's own SC-4)

**Issue:** `events()` takes no `Request` and carries no `dependencies=`. It is the only route in the application that never reaches `enforce("auth", "anon", request)` — `_gate` (`main.py:84-131`) meters even before the credential is resolved, precisely so an anonymous caller cannot get free work; `/events` skips that entirely. `fly.toml`'s `[http_service]` block sets no `concurrency` hard_limit, so nothing upstream caps connections either.

Concrete failure scenario, no credential required:

```
while :; do curl -sN https://relay-agent.fly.dev/events >/dev/null & done
```

1. **Scale-to-zero is defeated, which is the milestone's budget constraint.** Each connection subscribes (`main.py:372`) and is held for up to `events_idle_seconds = 300` (`config.py:122`). Fly's autostop keys on active inbound connections. A reconnect loop keeps the machine at `started` indefinitely — the exact guarantee D-09 exists to provide, inverted by the endpoint D-09 was written for. The manual verification row in `05-VALIDATION.md` ("leave it idle past the ceiling, `fly machine list` shows `stopped`") tests the *forgotten tab* case, not the *adversarial* one.
2. **The paid run's event loop stalls.** `RunEventBroker.publish` (`events.py:89-99`) is synchronous and iterates every subscriber; it is called from `event_stream` on the loop (`main.py:269`) once per agent event. D-10's guarantee is per-subscriber ("a stalled watcher backpressures nothing") but not per-*count*: the cost is `O(N)` with N attacker-chosen, on the same loop that answers the container `HEALTHCHECK`. `main.py:113-118` documents that a stalled loop failing `/health` inside 3s gets the machine restarted, killing every in-flight run.
3. **Memory.** N × up-to-256 dict frames on a 512MB `shared-cpu-1x` VM, with no cap in `subscribe()`.

**Fix:**
```python
# main.py — meter it like every other public-facing surface
@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    await enforce("events", "anon", request)   # new bucket in config.py, e.g. "30/minute"
    ...

# events.py — refuse past a ceiling rather than growing without bound
def subscribe(self) -> asyncio.Queue:
    if self.closed or len(self._subs) >= settings.events_max_subscribers:
        raise BrokerUnavailable("too many live viewers")
    ...
```
plus `[http_service.concurrency] type="connections"; hard_limit=<n>; soft_limit=<n>` in `fly.toml`, and a `503` handler on `BrokerUnavailable`. The bucket must be metered *before* the generator starts, for the reason `_gate` already documents: a `StreamingResponse` locks its status at 200 on first yield.

---

### CR-02: `run_events` records effect before cause on the guardrail-denial path, and the true order is unrecoverable

**File:** `src/relay/agent.py:366-372, 445-465, 490-505`, `src/relay/events.py:204-217, 252-276`
**Severity:** BLOCKER (durable audit record is wrong; the consumer is Phase 6)

**Issue:** Confirmed by driving a real run. For a write-tier tool that is denied (`ticket_binding` or `citation`), `recorder.execute_and_record` runs in the offload worker and inserts the `tool_result` row (`seq=3`) *before* returning; the `guardrail` event that explains the denial is only recorded afterwards at its yield site (`seq=4`). The SSE stream shows the opposite, deliberate order (`guardrail`, then `tool_result`).

This is not merely cosmetic:

- `RunRecorder`'s own docstring (`events.py:212-217`) states the counter is "what makes phase 6's drill-down renderable in order without trusting `created_at` to break ties". On this path that claim is false.
- **The true order cannot be recovered from the table.** `created_at` is `datetime('now')` (`db.py:78`) — second resolution — so both rows carry the same timestamp. `seq` is the declared tiebreaker and it is inverted. There is no third signal.
- The path where it happens is the *security guardrail* path. An audit record that shows a `send_reply` result before the denial that produced it is the wrong record of a safety control firing — the one place ordering carries meaning.
- Nothing pins it. No test in `test_run_events.py` scripts a denial at all (see WR-07), so a Phase 6 renderer that assumes the SSE order will be silently wrong.

The disclosure's mitigation is a comment at `agent.py:451-455`. A comment is not a pin and does not make the record correct.

**Fix — pick one, but not "document it":**
```python
# events.py: let the caller reserve the slot the guardrail row will occupy, so the
# denial keeps the lower seq even though it is recorded second.
def reserve_seq(self) -> int:
    self._seq += 1
    return self._seq

def record_at(self, event: AgentEvent, seq: int) -> None: ...
```
or, simpler: emit the `guardrail` row from *inside* `execute_and_record` when `is_error and payload.get("denied_by")`, before the `tool_result` row, so both land in the same transaction in causal order. If the team accepts the current order instead, it must be pinned by a test that scripts a denial and asserts the exact `(seq, type)` list, and stated as a contract in `05-CONTEXT.md` for Phase 6.

---

### CR-03: published frames carry no run or ticket identity, so concurrent runs are conflated in the public feed

**File:** `src/relay/events.py:157-201` (every branch), `src/relay/main.py:267-269`
**Severity:** BLOCKER (DASH-01 / SC-2 — the feed is incorrect for the multi-run case it will meet in production)

**Issue:** No frame `project()` produces carries `run_uid` or `ticket_id`. The service admits concurrent runs by design: `RunRegistry` holds a *dict* of active runs, `owner_process_limit` is `60/hour`, and `_snapshot_frame()` (`main.py:348-351`) renders a *list* of them. With two runs in flight, a subscriber receives interleaved `tool_use` / `tool_result` / `usage` / `resolution` frames with no attribution:

```
{"type":"tool_use","tool":"search_docs"}      <- run A
{"type":"tool_use","tool":"lookup_customer"}  <- run B
{"type":"usage","steps":2,"cost_usd":0.021}   <- which run's cost?
{"type":"resolution","via":"send_reply",...}  <- which ticket resolved?
```

Phase 6's per-run cards and the cost figures on them are unbuildable from this, and the frames cannot be joined to the durable `run_events` rows this phase exists to write. `test_events_delivers_a_live_run` (`test_run_events.py:700`) drives exactly one run, so the phase's own SC-2 proof is scoped to hide the defect.

This is not a redaction decision: `_snapshot_frame` already publishes `ticket_id` on the same endpoint (`main.py:349`), and `/metrics` publishes `last_runs[].ticket_id` publicly with no gate. The omission looks like an oversight, not a D-07 call — and adding a field to a locked allowlist after the phase closes is exactly the deliberate decision this phase was supposed to make.

**Fix:** thread the run's identity into the projection and make it an explicit D-07 line item.
```python
# main.py event_stream — the run already holds run_uid
frame = project(event, run_uid=run_uid, ticket_id=ticket.id)

# events.py — added to the envelope, not to any per-type branch
def project(event: AgentEvent, *, run_uid: str, ticket_id: int) -> dict | None:
    frame = _project_body(event)
    return None if frame is None else {"run_uid": run_uid, "ticket_id": ticket_id, **frame}
```
and extend `test_events_delivers_a_live_run` (or add a sibling) to drive two runs concurrently and assert every frame is attributable.

---

## Warnings

### WR-01: `is_error` is dropped by four of five `_project_tool_result` branches — denied and failed actions publish as null-shaped successes

**File:** `src/relay/events.py:126-151`
**Issue:** The non-dict branch (`:125`) and the fall-through (`:154`) both keep `is_error`. The four named-tool branches do not. A guardrail-denied `send_reply` returns `{"error": ..., "denied_by": "ticket_binding", ...}` — which *is* a dict — so it takes the `send_reply` branch and publishes, verbatim from a real run:

```json
{"type": "tool_result", "tool": "send_reply", "reply_id": null, "status": null}
```

No error signal at all. Same for `create_escalation` (`{escalation_id: null, status: null}`) and `set_category` (`{category: null}`). Worse for `search_docs`: an errored search returns `{"error": ...}`, the branch reads `result.get("results")` → `None` → not a list → the frame becomes `{"results": []}`, making "the tool blew up" byte-identical to "the KB has nothing about this" — the latter being the signal `retrieval.py` documents as what makes the model escalate.

A public feed advertised as showing "a credible, safe, observably-real AI agent service" cannot distinguish a guardrail firing from a rendering blank.

**Fix:** branch on the error flag before dispatching on tool name.
```python
def _project_tool_result(d: dict) -> dict:
    tool, result, is_error = d.get("tool"), d.get("result"), d.get("is_error")
    if is_error or not isinstance(result, dict):
        # Publish that it failed and which guard, never the denied payload.
        return {"type": "tool_result", "tool": tool, "is_error": True,
                "denied_by": result.get("denied_by") if isinstance(result, dict) else None}
    ...  # success branches, each also carrying "is_error": False
```

### WR-02: `notice.results` is `project()`'s only forward of unconstrained shape — a future one-line edit in `agent.py` leaks retrieved prose

**File:** `src/relay/events.py:193-196` vs `src/relay/agent.py:476, 487`
**Issue:** Every other allowlisted field in `project()` forwards a scalar or an enum. `"results": d.get("results")` forwards whatever the notice carries. Today `agent.py:487` sets it to `len(payload.get("results", []))` — an int — but nothing in `project()` enforces that. The natural future edit ("make the degraded notice show *which* results we fell back to") changes that line to the list, and `project()` then publishes each result's `text` and `heading` — the retrieved KB prose that `_project_tool_result` goes out of its way to strip four lines earlier — to an unauthenticated endpoint. Nothing would fail: `test_project_keeps_the_cost_and_outcome_the_dashboard_runs_on` (`test_run_events.py:310`) passes `results: 3` and only asserts on `cause`.

**Fix:** make the field name state its own shape and coerce it.
```python
if t == "notice":
    n = d.get("results")
    return {"type": t, "kind": d.get("kind"), "tool": d.get("tool"),
            "retrieval_mode": d.get("retrieval_mode"), "cause": d.get("cause"),
            "result_count": n if isinstance(n, int) else None}
```

### WR-03: `RunEventBroker.closed` is write-only in production; `subscribe()` ignores it and `close()` never clears `_subs`

**File:** `src/relay/events.py:53, 55-58, 101-111`
**Issue:** `self.closed` is assigned at `:53` and `:109` and read *nowhere* in `src/` — only `test_run_events.py:184` reads it, so it is a field that exists to satisfy a test. Consequences:

- A `/events` connection accepted after `lifespan`'s `broker.close()` (`main.py:62`) subscribes successfully, yields a snapshot built from a drained registry, and then sits for the full `events_idle_seconds` (300s default) never receiving a sentinel — the precise failure `close()` exists to prevent, inside uvicorn's graceful-shutdown window. The window is narrow (uvicorn stops accepting first) but it is exactly the "narrow race" this codebase elsewhere closes rather than tolerates (`runs.py:67-88` argues the same case for `RegistryDraining`).
- `close()` fans the sentinel but leaves every queue in `_subs`; removal happens only in each generator's `finally`. A subscriber task cancelled without its finally running is retained for the life of the process, and `publish` keeps writing to it — the exact leak `unsubscribe`'s docstring (`:61-68`) says is worse than a leaked registry entry.

**Fix:** honour the flag in `subscribe()` (raise / return a pre-sentinelled queue) and `self._subs.clear()` after the fan-out in `close()`.

### WR-04: the redaction boundary is split across two modules, and `events.py` claims otherwise

**File:** `src/relay/events.py:9-12` (docstring), `src/relay/main.py:331-352`
**Issue:** The module docstring states `project()` "is the only path raw event data may take to become a public frame", and `/events`' own docstring says "Do not add a second serialisation path around that" (`main.py:362`). `_snapshot_frame()` *is* a second public serialiser, on the same endpoint, in a different module. It is correct today and `test_events_sends_initial_snapshot_on_connect` pins its exact key set (`test_run_events.py:814`), so this is a structural warning, not a leak: a reviewer auditing "the redaction boundary" opens `events.py` and does not see it, and nothing asserts that no *third* path exists.

**Fix:** move `_snapshot_frame` into `events.py` beside `project()` (it needs only `list[ActiveRun]`, so `main.py` passes `app.state.runs.snapshot()`), and add a test that the `/events` generator's non-comment output is only ever `_snapshot_frame()` or `project()` output.

### WR-05: `run_events` retains raw customer PII, ticket bodies, reply text and tool arguments indefinitely with no retention or size cap

**File:** `src/relay/db.py:65-79`, `src/relay/events.py:225-239`
**Issue:** `payload` is stored RAW by design (D-01, and `_insert_event`'s docstring is explicit). There is no `DELETE`, no retention sweep and no size cap anywhere in the codebase; `db.py:86` acknowledges the table "grows by ~10 rows per run for the life of the volume" while reasoning about *indexing* and never about *retention*. On a public demo that anyone can drive (5 runs/hour/IP), this accumulates unbounded personal data — every customer email, every ticket body, every reply the agent sent — on a Fly volume, forever. That is both a disk-exhaustion path on a 512MB machine and a data-protection posture nobody chose.

**Fix:** a retention sweep alongside the existing daily-budget read, plus a stated window.
```python
# db.py or a small maintenance helper, called from lifespan startup
conn.execute("DELETE FROM run_events WHERE created_at < datetime('now', ?)",
             (f"-{settings.events_retention_days} days",))
```

### WR-06: `test_recorder_untouched_files` cannot fail for the thing it names, and fails for things it does not

**File:** `tests/test_run_events.py:647-667`
**Issue:** It runs `git diff --quiet HEAD -- <frozen files>` — *working tree vs HEAD*. On any committed state that diff is empty by definition, so it is green regardless of what the phase did to those files. **Mutation that exposes it:** `echo "raise SystemExit" >> src/relay/evals.py && git commit -am wip && pytest -k untouched_files` → still passes. It proves the tree is clean, not that the phase left `evals.py` alone.

Two further defects in the opposite direction:
- Any developer with an unrelated *uncommitted* edit to `evals.py`, `mcp_server.py` or `ci.yml` gets a red suite reading "phase 5 modified a frozen file" — a false positive that will train people to ignore it.
- Run outside a git checkout (installed sdist/wheel, `pip install .` in the Dockerfile), `git diff` exits 128 and the test fails with the same misleading message. The suite now has a hidden dependency on `git` and on CWD being a repo.

The wave summary self-declares it "a regression guard, not a proof". It is not even that.

**Fix:** delete it, or diff against the phase base and skip when there is no repo:
```python
if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=repo, capture_output=True).returncode:
    pytest.skip("not a git checkout")
base = subprocess.run(["git", "merge-base", "HEAD", "main"], ...).stdout.strip()
assert subprocess.run(["git", "diff", "--quiet", f"{base}..HEAD", "--", *frozen], ...).returncode == 0
```

### WR-07: no test covers persistence or projection on any early-return path of `run_ticket`

**File:** `tests/test_run_events.py` (all scripted runs), `src/relay/agent.py:313, 321, 331, 542, 561`
**Issue:** Every `FakeClient` script in the file ends on `stop_reason="end_turn"`. Nothing exercises `_persisted` on the `api_connection_error`, `api_error`, `model_refusal`, `budget_exceeded` or `step_limit_reached` returns, and nothing scripts a guardrail denial (which is why CR-02 is invisible to the suite). **Mutation that survives the whole suite:** delete `await _persisted(...)` from the `budget_exceeded` yield (`agent.py:542-544`), leaving a bare `yield AgentEvent(...)` — 315 still pass, and a budget-exceeded run silently loses its terminating row.

The `error` branch of `project()` (`events.py:181-182`) is also untested against a real `anthropic.APIStatusError`: `d.get("type")` is the *upstream response body's* `error.type` (`anthropic/_exceptions.py:79-83`), i.e. a value Anthropic controls, republished verbatim to an unauthenticated endpoint. It is an enumerated string today; nothing in this repo asserts that.

**Fix:** one scripted run per terminal reason asserting `(seq, type)` in `run_events` and the projected frame, plus one denial run pinning CR-02's ordering either way.

### WR-08: the guarded `ALTER TABLE` is check-then-act and races two processes booting against the same volume

**File:** `src/relay/db.py:248-250`
**Issue:** `PRAGMA table_info(runs)` and the `ALTER` are two separate statements with no lock and no enclosing transaction. `init_db` is called from `main.py:34`, `mcp_server.py:147` *and* `evals.py:337`, and `CLAUDE.md`/`ARCHITECTURE.md` explicitly document the HTTP app and the MCP server running against the same `relay.db`. If both observe the column missing, the loser raises `sqlite3.OperationalError: duplicate column name: run_uid` out of `lifespan` and the container fails to boot — on the one code path (`D-13`) written specifically to be safe on the live volume. `test_run_uid_migration_is_idempotent` covers sequential re-runs only.

**Fix:**
```python
with conn.transaction():
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_uid" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")
```
(or catch `sqlite3.OperationalError` and re-check `table_info` before re-raising).

### WR-09: dropped frames are silent — no log, no counter, no gap marker, and a bare `pass` on the double-failure path

**File:** `src/relay/events.py:71-87`
**Issue:** `_offer` swallows every drop, and the inner `except (QueueEmpty, QueueFull): pass` (`:84-87`) is the closest thing in this codebase to an empty catch — with no logging, against `CLAUDE.md`'s structured-logging convention and this project's own "no silent swallowing" posture. Consequences: a viewer whose queue overflowed sees a feed with a hole it cannot detect and will not report; an operator has no signal that the demo is dropping frames; and `events_queue_maxsize = 256` was picked with no measurement of frames-per-run against a browser's read rate, so nobody knows how close to the edge it is. D-10 says drop rather than block; it does not say drop invisibly.

**Fix:** count drops per subscriber and either log at a rate limit or stamp the next frame:
```python
except asyncio.QueueFull:
    try:
        q.get_nowait(); q.put_nowait(frame)
        self.dropped += 1
        logger.warning("events.frame_dropped",
                       extra={"ctx": {"subscribers": len(self._subs), "dropped": self.dropped}})
    except (asyncio.QueueEmpty, asyncio.QueueFull):
        self.dropped += 1
```

### WR-10: `/metrics` now publishes `run_uid` — the join key into the private PII table — via `SELECT *`

**File:** `src/relay/telemetry.py:102, 124`, `src/relay/main.py:324-328`
**Issue:** `run_metrics` does `SELECT * FROM runs`, so the new `run_uid` column now appears in `last_runs` on the ungated public `/metrics` endpoint. The value is a random hex and is not itself a secret, but it is the key into `run_events`, a table this phase deliberately filled with unredacted customer data, and it is being published *before* Phase 6 decides the drill-down's access model. It is also a silent public API shape change with no test pinning `/metrics`' keys — `SELECT *` will do the same for the next column anyone adds.

**Fix:** name the columns in `run_metrics`' query (or project `last_runs` explicitly), and add a test asserting the exact key set of a `last_runs` entry.

### WR-11: `events.py` violates the project's own signature conventions on its most safety-critical function

**File:** `src/relay/events.py:49, 71, 225, 252-254`
**Issue:** `CLAUDE.md` states type hints are mandatory on all function signatures and that functions with more than 2-3 args use keyword-only parameters. `events.py` breaks both on the D-04 seam:
- `:252` `def execute_and_record(self, execute_bound, spec, name: str, raw_input: dict, policy, *, event_type: str)` — three of five parameters unannotated, and five positional args on the function the whole atomicity guarantee rests on. A caller swapping `spec` and `policy` type-checks fine and fails at runtime inside an open transaction.
- `:71` `def _offer(self, q, frame) -> None:` — no parameter hints.
- `:49` `self._subs: set[Any]` — should be `set[asyncio.Queue]`; `Any` here exists only so the test's `_HostileQueue` fits, and it erases the contract for readers.
- `:225` `_insert_event(self, type: str, data: dict)` shadows the builtin `type` (and the sibling `execute_and_record` already calls the same concept `event_type`).

**Fix:** annotate (`execute_bound: Callable[[ToolSpec | None, str, dict[str, Any], ToolPolicy], tuple[str, bool]]`, `spec: ToolSpec`, `policy: ToolPolicy`, `q: asyncio.Queue`, `frame: dict`), make `execute_and_record`'s collaborators keyword-only, rename `type` → `event_type`.

### WR-12: `test_events_heartbeats_then_idle_closes` has too little timing margin for CI

**File:** `tests/test_run_events.py:866-883`
**Issue:** The test pins `events_heartbeat_seconds=0.02` / `events_idle_seconds=0.2` and then asserts `len(keep_alives) >= 2`. That budget allows ~10 heartbeats on an idle machine but only ~2 if each `asyncio.wait_for(q.get(), 0.02)` round trip drifts to 0.1s — routine on a loaded shared CI runner. A miss produces a red suite with no real defect, which is the fastest way to get a load-bearing SC-4 test marked flaky and then skipped. (The falsification property itself is sound — I traced the named mutation: resetting `idle_deadline` in the heartbeat branch makes `_read_to_close` hit its 3.0s ceiling and `pytest.fail`. Only the margin is the problem.)

**Fix:** widen the ratio rather than the count — `heartbeat=0.02`, `idle=1.0`, read timeout `5.0`, assert `>= 2` keep-alives. Still sub-second, ~50x the margin.

---

## Test-Quality Audit

Findings above already cover the two structural test defects (WR-06, WR-07) and the flake risk (WR-12). Verdicts on the four tests the brief singled out:

| Test | Verdict | Evidence |
|---|---|---|
| `test_no_projection_leaks_sensitive_data` (:566) | **Sound.** Not vacuous in either direction. | It proves the sentinels were genuinely in the run twice over — `assert sentinel in body` against the run's own SSE stream (:614) and `assert sentinel in raw` against the `run_events` payloads (:626) — before asserting absence. It covers **every** frame, not the first: the `leaks` comprehension (:638-643) iterates `enumerate(frames)` × `SENTINELS` and asserts `== []`. All three sentinels ride a `tool_use.input`, so the named spread mutation opens all three. Residual gaps (not defects in this test): no sentinel is routed through a `guardrail` or `notice` frame, and no sentinel exists in KB prose, so a mutation adding `text` to the `search_docs` results projection would be caught only by the unit test at :224, not by this one. |
| `test_send_reply_and_its_event_row_commit_atomically` (:346) | **Sound. Forces a real mid-transaction failure.** | It monkeypatches `RunRecorder._insert_event` to raise *after* the tool's own write has run inside the outer transaction, then asserts `COUNT(*) FROM replies == 0` **and** `tickets.status == 'open'` — i.e. it asserts the rollback, not just co-presence. I verified the named mutation reds it: hoisting `_insert_event` below the `with` leaves the reply committed, so `replies == 1`. The happy-path half is also re-read from a **reopened** connection (:399), so an uncommitted-but-visible write cannot pass it. |
| `test_events_heartbeats_then_idle_closes` (:853) | **Falsifiable — the executor's claim holds.** | Resetting `idle_deadline` in the heartbeat branch makes the `while True` loop non-terminating; `_read_to_close`'s `asyncio.wait_for(..., 3.0)` then raises `TimeoutError` → `pytest.fail`. The test does *not* pass under that mutation. Flake margin filed as WR-12. |
| `test_events_sends_initial_snapshot_on_connect` (:778) | **Sound for D-14.** | Publishes the live frame only *after* reading the first chunk (:797), so "snapshot first" is an ordering fact, not a scheduling accident. `assert set(payload["runs"][0]) == {"ticket_id", "running_for_ms"}` is an exact-key assertion, so adding a field to `ActiveRun` and spreading it via `asdict()` reds it. Weakness: it observes a single run, so it cannot see a per-run field being cross-contaminated. |
| `test_recorder_untouched_files` (:647) | **Cannot fail.** | See WR-06 — named mutation and two false-positive modes. |
| `test_broker_never_leads_the_database` (:513) | **Sound.** | Samples `COUNT(*) FROM run_events` *inside* the patched `publish`, on the loop, and asserts both `committed >= k` and `committed[-1] == len(committed)` — so a run that quietly stopped persisting some events cannot hide behind the `>=`. The `create_task` mutation it names would red it. |
| `test_publish_drops_oldest_and_never_blocks` (:130) | **Sound**, and the `_HostileQueue` double-failure case is a genuine hostile-input test rather than decoration. |

**Coverage gaps not attributable to any one test:** no denial run (CR-02, WR-01), no early-return run (WR-07), no concurrent-run test (CR-03), no test that `broker.publish` is unreachable except through `project()` (WR-04), no test pinning `/metrics`' response keys (WR-10).

---

## Info

### IN-01: private `_CLOSE_SENTINEL` crosses a module boundary
**File:** `src/relay/main.py:18`, `src/relay/events.py:36`
The leading underscore says "internal to `events.py`", but the sentinel is part of the broker's contract with every consumer — `main.py` must compare against it to end the stream. Either make it public (`CLOSE`) or hide the comparison behind `broker.is_sentinel(frame)`; the current form invites a future refactor to treat it as private and break `/events` silently.

### IN-02: SSE responses set no anti-buffering headers
**File:** `src/relay/main.py:321, 411`
Neither `StreamingResponse` sets `Cache-Control: no-cache` or `X-Accel-Buffering: no`. `test_events_delivers_a_live_run` proves liveness at the application layer; an intermediary that buffers turns the same feed into a batched one in production, and nothing in the suite can observe that. Cheap insurance on a route whose entire value is being live.

### IN-03: unvalidated interpolation into the SSE frame header
**File:** `src/relay/main.py:404`
`f"event: {frame['type']}\ndata: ..."` interpolates a dict value into SSE framing. Every `type` originates from a literal today (`agent.py` yield sites, `project()`, `_snapshot_frame`), so it is safe; a future type derived from any external value would be a frame-splitting injection on a public endpoint. A one-line guard (`assert frame["type"] in _PUBLISHABLE_TYPES`, or `.replace("\n", "")`) removes the class.

### IN-04: per-event transaction plus a synchronous `record_run` on the loop amplifies lock contention
**File:** `src/relay/events.py:249-250`, `src/relay/telemetry.py:76`, `src/relay/main.py:296-312`
`RunRecorder.record` opens one `transaction()` per event (~10/run), each acquiring `Database`'s process-wide `RLock` from a worker thread, while `record_run` still runs **synchronously on the event loop** in `event_stream`'s finally. `main.py:113-118` documents a measured 0.81s loop stall from exactly this pattern and a 3s container `HEALTHCHECK`. The nesting itself is correct — I traced it and found no path where a transaction spans a `yield` or crosses threads: `record` and `execute_and_record` both open and close entirely inside their `to_thread` worker, and `Database._depth` is only ever touched under the held `RLock`. This is a headroom note, not a defect, but it is worth measuring before Phase 6 raises concurrency.

---

_Reviewed: 2026-08-12_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_

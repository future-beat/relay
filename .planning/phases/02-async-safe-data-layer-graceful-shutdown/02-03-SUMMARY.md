---
phase: 02-async-safe-data-layer-graceful-shutdown
plan: 03
subsystem: agent-loop / tools / telemetry
tags: [asyncio, to_thread, transactions, sqlite, concurrency, test-doubles]
status: complete
requires:
  - "plan 02-01 — relay.db.Database.transaction() and the materialised Result; this plan is its first consumer"
provides:
  - "tools.create_escalation / send_reply / set_category as single units of work (with db.transaction())"
  - "telemetry.record_run wrapped in a transaction, still a plain sync keyword-only call"
  - "the single asyncio.to_thread offload seam in agent.run_ticket — tool execution no longer blocks the event loop"
  - "tests.helpers.TicketAwareFakeClient — one fake client that serves overlapping runs"
affects:
  - "plan 02-05's concurrency test consumes TicketAwareFakeClient"
  - "plan 02-05's test_no_registered_executor_is_a_coroutine_function is the mechanical gate for the sync contract this plan preserved (D-02)"
  - "plan 02-04's main.py handler offloads sit above this seam; record_run stays sync so its CR-01 finally path is unchanged"
  - "phase 3's retriever replaces search_docs' body underneath the same sync executor contract"
tech-stack:
  added: []
  patterns:
    - "one offload seam, sync executors below it: await asyncio.to_thread(_execute_guarded, ...) inside a span block that holds no yield"
    - "lastrowid read inside the transaction block, json.dumps return outside it"
    - "type hints follow the object that is actually passed — sqlite3.Connection hints removed from the two files this plan owns"
key-files:
  created: []
  modified:
    - src/relay/tools.py
    - src/relay/telemetry.py
    - src/relay/agent.py
    - tests/helpers.py
key-decisions:
  - "lookup_customer and run_metrics were retyped to Database alongside the writers even though neither needed a transaction — leaving one stale sqlite3.Connection hint behind would have kept `import sqlite3` alive in both files purely to annotate an object that is never a sqlite3.Connection"
  - "set_category is a single UPDATE and still got a transaction block — the plan's acceptance gate demands 3, and uniformity across the three write-tier executors is worth more than saving two lines on the one that is currently single-statement"
  - "the offload comment names what cancellation does NOT buy (the worker thread runs to completion, so a disconnect is not 'no side effect') because that is the non-obvious half and the transaction is what makes it safe"
  - "TicketAwareFakeClient's regex was validated against a real ticket_prompt(...) string, not a hand-written one — the prompt renders 'New support ticket #<id>', so r'[Tt]icket #?(\\d+)' matches"
requirements-completed: [DATA-01]
metrics:
  duration: ~9 min
  tasks-completed: 3
  files-changed: 4
  tests-added: 0
  suite: 117 passed (unchanged from the wave-1 baseline; this plan adds no tests — 02-05 owns them)
  completed: 2026-08-09
---

# Phase 2 Plan 03: Transactions Below the Edge & the Offload Seam Summary

Tool execution now runs off the event loop through a single `asyncio.to_thread` call, and the four writers it can reach commit as single units of work — so the offload is a fix rather than the regression it would have been against Phase 1's hand-rolled `commit()` calls.

## What Was Built

**`src/relay/tools.py`** — the three write-tier executors became one unit of work each:

- `create_escalation`, `send_reply`, `set_category` take `db: Database` and wrap their statements in `with db.transaction():`. The explicit `conn.commit()` calls are gone — `transaction()` commits on clean exit.
- `cur.lastrowid` is assigned to a local (`escalation_id`, `reply_id`) **inside** the block. Reading it after the lock drops returns whatever another thread inserted in the meantime.
- The `json.dumps(...)` returns stay outside the block, and the "Email delivery is mocked" comment stays where it was.
- `lookup_customer` was retyped (no transaction — one SELECT plus one SELECT, both reads). `build_registry` keeps its `conn` parameter name and every lambda closure verbatim, which is what keeps `ToolSpec.execute` a sync `Callable[..., str]`.
- `import sqlite3` is gone; `from .db import Database` replaces it.

**`src/relay/telemetry.py`** — `record_run`'s INSERT is inside `with conn.transaction():`. It is one statement, but the implicit commit is exactly the cross-request hazard. The function is still `def`, still keyword-only after `conn`, and contains no `await` — it runs in `event_stream`'s `finally`, the fifteen lines Phase 1's CR-01 fix owns. `run_metrics` retyped to `Database` and stays sync; plan 02-04 offloads it at the `/metrics` handler.

**`src/relay/agent.py`** — exactly one line changed shape:

```python
result, is_error = await asyncio.to_thread(
    _execute_guarded,
    spec, block.name, block.input, policy,
    bound_ticket_id=ticket["id"],
)
```

It sits inside the existing `with tracer.start_as_current_span(f"tool.{block.name}", context=run_ctx)` block, which contains no `yield` — that is what makes an `await` safe here under the module's suspend-at-yield rule. `import asyncio` joined the stdlib group. `_execute_guarded` itself is byte-identical: still sync, still `tuple[str, bool]`, still carrying its sanctioned `except Exception ... # noqa: BLE001`.

**`tests/helpers.py`** — appended `TicketAwareFakeClient`. `FakeClient` plays one fixed script from a single iterator, so six overlapping runs cannot share an instance; this one parses the ticket id out of the prompt and answers per-ticket, which is what makes concurrent runs issue genuinely concurrent *writes*.

## Key Implementation Details

The load-bearing pairing is that the transactions and the offload had to land together. `to_thread` against Phase 1's `INSERT → UPDATE → commit()` sequence is the Pitfall 3 bug: `commit()` is connection-scoped, so a second thread committing between the INSERT and the UPDATE makes the first thread's reply row durable, and the first thread's rollback then undoes nothing. Task 1 before Task 2 is not cosmetic ordering.

Span parenting needed no code change: `asyncio.to_thread` runs its target inside `contextvars.copy_context()`, so the tool span is still current inside the worker thread. `grep -v '^\s*#' src/relay/agent.py | grep -c 'async with'` is still `0` — `to_thread` is an `await`, not a context manager.

Verified end to end beyond the suite, with six overlapping `run_ticket` calls sharing one `Database` and one `TicketAwareFakeClient` against a file-backed DB:

```
tickets: [(1,'resolved'), (2,'resolved'), (3,'resolved'), (4,'resolved'), (5,'resolved'), (6,'resolved')]
replies: [1, 2, 3, 4, 5, 6]
```

Six resolved tickets, exactly one reply each, no interleaved row — the shape plan 02-05 will pin as a test.

## How to Verify

```bash
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q                     # 117 passed
.venv/bin/ruff check src tests                                          # All checks passed!
grep -c 'with db.transaction()' src/relay/tools.py                      # 3
grep -v '^\s*#' src/relay/tools.py | grep -c '\.commit()'               # 0
grep -c 'await asyncio.to_thread' src/relay/agent.py                    # 1
grep -v '^\s*#' src/relay/agent.py | grep -c 'async with'               # 0
grep -c 'async def record_run\|await ' src/relay/telemetry.py           # 0
git diff a0b73fe -- tests/helpers.py | grep -c '^-[^-]'                 # 0 (append-only)
```

Contract probes:

```bash
# _execute_guarded is still sync and still returns tuple[str, bool]
python -c "import inspect; from relay.agent import _execute_guarded as f; assert not inspect.iscoroutinefunction(f); print(inspect.signature(f).return_annotation)"
# record_run's first param is still `conn`, everything after it keyword-only
python -c "import inspect; from relay.telemetry import record_run as f; p=list(inspect.signature(f).parameters); assert p[0]=='conn' and all(inspect.signature(f).parameters[n].kind.name=='KEYWORD_ONLY' for n in p[1:]); print('sig ok')"
# no registered executor became a coroutine function (D-02, previewing 02-05's gate)
python -c "import inspect,pathlib; from relay.db import connect,init_db; from relay.tools import build_registry; d=connect(':memory:'); init_db(d); assert not [n for n,s in build_registry(d,pathlib.Path('kb')).items() if inspect.iscoroutinefunction(s.execute)]; print('all sync')"
```

D-03 gate — prints nothing:

```bash
git diff --name-only a0b73fe -- src/relay/mcp_server.py src/relay/evals.py src/relay/main.py src/relay/ratelimit.py \
  tests/test_api.py tests/test_auth.py tests/test_guardrails.py tests/test_mcp.py \
  tests/test_observability.py tests/test_ratelimit.py tests/test_tools.py tests/test_evals.py
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Task 3's verification probe called `ticket_prompt` with the wrong signature**

- **Found during:** Task 3, reading `src/relay/prompts.py` as the task instructed before writing the regex.
- **Issue:** The plan's inline probe passes a dict — `ticket_prompt({'id':42,'customer_email':...})` — but the real signature is positional: `ticket_prompt(ticket_id, customer_email, subject, body)`. Run as written it raises `TypeError` before ever exercising the double, so the acceptance criterion ("resolves the ticket id out of a **real** `ticket_prompt(...)` string") could not be met by the literal command.
- **Fix:** Ran the probe with the real signature, `ticket_prompt(42, 'ava@acmecorp.com', 's', 'b')`, and extended it to also assert the second-turn `end_turn` branch. Printed `ticket-aware ok`. The double itself needed no change — the prompt does render `New support ticket #42`, so RESEARCH.md's `r"[Tt]icket #?(\d+)"` matches as written.
- **Files modified:** none (verification command only).
- **Commit:** `eb4fc17`

**2. [Rule 3 - Blocking] A comment tripped its own acceptance gate**

- **Found during:** Task 1 verification.
- **Issue:** The comment explaining why `record_run` stays sync read "where a new await is a risk", which made `grep -c 'async def record_run\|await ' src/relay/telemetry.py` return `1` against a required `0`.
- **Fix:** Reworded to "where an extra suspension point is a risk". Same meaning, gate satisfied honestly rather than by loosening the gate. (Plan 02-01 hit the identical class of issue with `time.sleep` — worth noting for future planners writing substring gates.)
- **Files modified:** `src/relay/telemetry.py`
- **Commit:** `95e5e7c`

### Scope Judgements

**`lookup_customer` and `run_metrics` retyped to `Database`.** The plan scoped the rename to the three writers and `record_run`. Stopping there would have left one `sqlite3.Connection` hint in each file, which keeps `import sqlite3` alive in both solely to annotate an object that is never a `sqlite3.Connection`. Both functions receive the same `Database` every caller already passes; no behaviour changed, and both files are inside this plan's `files_modified`. This is the opposite of D-11's situation — there, the stale hints live in D-03-protected files and had to stay.

**`set_category` got a transaction despite being a single UPDATE.** Required by the plan's `grep -c 'with db.transaction()' == 3` gate, and the right call regardless: the three write-tier executors now read identically, so the next person adding a statement to `set_category` inherits the boundary instead of having to notice it is missing.

### Deliberate Non-Changes

- **WR-01** (TOCTOU between the budget check and the reservation, `ratelimit.py`) — untouched, per the plan and `01-DEFERRED.md`. `git diff a0b73fe -- src/relay/ratelimit.py` is empty.
- **`record_run` not offloaded** — RESEARCH.md Pitfall 6 measured that offloading works, and recommends against it anyway: a new suspension point in CR-01's `finally` for an unmeasurable gain on one INSERT.
- **`main.py` untouched** — owned by the parallel plan 02-04.
- **D-11 still stands:** `mcp_server.py` and `evals.py` keep their stale `sqlite3.Connection` hints. This plan's `to_thread` change makes `mcp_server.call_mcp_tool`'s direct, synchronous `_execute_guarded` call more load-bearing, not less — it is the reason the function could not become `async def`.

## TDD Gate Compliance

This plan carries `type: execute`, not `type: tdd`, and no task carries `tdd="true"` — it adds no behaviour that a new test would cover (plan 02-05 owns this phase's new tests). The gate applied instead was the inverse: 117 pre-existing tests, including the full D-03-protected set, had to stay green with **zero edits** to any of them. They did, at every one of the three commits. Commit types are therefore `feat` / `perf` / `test` by content rather than a RED→GREEN sequence.

## Known Stubs

None. Every symbol this plan touches is fully wired. `TicketAwareFakeClient` has no in-repo caller yet by design — it is the interface plan 02-05 was planned against, and it was exercised against a real `ticket_prompt` string and a real six-way concurrent run before being committed.

## Threat Flags

None — no new network endpoint, auth path, file access pattern, or schema change. The register's four `mitigate` rows are all implemented:

| Threat ID | Mitigation | Evidence |
|-----------|------------|----------|
| T-02-09 | `ToolPolicy` / `bound_ticket_id` are pure-function checks on their own arguments, so thread placement cannot weaken them; `bound_ticket_id` is still bound at call time from this run's ticket | `tests/test_guardrails.py` passes **unedited** (ASVS V4), including the three prompt-injection ticket-binding tests |
| T-02-10 | `with db.transaction()` makes the lock the transaction boundary; `lastrowid` read inside the block | `grep -c 'with db.transaction()'` is 3; six-way concurrent probe yields one reply per ticket |
| T-02-11 | `record_run` transactional, sync, keyword-only signature unchanged | signature probe prints `sig ok`; `test_mid_stream_disconnect_still_records_the_spend` passes unedited |
| T-02-12 | `_execute_guarded` stayed sync; no executor is a coroutine function | `inspect.iscoroutinefunction` probe over the whole registry returns empty |

T-02-13 is inherited from plan 02-01's materialised `Result` and this plan adds no unlocked cursor stepping. T-02-SC holds: `pyproject.toml` untouched, no package installed.

## Requirements Satisfied

- **DATA-01** (offload half): tool execution is off the event loop through one seam, and the writes it reaches are transactional so the offload is not a regression. The HTTP-handler offloads belong to plan 02-04 and the mechanical coroutine-contract test to plan 02-05.

## Self-Check: PASSED

- `src/relay/tools.py` — FOUND (modified)
- `src/relay/telemetry.py` — FOUND (modified)
- `src/relay/agent.py` — FOUND (modified)
- `tests/helpers.py` — FOUND (modified, append-only)
- Commit `95e5e7c` — FOUND
- Commit `ee58985` — FOUND
- Commit `eb4fc17` — FOUND
- D-03 gate against `a0b73fe` — prints nothing
- `src/relay/main.py` diff against `a0b73fe` — empty (plan 02-04 owns it)
- STATE.md / ROADMAP.md — not modified (orchestrator owns those writes)

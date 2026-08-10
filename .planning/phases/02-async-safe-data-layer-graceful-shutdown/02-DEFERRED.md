# Phase 2 — Deferred review findings

**Deferred:** 2026-08-10, by user decision after the critical and coverage fixes landed.
**Source:** `02-REVIEW.md` (1 Critical, 7 Warning, 8 Info — reviewed at `standard` depth)
**Destination:** gap-closure phase — `/gsd:plan-phase 2 --gaps`, alongside Phase 1's outstanding items in `../01-security-perimeter/01-DEFERRED.md`

## Resolved in Phase 2 (not deferred)

| ID | Finding | Commit |
|----|---------|--------|
| CR-01 | An exception from `record_run` inside `event_stream`'s `finally` skipped both `release_run` and `deregister`, permanently leaking a registry entry and killing the drain for the process lifetime | `96f3ffb` |
| WR-02 | The phase's headline behaviour was untested — deleting the `drain()` call, or reordering it after `conn.close()`, both left 126/126 green | `8380531` |
| WR-03 | `transaction()` adoption was untested — stripping it from `send_reply` or `record_run` left 126/126 green | `47b1dbc` |
| WR-06 | `enforce_daily_budget` blocked the event loop on `Database`'s RLock (measured 0.81s, bounded by `busy_timeout` 5s, exceeding the 3s container HEALTHCHECK) | `39bf266` |

## STATUS: CLOSED 2026-08-10

All four deferred warnings were fixed in the gap-closure pass on `phase-2-async-data-layer`:

| ID | Resolution | Commit |
|----|-----------|--------|
| WR-01 | `transaction()` made nest-safe with savepoints (not a depth counter — an inner failure must not roll back the connection) | `977586d` |
| WR-04 | `register()` raises `RegistryDraining`; the refusal is delivered as an in-stream `error` event since the status line is already 200 | `32d9c2f` |
| WR-05 | The drain waiter is built on the loop that awaits it, dropped in a `finally` | `5dc3d1e` |
| WR-07 | Both readers annotated `Database`; a pairing assertion added so the next audit cannot miss it | `cd4e284` |

**Found during the fix, not by either review:** the explicit `BEGIN` in `transaction()` is
load-bearing. pysqlite only auto-begins ahead of DML, so without it an outer block that has not
yet written leaves the *inner* block holding the outermost savepoint — and SQLite defines
`RELEASE` of that savepoint as a commit, reintroducing the original bug by another route. Phase 5's
`run_events` writer emits a step event before the run row exists, which is exactly that shape.
Independently verified: removing the `BEGIN` fails
`test_an_inner_block_that_opens_the_transaction_still_cannot_commit_it`.

The original deferral rationale is preserved below for history.

## Deferred to gap closure

### WR-01 — `transaction()` is not nest-safe — **CLOSED `977586d`**
An inner `transaction()` block commits the outer block's partial work; the outer rollback then
undoes nothing. This reintroduces Pitfall 3 through the very API built to prevent it.

**Why deferred:** no call site nests today, so it is latent rather than live.
**Why it will bite:** Phase 5's `run_events` writer writes step events *during* a run that is
already inside a transaction — that is exactly where someone nests. Fix before Phase 5 lands,
not after.

**Fixed with savepoints rather than a loud error or a depth counter.** Only the outermost level
commits or rolls back the connection; inner levels take a `SAVEPOINT`, so an inner unit of work can
fail on its own without discarding the enclosing one, and an outer rollback still discards it. A
plain depth counter would have flattened nesting — an inner failure would roll back the whole
connection, so Phase 5's failed step event would silently take the run row with it.

**The non-obvious half:** the outermost level now issues an explicit `BEGIN`. pysqlite only
auto-begins ahead of DML, so an outer block that has not written yet leaves the *inner* block
holding the connection's outermost savepoint — and SQLite defines `RELEASE` of that savepoint as a
commit, which is the original bug by another route. Phase 5's writer emits a step event before the
run row exists, i.e. exactly that shape. Covered by
`test_an_inner_block_that_opens_the_transaction_still_cannot_commit_it`, which is the only one of
the three new tests that catches a dropped `BEGIN`.

### WR-04 — a run can register *after* `drain()` has already returned — **CLOSED `32d9c2f`**
`drain()`'s docstring claims it stops admitting runs, but `RunRegistry.register()` never consults
`self.draining`. The refusal lives ~100 lines away in `main.py`, is checked in the *handler*, while
registration happens later in the *generator body* (correctly, per Pitfall 4) — and those two
points are separated by an arbitrary scheduling gap. Reviewer's probe:

```
drain returned: True   draining: True
registered after drain; active = 1
second drain: False   elapsed 0.203
```

**Why deferred:** the window is narrow and the outer timeouts (uvicorn 20s, Fly 30s) absorb it in
practice. **Why it still matters:** it means the drain's own guarantee is weaker than its docstring
states, and the honest fix is either to make `register()` refuse while draining or to correct the
docstring — currently the code and the contract disagree.

**Fixed by enforcing the contract, not by weakening it.** `register()` raises `RegistryDraining`,
and `event_stream` delivers the same `shutting_down` copy as an in-stream `error` event — by then
the `StreamingResponse` has locked its status line at 200, so it cannot be a 503. The reservation is
released explicitly on that path, because returning early skips the `finally` that normally does it.
The handler's 503 stays: it is the cheap check that keeps the common case out of the generator.
Phase 5's second registration site inherits the refusal without knowing about `main.py`.

### WR-05 — `RunRegistry._idle` binds to the first event loop that waits on it — **CLOSED `5dc3d1e`**
`asyncio.Event` is constructed in `__init__` but bound to whichever loop first awaits it. Surfaced
as a teardown `RuntimeError` under mutation.

**Status changed by the CR-01 fix:** the review identified two escalation paths. The first — "CR-01
removes the fast-path protection outright" — is now closed, because `active` returns to 0 and the
drain short-circuits before touching the event. The second, offloading `record_run` (Pitfall 6),
is still live. The underlying binding defect is untouched and remains unasserted.

**Fixed:** `drain()` builds the `asyncio.Event` on the loop that is about to wait on it and drops it
in a `finally`, so nothing loop-bound survives a drain. `register()` no longer needs a paired
`clear()` — a waiter exists only while a drain is waiting, and a drain in progress now refuses new
runs (WR-04). `test_one_registry_can_drain_on_two_different_event_loops` drains the same registry
from two `asyncio.run` loops with a run genuinely in flight in each; under the old code it
reproduces the reviewer's `RuntimeError: ... is bound to a different event loop` verbatim.

### WR-07 — stale `sqlite3.Connection` annotations in `ratelimit.py` — **CLOSED `cd4e284`**
`ratelimit.py:147,196` still annotate `sqlite3.Connection` though they now receive `Database`.
Unlike `mcp_server.py`/`evals.py` (recorded as D-11), this file is **not** D-03-protected, so it
could simply be corrected. The D-11 audit missed it.

**Fixed:** both now annotate `Database`, and `import sqlite3` — which existed only to support these
two hints — is gone. `test_the_budget_readers_annotate_what_connect_actually_returns` asserts the
pairing, since nothing asserting it is why the audit missed the file in the first place.

### Info findings (8)
Not itemised here. See `02-REVIEW.md` `## Info`.

## Verified as correct — do not re-litigate

`02-REVIEW.md` carries a "Verified as Correct" table of 12 settled items, including: the `to_thread`
offload seam, OTel span parenting surviving the offload, close-vs-transaction ordering,
registration-inside-generator, 503-before-reservation ordering, the three-way timeout nesting, and
Phase 1's WR-01 deferral being intact. A re-review should start from that table.

## Known-unverified, deploy-time

Carried forward — neither is closable in CI:

- `fly config show` reporting `kill_timeout = 30`, and a `fly deploy` during an active run showing
  the drain log line rather than a truncated stream.
- `fly machine list` still reaching `stopped` when idle — the real-world counterpart to the
  registry-empty test, and the guard on the "cheap to keep running" core-value constraint.

Plus Phase 1's two outstanding deploy-time items (live `Fly-Client-IP` behaviour; README demo key
matching what `fly secrets set` actually sets).

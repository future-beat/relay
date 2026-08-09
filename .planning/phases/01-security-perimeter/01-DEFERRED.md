# Phase 1 — Deferred review findings

**Deferred:** 2026-08-09, by user decision after the critical fixes landed.
**Source:** `01-REVIEW.md` (3 Critical, 6 Warning, 8 Info — reviewed at `standard` depth)
**Destination:** gap-closure phase — `/gsd:plan-phase 1 --gaps`

## Resolved in Phase 1 (not deferred)

| ID | Finding | Commit |
|----|---------|--------|
| CR-01 | Mid-stream disconnect discarded the run's cost, under-counting the daily ceiling | `b6da97e` |
| CR-02 | Leaked reservation was permanent and remotely triggerable (10 aborts → service-wide 503) | `108d73d` |
| CR-03 | Auth failures were unmetered — unlimited online key guessing | `c81598e` |
| — | `demo.sh` regression introduced by CR-03's `.env.example` change | `bbc40a8` |
| WR-03 | `Fly-Client-IP` trusted verbatim: first-occurrence wins, no IP validation, `/` injected limiter-key segments | `ae7b324` |
| WR-05 (docs half) | README claimed the read path was **Defended**; narrowed to writes, read path recorded as undefended | `3440554` |

## Deferred to gap closure

### WR-01 — TOCTOU between the budget check and the reservation
`enforce_daily_budget` and `reserve_run` are separated by `await enforce(...)`, so concurrent
runs can each observe the pre-reservation total and overshoot the ceiling.

**Why deferred:** bounded overshoot of roughly one run's cost on a $5/day demo budget. Closing it
means locking the hot path, which costs more than the error it prevents. Recommend accepting
permanently rather than fixing, unless the ceiling ever guards something that actually matters.

### WR-02 — an exhausted budget disables rate limiting on `/process`
The budget check precedes the limiter and raises, so a 503 short-circuits throttling entirely.
Each such request also runs an unindexed `SUM(cost_usd)` over the whole `runs` table.
`tests/test_ratelimit.py::test_rate_limit_and_budget_ordering` asserts the current ordering, so
fixing this means deliberately rewriting that assertion.

**Partially mitigated by CR-03:** callers now burn the anonymous auth bucket before reaching the
budget check, so `/process` is no longer unlimited-rate during an outage — the 60/minute anon cap
is the backstop. The ordering inversion itself is unchanged.

### WR-04 — `bound_ticket_id` defaults to `None`, so SEC-04 is fail-open
The guard activates on `bound_ticket_id is not None`. Omitting the keyword silently disables it
with no error, no log, and no failing test. `mcp_server.py:120` already calls it without the
argument.

**Why it matters:** the phase's headline control is opt-in at the call site. A future caller that
forgets the keyword gets no protection and no signal. Recommend a sentinel default that raises
rather than silently permitting.

### WR-06 — the published demo key disagrees with itself across files
`README.md` hardcodes `relay-demo-2026`; `.env.example` now ships empty values with a generation
hint; `demo.sh` requires `RELAY_DEMO_KEY` to be exported. D-02 specified one published key with a
single source of truth, and the dashboard is the only surface that actually reads from settings.

**Note:** the CR-03 and `demo.sh` fixes changed the shape of this finding — the stale `dev-demo-key`
literals are gone, but the README literal is still hardcoded and unasserted.

### Info findings (8)
Not itemised here. See `01-REVIEW.md` `## Info`. IN-07 (release amount asymmetry) was resolved
incidentally by the CR-02 fix.

## Known-unverified, deploy-time

Carried from `01-05-SUMMARY.md` — neither is closable in CI:

- The live Fly proxy's `Fly-Client-IP` behaviour from two networks. WR-03's fix hardens parsing
  and duplicate handling, but the assumption that Fly overwrites rather than appends is still
  unverified against the real proxy.
- That the README's `relay-demo-2026` matches what `fly secrets set RELAY_DEMO_KEY=...` actually
  sets. Nothing in code or tests asserts that pairing (this is WR-06's practical edge).

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

### WR-01 — TOCTOU between the budget check and the reservation — **ACCEPTED, not deferred**

**Closed 2026-08-10 as a deliberate accepted risk.** Recorded in `README.md` under
*Accepted, knowingly*; no code change. Re-litigate only if the ceiling starts guarding
something that matters more than a demo's Claude bill.

`enforce_daily_budget` and `reserve_run` are separated by `await enforce(...)`, so concurrent
runs can each observe the pre-reservation total and overshoot the ceiling.

**The bound, stated accurately.** The original note said "roughly one run's cost", which
understates it twice over. The real bound is `(concurrency - 1) x max_run_cost_usd`, and the
window is not microseconds: between the check and the reservation sit the tiered limiter and
`_get_ticket`'s `asyncio.to_thread` hop, which Phase 2's WR-06 measured at 0.81s while a worker
thread held `Database`'s lock. Reaching the high end needs many distinct IPs arriving in parallel
at the moment the ceiling is crossed — a deliberate attack, not organic traffic — and it costs the
attacker nothing but buys a one-off, bounded overspend.

**Why accepting is still right.** Closing it means claiming the reservation in the dependency,
ahead of the ticket lookup, which means releasing it again on the 404, 409 and drain-refusal paths.
That is precisely the shape of CR-02 — a leaked reservation, permanent and remotely triggerable,
ten of which pin the ceiling shut for the process lifetime. The fix's failure mode is worse and
likelier than the bug's. `max_run_cost_usd` is also the *worst-case* reservation; real runs cost a
fraction of it, so the realised overshoot is smaller than the arithmetic bound.

### WR-02 — an exhausted budget disables rate limiting on `/process` — **CLOSED `970c633`**
The budget check precedes the limiter and raises, so a 503 short-circuits throttling entirely.
Each such request also runs an unindexed `SUM(cost_usd)` over the whole `runs` table.
`tests/test_ratelimit.py::test_rate_limit_and_budget_ordering` asserts the current ordering, so
fixing this means deliberately rewriting that assertion.

**Partially mitigated by CR-03:** callers now burn the anonymous auth bucket before reaching the
budget check, so `/process` is no longer unlimited-rate during an outage — the 60/minute anon cap
is the backstop. The ordering inversion itself is unchanged.

**Fixed by metering the refusal, not by swapping the order.** The 503 path now charges a dedicated
`("process", "outage")` bucket (`RELAY_OUTAGE_PROCESS_LIMIT`, default `10/minute`) on its way out.
That keeps the property the ordering existed for — a global outage does not spend the caller's own
per-IP allowance — so `test_rate_limit_and_budget_ordering` did **not** have to be rewritten and
still asserts exactly what it did before. Two new tests cover the throttle and the non-spend.

### WR-04 — `bound_ticket_id` defaults to `None`, so SEC-04 is fail-open — **CLOSED `add18a3`**
The guard activates on `bound_ticket_id is not None`. Omitting the keyword silently disables it
with no error, no log, and no failing test. `mcp_server.py:120` already calls it without the
argument.

**Why it matters:** the phase's headline control is opt-in at the call site. A future caller that
forgets the keyword gets no protection and no signal. Recommend a sentinel default that raises
rather than silently permitting.

**Fixed by removing the argument, not by defaulting it differently.** D-03 freezes `mcp_server.py`,
which calls `_execute_guarded` with four positional args and no binding, so the keyword could not be
made required. Instead `agent.bind_to_ticket(ticket_id)` returns an executor with the binding baked
in, and `run_ticket` uses it — there is no per-call argument left to omit, and no executor at all
without an int ticket id. `_execute_guarded` keeps its signature and its `tuple[str, bool]` arity;
its default is now the explicit `UNBOUND` sentinel (the MCP path's legitimate "no current run"),
and an explicit `None` — a caller that held a binding and lost it — raises.

### WR-06 — the published demo key disagrees with itself across files — **CLOSED `c06022c`**
`README.md` hardcodes `relay-demo-2026`; `.env.example` now ships empty values with a generation
hint; `demo.sh` requires `RELAY_DEMO_KEY` to be exported. D-02 specified one published key with a
single source of truth, and the dashboard is the only surface that actually reads from settings.

**Note:** the CR-03 and `demo.sh` fixes changed the shape of this finding — the stale `dev-demo-key`
literals are gone, but the README literal is still hardcoded and unasserted.

**Fixed:** the literal is declared once as `relay.config.PUBLISHED_DEMO_KEY`, and a test pins both
the README curl example and `scripts/demo.sh` to it while rejecting any second demo-key literal in
either file. Deliberately *not* the default for `settings.demo_key` — auth fails closed when unset,
and a default would make every unconfigured deployment honour a key published on the internet; that
is asserted too, so the convenience cannot be added later by mistake. `.env.example` still ships
empty: local dev generates its own key, and only the hosted instance uses the published one.

This also closes the second deploy-time item below in CI as far as it can be closed: the README and
`demo.sh` now provably agree with each other. What `fly secrets set` actually holds remains
unverifiable from here.

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

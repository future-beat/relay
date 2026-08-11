# Phase 5: Run Event Persistence & Live Feed - Context

**Gathered:** 2026-08-11
**Status:** Ready for planning

<domain>
## Phase Boundary

Durably record every agent step to a new `run_events` table, and fan out a public, redacted live feed of runs over an SSE `/events` endpoint. Requirements DATA-03, DASH-01. This phase builds persistence + the live feed and the redaction boundary; the dashboard UI that *consumes* the feed (cards, drill-down, charts, Try-it) is Phase 6. Enough of a minimal dashboard consumer to prove SC-2 is in scope; the polished experience is not.
</domain>

<decisions>
## Implementation Decisions

### Architecture through-line
- **D-01:** Two separate paths that never block each other — the **DB is the source of truth** (durable `run_events`, SC-1), the **broker is a lossy live mirror** (in-memory fan-out, SC-2), and the **public projection is allowlisted** (SC-3). Durability and liveness are decoupled by construction.

### Fan-out mechanism
- **D-02:** In-process **`RunEventBroker`** holding a set of per-subscriber `asyncio.Queue`s. When a run persists an event it also publishes the *redacted projection* to live subscribers. Chosen over DB-tailing because this is a single-process, single-machine deployment (no cross-process consumer to justify polling the DB), and latency is immediate rather than poll-interval.
- **D-03:** On restart, in-flight subscribers simply reconnect and see new runs; history is not replayed to the live feed. Acceptable because durable history lives in `run_events` for Phase 6's drill-down. No `Last-Event-ID` resume (already Out of Scope for the milestone).

### Write path & transaction nesting
- **D-04:** Each event row is persisted **during the stream**, inside the SAME `transaction()` as that step's own writes. This is the first real exercise of Phase 2's nest-safe `transaction()` (deferred WR-01, closed in gap closure): a `send_reply` opens a transaction and the event write for that step nests inside it — savepoints make that correct. Do NOT open a second top-level transaction per event.
- **D-05:** The DB write goes through the existing `asyncio.to_thread` seam (Phase 2), so a slow disk never stalls the paid run's event loop.
- **D-06:** **Publish to the broker only AFTER the DB write commits.** The live feed must never show an event that wasn't durably recorded — the DB is the source of truth, the broker mirrors it, never leads it.

### Redaction boundary (SC-3 — a security boundary)
- **D-07:** **Allowlist, not denylist.** The public projection is built from an explicit set of safe fields: event type, tool *name* (never its inputs), outcome/resolution, cost, retrieval doc *ids* + scores, and guardrail *denials* (that a guard fired + which guard, never the denied payload). Everything else — ticket body, customer email, reply text, tool arguments, API keys — is excluded by construction. A denylist leaks the first field someone forgets; an allowlist fails closed.
- **D-08:** A dedicated test asserts known-sensitive strings (a seeded customer email, ticket body, a fake key) never appear in any projection. This is the SC-3 guard and must be mutation-checked (adding a raw field to the projection makes it fail).

### Scale-to-zero + no-stall (SC-4)
- **D-09:** `/events` streams send a periodic comment **heartbeat** and **close after an idle ceiling** (~5 min with no runs) so a forgotten dashboard tab cannot hold the Fly machine awake and defeat `min_machines_running=0` / "cheap to keep running". `EventSource` auto-reconnects when the user returns.
- **D-10:** The broker uses **bounded queues with drop-oldest** on a slow subscriber. A stalled watcher backpressures nothing — the paid run's publish is fire-and-forget. Satisfies SC-4 both directions: no stall of the paid run, and the machine still reaches `stopped`.
- **D-11:** `/events` is **public and projection-only** (no key). It carries only allowlisted projections, so publishing it openly costs nothing — consistent with the Phase 1 public-surface posture (dashboard/metrics/health are already public).

### Claude's Discretion
- `run_events` schema columns (at least: run/ticket ref, seq, event type, timestamp, a JSON payload column); indexing for Phase 6 drill-down
- Broker queue size and heartbeat/idle interval exact values (defaults per D-09/D-10)
- Whether the broker lives on `app.state` beside the registry, or is folded into `RunRegistry`
- The minimal `/events`-consuming smoke needed to prove SC-2 (not the Phase 6 UI)
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `.planning/phases/02-async-safe-data-layer-graceful-shutdown/02-DEFERRED.md` — WR-01 `transaction()` nest-safety (closed in gap closure); D-04 is its first real exercise
- `.planning/phases/02-async-safe-data-layer-graceful-shutdown/02-CONTEXT.md` — the `to_thread` seam (D-05), the RunRegistry + drain the broker sits beside
- `.planning/phases/01-security-perimeter/01-CONTEXT.md` — the public-surface posture D-11 extends; the `guardrail` event shape the projection summarises
- `.planning/research/FEATURES.md` — run_events as "the hidden critical path"; the redaction + no-stall requirements stated there
- `.planning/research/ARCHITECTURE.md` — the projection-only public `/events` feed, and why a live feed threatens scale-to-zero
- `src/relay/main.py` — `event_stream` (where events are yielded + record_run fires; the persist + publish hooks go here), lifespan
- `src/relay/runs.py` — `RunRegistry` (the broker's likely home / sibling)
- `src/relay/db.py` — `Database`, `transaction()` (nest-safe), the schema DDL for the new table
- `src/relay/models.py` — `AgentEvent` (the event shape being persisted + projected)
- `src/relay/agent.py` — the event types run_ticket yields (tool_use, tool_result, retrieval, guardrail, usage, resolution)
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable assets
- `event_stream` in `main.py` already iterates every `AgentEvent` and yields it to the client — the persist + publish hooks attach here, alongside the existing `record_run`/register/deregister
- `RunRegistry` (Phase 2) is the model for a per-app-startup in-memory coordinator on `app.state`; the broker is a sibling
- `Database.transaction()` is nest-safe via savepoints (Phase 2 gap closure) — D-04 depends on this being correct
- The `to_thread` offload seam (Phase 2) is how the DB write stays off the loop (D-05)
- `guardrail` + `notice` events (Phase 1/3) are already additive on the SSE contract; the projection summarises them

### Established patterns
- Public routes are FastAPI route dependencies, never middleware; SSE responses lock status at 200 on first yield (so `/events` auth would be pointless anyway — it's public by design, D-11)
- Redaction/allowlist mirrors how the citation guard was built (allowlist accept-set, Phase 3)
- Every new control gets a mutation-checked test; a redaction denylist would be the classic "passes while leaking" failure this project keeps catching

### Integration points
- New `run_events` table in `db.py` SCHEMA; persisted from `event_stream` via `to_thread`, inside the step's transaction
- New broker on `app.state`; `event_stream` publishes redacted projections after commit
- New public `GET /events` SSE route subscribing to the broker, heartbeat + idle cap
- The registry-drain (Phase 2) must also close broker subscribers on shutdown
</code_context>

<specifics>
## Specific Ideas
- The redaction test is the load-bearing one: seed a customer email + ticket body + fake key into a run, assert none appear in any projection, and mutation-check by adding a raw field. This is SC-3 and the project's recurring "unfalsifiable check" trap applies squarely.
- D-04 is worth an explicit test that a run persisting events inside a `send_reply` transaction commits both the reply and its event rows atomically — the nesting working end to end, not just in the Phase 2 unit tests.
</specifics>

<deferred>
## Deferred Ideas
- The polished dashboard (cards, per-run drill-down, SVG charts, budget gauge, Try-it form) — Phase 6 (DASH-02..05)
- `Last-Event-ID` / SSE resume — Out of Scope (milestone)
- Rejected-action counter, cost-per-stage attribution — v2, ride on run_events but not this phase
- Persisting the real-model recovery-probe artifact from Phase 4 (a loose end, not this phase's scope)
</deferred>

---

*Phase: 05-run-event-persistence-live-feed*
*Context gathered: 2026-08-11*

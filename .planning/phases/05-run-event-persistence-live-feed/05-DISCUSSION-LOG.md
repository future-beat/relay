# Phase 5: Run Event Persistence & Live Feed - Discussion Log

> Audit trail only. Decisions live in CONTEXT.md.

**Date:** 2026-08-11
**Phase:** 05-run-event-persistence-live-feed
**Mode:** user requested recommendations; all four accepted as-is.

## Fan-out mechanism
Recommended & accepted: in-process `RunEventBroker` (per-subscriber asyncio.Queue), fed off the DB write, not DB-tailing. Single-machine deployment → no cross-process consumer to justify polling; immediate latency. DB is durable truth, broker is live mirror. No history replay on restart (durable in run_events for Phase 6).

## Write path & transaction nesting
Recommended & accepted: persist each event during the stream, inside the step's own `transaction()` (first real exercise of Phase 2's nest-safe savepoints), via the `to_thread` seam so a slow disk never stalls the loop. Publish to the broker only AFTER commit — the feed never leads the DB.

## Redaction boundary
Recommended & accepted: allowlist not denylist. Projection = event type, tool name (not inputs), outcome, cost, retrieval doc ids+scores, guardrail denials (that a guard fired, not the payload). Everything else excluded by construction. Dedicated leak test, mutation-checked. SC-3 is a security boundary.

## Scale-to-zero + no-stall
Recommended & accepted: `/events` heartbeat + idle-close (~5 min) so a forgotten tab can't hold the Fly machine awake; bounded queues drop-oldest so a slow watcher backpressures nothing. Satisfies SC-4 both directions. `/events` public + projection-only (no key), consistent with Phase 1 public surface.

## Deferred
Phase 6 dashboard UI; Last-Event-ID resume (OOS); rejected-action counter & cost-per-stage (v2); persisting Phase 4's recovery-probe artifact.

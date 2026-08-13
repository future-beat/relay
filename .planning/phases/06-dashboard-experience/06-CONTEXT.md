# Phase 6: Dashboard Experience - Context

**Gathered:** 2026-08-12
**Status:** Ready for planning

<domain>
## Phase Boundary

The polished dashboard: a visitor understands the system's cost, quality, and behavior in under a minute — and can run it themselves. Requirements DASH-02 (aggregate cards + outcome distribution from `/metrics` via SQL aggregation), DASH-03 (per-run drill-down from `run_events`), DASH-04 (inline SVG cost/latency charts + daily budget gauge, no CDN, no build step), DASH-05 ("Try it" form with prefilled examples, submits with the published demo key, streams live). Phase 5 delivered the transport (`/events`, `run_events`, a minimal EventSource consumer); this phase delivers the experience on top of it.
</domain>

<decisions>
## Implementation Decisions

### Drill-down disclosure (the phase's security boundary)
- **D-01:** The per-run drill-down is **public but server-redacted**. A route (`GET /runs/{run_uid}` or similar) returns drill-down JSON built **server-side** through the same allowlist discipline as `project()` — tool inputs/outputs render as shapes and verdicts (tool name, argument keys, result status, retrieval doc ids + scores, cited-vs-not, guardrail denials, timings), never raw text. Timings and the cited-vs-not comparison are not sensitive and render in full.
- **D-02:** **Try-it runs are the full-fidelity exception.** A run created via the dashboard's Try-it form may show raw tool inputs/outputs in its drill-down — the visitor authored that ticket and demo tickets contain no real customer PII. Mechanism for marking a run as demo-originated is planner's choice (e.g. a flag on the run row set when the ticket arrives via the demo key), but the distinction must be made server-side, never by the client.
- **D-03:** This implements Phase 5's W-1 condition (see `src/relay/events.py` `attribute_to_run` docstring): `run_uid` stays a correlation token, never a bearer credential — holding a uid gets you nothing that isn't already redacted. Do not add an authenticated full-fidelity path this phase; if one is wanted later it is its own decision.

### Page architecture
- **D-04:** `DASHBOARD_HTML` moves out of `main.py` into a **template file** (e.g. `src/relay/templates/dashboard.html`) read once at startup. No build step, no template engine dependency beyond stdlib string substitution as today (`__RELAY_DEMO_KEY__` replacement). This resolves the CLAUDE.md "inline HTML/JS in a Python module" anti-pattern rather than deepening it.
- **D-05:** Single page remains — no route split, no SPA. The drill-down is a client-rendered panel/modal fed by the drill-down JSON route, not a separate HTML page.

### Try-it experience
- **D-06:** **Three prefilled examples** — billing, bug, how-to — matching the eval categories. Visitor can edit before submitting.
- **D-07:** **Real runs, not dry-run.** The reply/escalation appearing in the drill-down is the payoff; "observably-real" is the core value. The published demo key flows exactly as it does today.
- **D-08:** **Refusals are a first-class UI state, not an error.** When the rate limiter (429) or daily budget (503) refuses, the page renders the actual reason ("demo budget spent for today — resets midnight UTC") as a designed state. The refusal is the cost-control feature being demonstrated.

### Charts & gauge
- **D-09:** Charts are **client-built inline SVG from `/metrics` JSON** — `/metrics` stays a data API; no server-side SVG rendering.
- **D-10:** Cost/latency over time is **time-bucketed by day** (daily cost total, p50/p95 latency), not per-run points — legible at 3 runs or 300.
- **D-11:** The budget gauge reads **server-computed budget arithmetic**: `/metrics` grows a `budget` object (e.g. `{spent_today_usd, daily_ceiling_usd}`) produced by the same code path as `enforce_daily_budget`. The gauge must never re-derive spend in JS — the gauge and the gate must be incapable of disagreeing.

### Claude's Discretion
- Drill-down route shape, pagination/limits on `run_events` reads, and the demo-run marking mechanism (server-side, per D-02)
- Visual design: layout, typography, color, how cards/charts/feed/drill-down compose on the page
- SVG chart implementation details (axes, scales, empty states)
- How the existing minimal feed UI from Phase 5 is absorbed into the designed experience
- Which `/metrics` additions are needed (outcome distribution buckets, daily buckets) — computed via SQL aggregation per DASH-02, keeping WR-10's explicit-column discipline (never `SELECT *`, never expose `run_uid` on `/metrics`)
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### The boundary this phase builds on
- `.planning/phases/05-run-event-persistence-live-feed/05-CONTEXT.md` — LOCKED D-01..D-14 (allowlist projection, publish-after-commit, heartbeat/idle, snapshot); this phase must not weaken any of them
- `src/relay/events.py` — `project()`, `snapshot_frame()`, `attribute_to_run` (its docstring carries the W-1 condition D-03 implements), `RunRecorder`
- `.planning/phases/05-run-event-persistence-live-feed/05-DEFERRED.md` — WR-03/08/11/12 still open; do not silently collide with them
- `.planning/phases/05-run-event-persistence-live-feed/05-VERIFICATION.md` — W-3 (no bound on total connection-holding time) and the INFO on the model-chosen tool name being the only unbounded model-controlled string on the feed

### Surfaces being extended
- `src/relay/main.py` — `DASHBOARD_HTML` (moves out per D-04), `/events` route, `/metrics`, the `_gate` factory for any new route's rate limit
- `src/relay/telemetry.py` — `run_metrics()` and `_PUBLIC_RUN_COLUMNS` (WR-10 discipline for any new columns), `record_run`
- `src/relay/db.py` — `run_events` schema, `purge_expired_run_events` (30-day retention: drill-down must handle a run whose events were swept)
- `src/relay/ratelimit.py` — `enforce_daily_budget` arithmetic the gauge must share (D-11)
- `.planning/phases/01-security-perimeter/01-CONTEXT.md` — the published-demo-key posture D-07 extends

### Requirements
- `.planning/REQUIREMENTS.md` — DASH-02..05 wording
- `.planning/ROADMAP.md` — Phase 6 success criteria SC-1..4
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Phase 5's EventSource consumer in the dashboard: connect/reconnect handling, snapshot listener, per-run grouping by `run_uid`, `textContent`-only rendering — the designed feed absorbs this rather than rewriting it
- `_gate("events", public=True)` pattern: any new public JSON route (drill-down) gets the same anon rate-limit dependency
- `run_events` rows already carry `run_uid`, `ticket_id`, `seq`, `type`, `payload` — the drill-down is a read + server-side redaction over them
- The `/process` SSE stream already streams a run live; Try-it can consume it directly (the visitor's own run is full-fidelity by right — it's their ticket)

### Established Patterns
- Route dependencies, never middleware; SSE locks status at 200 on first yield
- Allowlist redaction built field-by-field, mutation-checked (`project()` is the model)
- All DB reads off the loop via `asyncio.to_thread`
- No bare except; structured logging `extra={"ctx": {...}}`; explicit columns, never `SELECT *`
- Every load-bearing control gets a test whose named mutation turns it red

### Integration Points
- New drill-down JSON route in `main.py` reading `run_events` (offloaded), redacting server-side
- `/metrics` grows outcome-distribution buckets, daily time buckets, and the `budget` object (SQL aggregation, explicit columns)
- `DASHBOARD_HTML` → `src/relay/templates/dashboard.html` + startup read; Dockerfile must COPY the templates dir
- Try-it form posts to the existing `/tickets` + `/process` with the demo key already published on the page
</code_context>

<specifics>
## Specific Ideas
- The refusal states (429/503) should read as the system working as designed — copy like "demo budget spent for today — resets midnight UTC", not an error toast
- Cited-vs-not highlighting in the drill-down: retrieval chunks the reply actually cited visually distinguished from retrieved-but-unused ones
- The under-a-minute test from the phase goal is the bar for layout: cost, quality, behavior legible without scrolling into the weeds
</specifics>

<deferred>
## Deferred Ideas
- Authenticated full-fidelity drill-down for non-demo runs (explicitly out per D-03 — its own decision later)
- W-3 (bounding total `/events` connection-holding time) and the tool-name clamp INFO from 05-VERIFICATION — perimeter work, not dashboard work; candidates for a gap-closure pass
- Rejected-action counter, cost-per-stage attribution — v2 (carried from Phase 5)
- Last-Event-ID / SSE resume — Out of Scope (milestone)
</deferred>

---

*Phase: 6-dashboard-experience*
*Context gathered: 2026-08-12*

# Phase 6: Dashboard Experience - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-08-12
**Phase:** 6-dashboard-experience
**Areas discussed:** Drill-down disclosure, Page architecture, Try-it experience, Charts & gauge shape

---

## Drill-down disclosure

| Option | Description | Selected |
|--------|-------------|----------|
| Public but server-redacted; Try-it runs full-fidelity | Drill-down JSON built server-side through allowlist discipline; shapes/verdicts not raw text; visitor-authored demo runs are the exception | ✓ |
| Demo-key-gated full fidelity | Full payloads behind the published demo key | |
| Fully public raw payloads | Render run_events payloads as stored | |

**User's choice:** Recommended option (asked for recommendations, then locked all four).
**Notes:** Implements Phase 5's W-1 condition — `run_uid` stays a correlation token; holding it gets nothing redacted. Authenticated full-fidelity path explicitly deferred as its own future decision.

---

## Page architecture

| Option | Description | Selected |
|--------|-------------|----------|
| Template file read at startup | `src/relay/templates/dashboard.html`, stdlib substitution, no build step | ✓ |
| Stay inline in main.py | Deepens the CLAUDE.md anti-pattern as the page triples | |
| Split routes / multiple pages | More surface than the single-page demo needs | |

**User's choice:** Recommended option.
**Notes:** Constraint is "no build step," not "no separate file." Single page retained; drill-down is a client panel fed by JSON, not a second page.

---

## Try-it experience

| Option | Description | Selected |
|--------|-------------|----------|
| 3 prefilled examples, real runs, refusals as designed states | billing/bug/how-to; real writes; 429/503 rendered as the cost-control feature working | ✓ |
| Dry-run submissions | Safer but breaks "observably-real" | |
| Single hardcoded example | Less demonstrative of category breadth | |

**User's choice:** Recommended option.

---

## Charts & gauge shape

| Option | Description | Selected |
|--------|-------------|----------|
| Client-built SVG from /metrics JSON, daily buckets, server-computed budget object | /metrics stays a data API; gauge shares enforce_daily_budget arithmetic | ✓ |
| Server-rendered SVG in Python | Couples presentation to the API layer | |
| Per-run scatter points | Illegible at low and high run counts | |

**User's choice:** Recommended option.
**Notes:** The gauge must be incapable of disagreeing with the gate — never re-derive spend in JS.

---

## Claude's Discretion

- Drill-down route shape, pagination, demo-run marking mechanism (server-side)
- Visual design and page composition; SVG implementation details
- How Phase 5's minimal feed UI is absorbed
- `/metrics` additions (distribution buckets, daily buckets, budget object) under WR-10's explicit-column discipline

## Deferred Ideas

- Authenticated full-fidelity drill-down for non-demo runs
- W-3 connection-holding bound + tool-name clamp (perimeter gap-closure, not dashboard work)
- Rejected-action counter, cost-per-stage attribution (v2)
- Last-Event-ID / SSE resume (out of scope for milestone)

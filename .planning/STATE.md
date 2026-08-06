# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-08-05)

**Core value:** A visitor hitting the live demo sees a credible, safe, observably-real AI agent service — impressive to read and watch, cheap to keep running.
**Current focus:** Phase 1 — Security Perimeter

## Current Position

Phase: 1 of 6 (Security Perimeter)
Plan: 0 of TBD in current phase
Status: Ready to plan
Last activity: 2026-08-06 — Roadmap created for v2 "Remaster" milestone (22 requirements across 6 phases)

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: —
- Total execution time: 0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: —
- Trend: —

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Roadmap: Keep SQLite, fix access patterns (no Postgres migration)
- Roadmap: Voyage `voyage-4-lite` embeddings, index built offline and committed
- Roadmap: Server-rendered dashboard (no build step, one container)
- Roadmap: Auth via env-var API key; MCP writes off by default

### Pending Todos

[From .planning/todos/pending/ — ideas captured during sessions]

None yet.

### Blockers/Concerns

[Issues that affect future work]

- Phase 2: `aiosqlite` vs locked-connection + single `to_thread` seam is an open decision (STACK.md vs ARCHITECTURE.md/PITFALLS.md disagree). Resolve in Phase 2 planning; async contagion through the sync `ToolSpec.execute` contract is the deciding constraint.
- Phase 3: Retrieval quality on a 381-word KB is reasoned, not measured. Capture an eval baseline before committing to chunking/similarity-floor decisions.
- Phase 5: `run_events` schema and the public redaction boundary are unspecified — short design pass needed during planning.
- Milestone-wide: no dependency lockfile exists; exact FastAPI/Starlette disconnect semantics need an empirical disconnect test.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| *(none)* | | | |

## Session Continuity

Last session: 2026-08-06
Stopped at: ROADMAP.md and STATE.md written; REQUIREMENTS.md traceability filled
Resume file: None

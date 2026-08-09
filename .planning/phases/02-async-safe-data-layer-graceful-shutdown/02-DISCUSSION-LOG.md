# Phase 2: Async-Safe Data Layer & Graceful Shutdown - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-08-09
**Phase:** 02-async-safe-data-layer-graceful-shutdown
**Areas discussed:** Async DB seam, Shutdown drain mechanism

Note: four areas were offered (async seam, connection/WAL strategy, shutdown drain, scope boundary).
The user was initially unsure which mattered; after a summary of the stakes they chose to discuss the
two real decisions and delegate connection/WAL to the planner (derivable from the seam choice) and the
scope boundary to the orchestrator (bookkeeping).

---

## Async DB seam

| Option | Description | Selected |
|--------|-------------|----------|
| Single `to_thread` offload | `await asyncio.to_thread(_execute_guarded, ...)`; sync contract preserved, MCP/evals/tool tests untouched | ✓ |
| `aiosqlite` rewrite | Async driver throughout; async spreads across 5 modules, silent coroutine-as-tool-result failure with no CI guard | |
| Thread-safe `Database` wrapper | Lock+WAL wrapper owning the connection, called synchronously from a thread | |

**Notes:** This was the phase's open decision, flagged in STATE.md because STACK.md and
ARCHITECTURE.md/PITFALLS.md disagreed. Decided on blast radius: `ToolSpec.execute` is a sync
`Callable[..., str]` and the failure mode of getting it wrong is silent rather than loud.
The wrapper option survives as Claude's Discretion for connection ownership.

| Option | Description | Selected |
|--------|-------------|----------|
| Test asserting no executor is a coroutine function | Converts the silent failure into a CI failure | ✓ |
| Convention + docstring | Rely on review | |
| Add mypy to CI | Catches the bug class generally, but new CI gate, broader scope | |

---

## Shutdown drain mechanism

| Option | Description | Selected |
|--------|-------------|----------|
| Hand-rolled in-flight task registry | Phase 5's live feed needs the same registry; no new dependency | ✓ |
| `sse-starlette` built-in SIGTERM drain | Off-the-shelf `AppStatus.handle_exit` hook with grace period | |
| Both | SIGTERM hook plus registry | |

**Notes:** Deliberately overrides STACK.md's `sse-starlette` recommendation. Reuse by Phase 5 was
the deciding factor.

| Option | Description | Selected |
|--------|-------------|----------|
| ~30s grace period | Typical run ~20s, bounded by step/budget caps; needs matching `fly.toml` kill_timeout | ✓ |
| ~10s | Faster deploys, some streams cut | |
| Drain immediately | Simplest, but contradicts success criterion 3 | |

| Option | Description | Selected |
|--------|-------------|----------|
| Registry holds only active runs | Idle server holds nothing, Fly autostop still suspends; explicit test | ✓ |
| Explicit idle assertion in phase gate | Same design plus manual post-deploy check | |
| Both | Registry design plus deploy-time verification | |

**Notes:** Scale-to-zero is a core-value constraint, so this was raised proactively rather than
left as an implementation detail.

## Claude's Discretion

- Connection ownership (shared+lock vs per-thread vs wrapper) — derive from the seam decision
- WAL/`busy_timeout` pragma placement and representative test fixtures
- Whether the MCP server's separate connection needs the same treatment

## Deferred Ideas

- `run_events` (DATA-03) — Phase 5
- WR-01 TOCTOU — gap closure, despite living in code this phase touches
- mypy in CI — broader than this phase

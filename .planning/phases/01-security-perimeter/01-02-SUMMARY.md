---
phase: 01-security-perimeter
plan: 02
subsystem: security
tags: [prompt-injection, guardrails, sse, agent-loop, opentelemetry, pytest]

# Dependency graph
requires: []
provides:
  - "Keyword-only `bound_ticket_id` guard in `_execute_guarded`, checked between validation and execution"
  - "`denied_by: ticket_binding` denial payload carrying `expected_ticket_id` / `supplied_ticket_id`"
  - "New additive `guardrail` SSE event type, emitted before its `tool_result`"
  - "`guardrail.ticket_id_mismatch` structured warning log and `relay.tool.binding_violation` span attribute"
affects: [04-evaluation, 05-run-events-dashboard]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Server truth asserted at the single tool-call choke point, bound at call time from the run's own ticket dict"
    - "Denials as recoverable model-readable instructions, never exceptions and never run-terminating"
    - "Additive SSE event types ride the existing type-agnostic formatter with zero serialization changes"

key-files:
  created: []
  modified:
    - src/relay/agent.py
    - src/relay/models.py
    - tests/test_guardrails.py

key-decisions:
  - "Reject a mismatched ticket_id rather than silently rebinding it (D-09) — a rebind would make the tool_use SSE event and the dashboard lie about what happened"
  - "Denial message is phrased as a retry instruction naming the correct id, so the run self-corrects instead of ending in ended_without_action (D-10, research Pitfall 3)"
  - "Bind at call time from run_ticket's own ticket dict; never into build_registry or app.state.registry, which are shared by every concurrent run"
  - "Return arity stays tuple[str, bool]; the denial is signalled through a denied_by field so mcp_server.call_mcp_tool is untouched"
  - "AgentEvent.type comment moved above the field rather than kept trailing — the extended union exceeded ruff's 100-char line limit"

patterns-established:
  - "Guard placement: validate first (coerced int), compare second, execute third"
  - "Guardrail observability triple: SSE event + dotted structured warning log + dotted OTel span attribute"
  - "Denial context in logs carries only ids and tool name — never ticket bodies or key material"

requirements-completed: [SEC-04]

# Metrics
duration: 21min
completed: 2026-08-09
---

# Phase 1 Plan 02: Ticket ID Binding Summary

**Server-side `ticket_id` binding in the agent's tool guard chain: a prompt-injected cross-ticket write is rejected with a recoverable, model-readable denial that surfaces as a new `guardrail` SSE event, with the run still resolving after the model retries.**

## Performance

- **Duration:** ~21 min
- **Started:** 2026-08-09T00:00:00Z (approx.)
- **Completed:** 2026-08-09
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- `_execute_guarded` gained a keyword-only `bound_ticket_id: int | None = None`, compared against the validated `ticket_id` **after** Pydantic validation and **before** `spec.execute`. A mismatch returns `{"error": ..., "denied_by": "ticket_binding", "expected_ticket_id": N, "supplied_ticket_id": M}` with `is_error=True` — the executor is never reached.
- The denial reads as an instruction ("Retry with ticket_id=1."), not a refusal, so the model self-corrects inside its existing step and budget limits. The recovery test proves the run still reaches `resolution` — this is the guard against the eval-pass-rate regression flagged in research Pitfall 3.
- `run_ticket` passes `bound_ticket_id=ticket["id"]` at call time and emits an `AgentEvent(type="guardrail", ...)` **before** the corresponding `tool_result`, so a stream consumer reads cause then effect. It also sets `relay.tool.binding_violation` on the tool span and logs `guardrail.ticket_id_mismatch` with only ids and the tool name.
- The tool result is now parsed once (`payload = json.loads(result)`) and reused by both the guardrail branch and the `tool_result` event.
- Four new tests cover denial + zero side effects, event shape and ordering, in-run recovery, and two concurrent runs over one shared registry never cross-binding.
- `src/relay/main.py` needed **zero** changes: the SSE formatter is type-agnostic, so the new event type serializes for free.

## Task Commits

1. **Task 1: Add the ticket_id binding check to the guard chain** — `85def75` (feat)
2. **Task 2: Emit the guardrail event from run_ticket and list it on AgentEvent** — `10741c0` (feat)
3. **Task 3: SEC-04 test coverage — denial, event, recovery, concurrency** — `d101f24` (test)

## Files Created/Modified

- `src/relay/agent.py` — keyword-only `bound_ticket_id` guard, denial payload, call-site binding, guardrail event, span attribute, warning log, phase-1 docstring bullet
- `src/relay/models.py` — `AgentEvent.type` comment extended with `guardrail` and the previously missing `usage`
- `tests/test_guardrails.py` — new `# --- ticket_id binding ---` section with four tests plus `_seed_tickets` / `_reply_ticket_ids` helpers

## Decisions Made

- **Seed real victim/other ticket rows in the new tests.** `replies.ticket_id` has a foreign key to `tickets`, and the `conn` fixture seeds only customers. Without a real ticket 99 row, an unguarded cross-ticket write would fail on the FK anyway and the tests would pass for the wrong reason. Seeding makes the tests genuinely load-bearing — confirmed by the mutation check.
- **`AgentEvent.type` comment placed above the field.** The plan asked for the trailing `#` comment to be extended, but the full seven-member union pushes the line to 103 characters, over ruff's configured limit of 100. Moved above the field; content is exactly as specified.

## Deviations from Plan

### Process deviation (no code impact)

**1. TDD RED phase run as scratch probes rather than committed tests for Tasks 1 and 2**
- **Found during:** Task 1
- **Issue:** Tasks 1 and 2 are marked `tdd="true"` but their `<files>` lists contain only source files, and Task 3's acceptance criterion pins `tests/test_guardrails.py` at exactly four new tests. Writing durable RED tests in Tasks 1–2 would have violated one or the other.
- **Fix:** Ran the RED phase as throwaway probe scripts in the scratchpad (confirmed `bound_ticket_id` did not exist for Task 1; confirmed the injected `send_reply(ticket_id=99)` actually wrote to the victim ticket and reached `resolution` for Task 2), then implemented to GREEN. The durable coverage landed in Task 3 exactly as specified, and the mandated mutation check supplies the real regression proof.
- **Files modified:** none beyond the plan's file lists
- **Verification:** Both probes went RED before implementation and GREEN after; mutation check below.

---

**Total deviations:** 1 process deviation, 0 code deviations
**Impact on plan:** None. All specified files, contracts, and acceptance criteria met as written.

## Verification

| Check | Result |
|-------|--------|
| `pytest -q` (full suite) | 42 passed (38 baseline + 4 new) |
| `pytest tests/test_mcp.py -q` | 6 passed — arity preserved |
| `pytest tests/test_guardrails.py -q` | 15 passed (11 + 4) |
| `ruff check src tests` | All checks passed |
| Guard ordering (`validate_tool_input` → `ticket_binding` → `spec.execute`) | lines 60 → 80 → 85 |
| `grep -c 'async with' src/relay/agent.py` | 0 |
| `git diff --stat src/relay/main.py` (vs plan base) | empty |
| Signature is keyword-only with `None` default | asserted via `inspect.signature` |
| Mutation check (guard disabled) | 3 of 4 new tests fail; reverted, not committed |

Note: the suite count is this worktree's own baseline plus this plan's additions. Plan 01-01 runs concurrently in a separate worktree and adds its own tests; the orchestrator's post-merge count will be higher.

## Issues Encountered

- The first pass-through probe hit `FOREIGN KEY constraint failed` because the in-memory test DB seeds customers but no tickets. This surfaced the FK dependency early and directly informed the `_seed_tickets` helper in Task 3 — without it the new tests would have been vacuous.

## Threat Model Coverage

| Threat ID | Disposition | Where it is enforced / proven |
|-----------|-------------|-------------------------------|
| T-01-05 | mitigated | Guard between validation and execution; `test_mismatched_ticket_id_is_denied` |
| T-01-06 | mitigated | Bound at call time from `ticket["id"]`; `test_concurrent_runs_do_not_cross_bind` |
| T-01-07 | mitigated | `guardrail` event + `guardrail.ticket_id_mismatch` log + `relay.tool.binding_violation` span attribute |
| T-01-08 | mitigated | Retry-instruction wording; `test_run_recovers_after_binding_denial` asserts `resolution`, not `ended_without_action` |
| T-01-09 | mitigated | Log `ctx` carries only `ticket_id`, `tool`, `supplied_ticket_id` |
| T-01-10 | accepted (guarded) | Arity unchanged; 6 MCP tests green |

No new security surface was introduced beyond the plan's threat register — no new endpoints, auth paths, file access, or schema changes.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Phase 4 EVAL-02 has its hook: the `guardrail` event with `guard="ticket_binding"` is a stable, assertable contract, and a denial no longer prevents a run from resolving.
- Phase 5 `run_events` can persist `guardrail` events with no producer changes.
- MCP is deliberately unprotected by this guard (`bound_ticket_id` stays `None` — there is no "current run" over stdio). MCP's protection is SEC-05's default flip, which is a separate plan in this phase.
- One manual `python -m relay.evals --limit 3` before the phase gate is still worth doing: the unit suite cannot see an eval pass-rate regression.

## Self-Check: PASSED

- `src/relay/agent.py` — FOUND
- `src/relay/models.py` — FOUND
- `tests/test_guardrails.py` — FOUND
- `.planning/phases/01-security-perimeter/01-02-SUMMARY.md` — FOUND
- Commit `85def75` — FOUND
- Commit `10741c0` — FOUND
- Commit `d101f24` — FOUND

---
*Phase: 01-security-perimeter*
*Completed: 2026-08-09*

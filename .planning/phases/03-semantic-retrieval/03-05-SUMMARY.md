---
phase: 03-semantic-retrieval
plan: 05
subsystem: agent
tags: [guardrails, citations, grounding, observability, agent-loop]

# Dependency graph
requires:
  - phase: 03-semantic-retrieval plan 04
    provides: "SendReplyInput.citations (optional list[str]) and the search_docs envelope carrying result ids + degraded flag"
  - phase: 03-semantic-retrieval plan 03
    provides: "kb/index.json, so search_docs returns real ids for a run to accumulate"
  - phase: 01-remaster (SEC-04)
    provides: "bind_to_ticket constructor injection and the recoverable-denial + guardrail-event pattern this copies"
provides:
  - "Per-run retrieved_ids set threaded into the executor via bind_to_ticket, grown from search_docs results"
  - "Citation guard: send_reply citing an id not retrieved this run is denied with denied_by='citation' and a retry instruction naming the valid ids"
  - "guardrail event with guard='citation', ordered before its tool_result"
  - "notice event kind='retrieval_degraded' (D-14) — the first AgentEvent type that is news rather than a control firing"
affects: [03-06 paid eval gate, phase 4 evals, phase 5 dashboard trace]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Constructor-injected run state: anything a guard checks against is baked into the executor at run start, never a per-call keyword that can be omitted"
    - "Recoverable denial: a guard returns a retry instruction that carries the valid alternatives in its payload, so the model self-corrects in-run instead of ending the run"
    - "notice vs guardrail: degraded-but-continuing news gets its own AgentEvent type, keeping 'a control denied something' unambiguous"

key-files:
  created: []
  modified:
    - src/relay/agent.py
    - src/relay/models.py
    - src/relay/prompts.py
    - tests/test_guardrails.py

key-decisions:
  - "retrieved_ids is passed by reference and mutated in run_ticket after the offloaded call returns — the executor sees ids the moment they are retrieved, and the set is never written from the worker thread"
  - "The set is updated on the event loop (not inside the tool thread), so no lock is needed and the milestone's shared-mutable-state pitfall does not apply"
  - "retrieved_ids=None means 'no run to check against' (the MCP path) and skips the check; an empty set is a real state that denies — mirroring UNBOUND vs a real ticket id"
  - "The denial payload carries sorted(retrieved_ids) as structured data, not just prose, because that is what makes in-run recovery mechanically possible"
  - "Degradation emits a notice before the tool_result (cause before effect), matching the guardrail ordering already in the loop"

patterns-established:
  - "A denial test is only complete with a recovery test that proves the run still reaches a terminal action"

requirements-completed: [RAG-04, RAG-05]

# Metrics
duration: 22min
completed: 2026-08-10
---

# Phase 3 Plan 05: Citation Guard and Degradation Notice Summary

**A reply may now only cite source ids `search_docs` actually returned during that run — a fabricated cite is denied with a retry instruction naming the valid ids, and the run recovers in-run rather than dying `ended_without_action`; a Voyage fallback surfaces as a distinct `notice` event.**

## Performance

- **Duration:** ~22 min
- **Tasks:** 2 (3 commits — test → feat for the TDD task, one feat for task 2)
- **Files modified:** 4
- **Test suite:** 186 → 195 passing; `ruff check src tests` clean; `grep -c 'async with' src/relay/agent.py` == 0

## Accomplishments

**Task 1 — citation guard (RED `fe788e0` → GREEN `b97783b`)**

- `bind_to_ticket(ticket_id, retrieved_ids=None)` takes the run's id set as a **constructor** argument. The returned executor's signature is still `(spec, name, raw_input, policy)`, so `test_the_agent_loop_takes_no_binding_argument_to_forget` stays green and there is no per-call keyword anyone can forget.
- `_execute_guarded` gained keyword-only `retrieved_ids: set[str] | None = None` and a subset check placed after the ticket-binding check, before `spec.execute`. Return arity is still `tuple[str, bool]`.
- Denial payload: `{"error": <retry instruction naming the retrieved ids>, "denied_by": "citation", "missing_citations": [...], "retrieved_ids": [...]}`.
- `run_ticket` builds `retrieved_ids: set[str] = set()`, passes it to `bind_to_ticket`, and grows it from `payload["results"]` after each successful `search_docs` — on the event loop, after `asyncio.to_thread` returns.
- A `citation_violation` branch mirrors `binding_violation`: `logger.warning("guardrail.citation_unretrieved", ...)` plus a `guardrail` event (`guard="citation"`) emitted **before** the tool_result. `relay.tool.citation_violation` was added as a span attribute.

**Task 2 — degradation notice, prompt, model comment (`96244c5`)**

- On a `search_docs` result with `degraded: true`, `run_ticket` emits `AgentEvent(type="notice", data={"kind": "retrieval_degraded", "tool", "retrieval_mode", "results"})` and logs `retrieval.degraded`. The run continues; the keyword baseline (no key, `degraded: false`) emits nothing.
- `SYSTEM_PROMPT`'s `send_reply` bullet now instructs the model to pass the `id` of every result it relied on as `citations`, and states that an uncited-source id is rejected and must be retried.
- `AgentEvent.type` documents `notice` and why it is distinct from `guardrail`.

## The trap this plan had to avoid

`agent.py` sets `resolved_via` only when `not is_error and block.name in TERMINAL_TOOLS`, so a denied `send_reply` leaves the run with no terminal action and yields `error: ended_without_action` — which fails the eval harness's `action_ok` and would regress 03-06's gate. Two things prevent it here: the denial is worded as a retry instruction, and — the load-bearing half — it returns `retrieved_ids` as structured data so the model has something concrete to retry with. `test_run_recovers_after_citation_denial` drives a fake client that reads the denial payload and retries with exactly those ids; it asserts the run reaches `resolution` via `send_reply` with exactly one reply row.

## Test discipline: mutation checks run

Every mutation was applied to a working tree copy and reverted immediately after.

| # | Mutation | Expected | Observed |
|---|----------|----------|----------|
| A | Citation check disabled (`if False and name == "send_reply"...`) | denial + event + recovery tests fail | 4 citation tests FAILED, 2 passed (the two "must NOT enforce" tests, correctly) |
| B | Denial returns `"retrieved_ids": []` — denial still fires, but names no valid ids | recovery test fails, guardrail-event test still passes | `test_run_recovers_after_citation_denial` FAILED ("the denial named no retrieved ids, so the model had nothing to retry with"); `test_citation_denial_emits_guardrail_event` still PASSED — clean isolation |
| C | `return` after the citation guardrail event (denial ends the run) | recovery test fails | `test_run_recovers_after_citation_denial` FAILED (never reaches `resolution`) |
| D | Notice emitted on every `search_docs` result | baseline test fails | `test_keyword_baseline_emits_no_notice` FAILED |
| E | Notice never emitted | degraded test fails | `test_retrieval_degraded_emits_notice` FAILED |
| F | Citation instruction removed from `SYSTEM_PROMPT` | prompt test fails | `test_the_system_prompt_tells_the_model_to_cite_retrieved_ids` FAILED |

**Honest caveats on load-bearing-ness:**

- Mutations B and C are what make the recovery test meaningful — it fails both when the denial stops carrying the valid ids and when the denial ends the run. That is the property the plan asked for.
- What it *cannot* test: whether a **real** Claude model gives up on the denial's wording. A fake client reads the payload, not the prose. Recoverability of the wording itself is only provable by 03-06's paid eval — this test proves the mechanism, not the persuasion.
- `test_the_system_prompt_tells_the_model_to_cite_retrieved_ids` is a string assertion over `SYSTEM_PROMPT`. It fails if the instruction is deleted (mutation F) but proves nothing about model behaviour. It is included because the whole guard goes silently unexercised if the model never cites; it is the weakest test in this plan and is labelled as such rather than presented as behavioural coverage.
- Mutations A and C also knocked out neighbouring tests (A: the guard-event test loses its event; C: the `return` skips the tool_result event entirely). B is the cleanly isolated one.

## Invariants verified

- `inspect.signature(bind_to_ticket(id, set()))` params == `["spec", "name", "raw_input", "policy"]` — asserted by a new test as well as the existing SEC-04 one.
- `_execute_guarded` still returns `tuple[str, bool]`; `src/relay/mcp_server.py` and `src/relay/evals.py` have **zero** diff across this entire phase (`git diff --stat 2f0c39a..HEAD`).
- `retrieved_ids=None` on the MCP path skips the check: `test_the_unbound_path_does_not_enforce_citations` sends a fabricated cite through `_execute_guarded` with no binding and it executes normally.
- D-12 back-compat: `[] ⊆ retrieved` always passes. The ~7 pre-existing `send_reply` test files were not touched and all still pass.
- `grep -c 'async with' src/relay/agent.py` == 0.
- SSE needs no change — `main.py:234` serializes `f"event: {event.type}..."` generically, so `notice` rides for free. `evals.extract_outcome` has no `else` branch, so it ignores the new type.

## Deviations from Plan

### Auto-fixed Issues

None. No Rule 1/2/3 fixes were needed.

### Additions beyond the plan's task list

**1. [Rule 2 - Coverage] Three tests the plan did not enumerate**
- **Found during:** Task 1
- **Why:** the plan's four tests covered denial, event ordering, recovery and degradation, but nothing pinned the two *must-not-enforce* directions or the empty-set state — the exact places an over-eager guard would break the MCP surface or D-12.
- **Added:** `test_the_citation_set_is_also_not_a_per_call_argument_to_forget` (empty set denies; executor signature unchanged), `test_the_unbound_path_does_not_enforce_citations`, `test_a_reply_with_no_citations_is_not_denied`.
- **Commits:** `fe788e0`, `b97783b`

**2. [Rule 2 - Coverage] `test_keyword_baseline_emits_no_notice`**
- **Found during:** Task 2
- **Why:** without it, "emit a notice on every search_docs result" passes the degraded test. Mutation D confirms it is the only thing that catches that.
- **Commit:** `96244c5`

**3. Agent module docstring gained a phase-3 guardrail entry**, matching the existing phase-1/phase-2 entries. Cosmetic, per the codebase's "docstrings explain why, referencing the phase" convention.

### Notes

- The `keyword_baseline` fixture is local to the new tests (requested by name), not module-autouse, so no pre-existing test in the file changed behaviour.
- On a developer machine with `VOYAGE_API_KEY` set, `conftest._no_outbound_http` makes Voyage fail, so unrelated runs through `search_docs` now also emit a `notice`. This is additive and asserted-around; CI has no key and stays on the silent baseline.

## Known Stubs

None. `citations` is still validated-but-not-persisted (a 03-04 decision, restated here so it is not mistaken for a stub introduced by this plan): the DB schema stays untouched until something reads citations back, and the guard is enforced in the executor rather than at the storage layer.

## Threat Flags

None. No new network endpoints, auth paths, file access, or schema changes. The three threats this plan mitigates (T-03-12 fabricated cite, T-03-13 denial-ends-run, T-03-14 silent fallback) are covered above; T-03-15 (cross-run leakage) is structurally prevented — the set is created inside `run_ticket` and captured by that run's closure, never on the shared registry, which `test_concurrent_runs_do_not_cross_bind` already exercises for the sibling binding.

## Self-Check: PASSED

- `src/relay/agent.py` — FOUND (contains `retrieved_ids`, `denied_by": "citation"`, `retrieval_degraded`)
- `src/relay/prompts.py` — FOUND (contains `citations`)
- `src/relay/models.py` — FOUND (contains `notice`)
- `tests/test_guardrails.py` — FOUND (contains `citation`)
- Commits `fe788e0`, `b97783b`, `96244c5` — all FOUND in `git log`
- `.venv/bin/python -m pytest -q` → 195 passed; `ruff check src tests` → clean

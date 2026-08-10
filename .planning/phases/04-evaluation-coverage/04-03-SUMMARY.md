---
phase: 04-evaluation-coverage
plan: 03
subsystem: evals
tags: [guardrails, citations, evals, falsifiability]
requires: [04-01, 04-02]
provides:
  - "run_ticket(seed_citation_denial=) — eval-only, keyword-only, default-off probe"
  - "run_case(seed_citation_denial=) — paid-dispatch arming path, report-only"
  - "free mechanism test proving a seeded citation denial is recoverable in-run"
affects:
  - src/relay/agent.py
  - src/relay/evals.py
  - tests/test_evals.py
tech-stack:
  added: []
  patterns:
    - "eval-only probe as a keyword-only default-off param; mutates only per-run state"
key-files:
  created: []
  modified:
    - src/relay/agent.py
    - src/relay/evals.py
    - tests/test_evals.py
decisions:
  - "D-08 hook drops the TOP HIT's located id after the grow-step, not before, so a later hit re-adding it as an anchor cannot undo the discard"
  - "The arming flag is deliberately NOT an argparse flag on relay.evals — a CLI switch could arm the threshold-gated 12-case suite"
  - "Mutation A is exercised as `retrieved_ids: []` rather than key-deletion; deletion fails earlier on a KeyError and never reaches the recovery path"
metrics:
  tasks: 3
  commits: 4
  duration: ~25m
  completed: 2026-08-11
  tests: 264 passed (baseline 261, +3)
---

# Phase 04 Plan 03: Denial-Recovery Seeding Hook Summary

Added the D-08 eval-only probe that forces exactly one real citation denial by dropping a genuinely-retrieved id from a run's accept-set, closing 03-REVIEW WR-10's unfalsifiability with a hook-dependent mechanism test.

## What Was Built

**`src/relay/agent.py` — `seed_citation_denial` hook.** `run_ticket` gained a keyword-only, default-`False` param. When armed, the `search_docs` grow-step (now at `agent.py:336-355`) fires once: it discards `results[0]["id"]` from the per-run `retrieved_ids` set and injects `"__seeded_missing__"` so the set stays non-empty, then logs `guardrail.citation_denial_seeded`.

> **CORRECTION (post-review, `aeeccd6`).** This originally read "a local `seed_armed` flag
> makes it at-most-once per run." That flag no longer exists, and the mechanism it
> described **was** the CR-02 defect: the discard survived a second *hit* but not a second
> `search_docs` *call*, so a fake that searched twice then cited the first search's top hit
> produced a seeded log line, zero guardrails, and a clean `send_reply` — an armed hook
> with no signal. The drop is now a `seeded_drops` set held for the run's life and
> subtracted after every grow.

The discard is applied **after** the whole `for hit in results` grow loop, not inside it. This matters: retrieval returns whole files with an `anchors` list, so a later hit could re-add the same id as one of its anchors and silently undo an in-loop discard.

The citation guard (`agent.py:150-172`) and `bind_to_ticket` were not touched. `main.py` has no reference to the flag.

**`src/relay/evals.py` — arming path.** `run_case` gained the same keyword-only, default-`False` param, forwarded to its `run_ticket` call. `run_evals`' 12-case loop (`evals.py:287`) never arms it, and the `pass_rate < args.threshold` gate (`evals.py:352-353`) is unchanged — the seeded case is report-only by construction.

Deliberately **not** exposed as an argparse flag. A `--seed-denial` switch on `python -m relay.evals` could be passed to the gated run and turn a report-only probe into a threshold failure; the paid dispatch arms it by calling `run_case(..., seed_citation_denial=True)` directly.

**`tests/test_evals.py` — six tests** (three at plan time; CR-02/CR-03 added three more). Two contract tests (hook is KEYWORD_ONLY/default-`False`; `main.py` never arms it) and the mechanism test `test_seed_denial_hook_denies_then_fake_recovers`.

## The Blocker That Was Avoided

The plan reviewer flagged that copying `RecoveringFakeClient` from `tests/test_guardrails.py` would produce an unfalsifiable test. That fake cites `FABRICATED_CITE = "refunds-2019.md#store-credit"` — an id absent from `kb/` entirely, so it is denied whether the hook is armed, a no-op, or deleted.

`HookDependentRecoveringClient` (new, in `tests/test_evals.py`) instead reads its first citation out of the live tool result:

```python
self.cited_first = last["results"][0]["id"]
```

That is the exact id the hook discards. Armed, it is denied; unarmed, it is valid and nothing is denied. Only the recovery step mirrors the original fake — it reads `retrieved_ids` back out of the denial payload rather than being scripted with the answer.

Two assertions carry this beyond "a denial happened":
- `guardrails[0].data["missing_citations"] == [client.cited_first]` — the denial is about the id the hook dropped, not some other id.
- `client.cited_first not in client.recovered_with` — the accept-set was genuinely narrowed, not merely reported on.

## Mutation Testing

Both named mutations were applied to `src/relay/agent.py`, run, and reverted.

| Mutation | Change | Result |
|---|---|---|
| **B** — hook is a no-op | replaced the one-shot `retrieved_ids.discard(dropped)` with `pass` (**that line no longer exists as of `aeeccd6`**; the equivalent mutation now neutralises the `seeded_drops` subtraction) | **FAILED** as required: `assert [e.data["guard"] for e in guardrails] == ["citation"]` → `assert [] == ['citation']`. The cited id stayed in the accept-set, so no denial fired. |
| **A** — denial names no valid ids | `"retrieved_ids": []` in the denial payload | **FAILED** as required: `assert client.recovered_with` → `assert []`. The fake was starved and had nothing to retry with. |
| **A (variant)** — key deleted outright | removed `"retrieved_ids"` from the payload | Also fails, but with `KeyError: 'retrieved_ids'` at `agent.py:417` where the guardrail *event* reads the same key — it never reaches the recovery path. Recorded in the inline `# mutation:` comment so the reproducible variant is the `[]` one. |

Mutation B failing is the load-bearing result: it is the check that the blocker did not reappear. After each mutation `git diff --quiet src/relay/agent.py` confirmed a byte-exact restore.

## Verification

| Check | Result |
|---|---|
| `pytest -q` | 264 passed (baseline 261, +3; floor >= 261 met) |
| `ruff check src tests` | All checks passed |
| `run_ticket` param is KEYWORD_ONLY, default `False` | pass |
| `run_case` param is KEYWORD_ONLY, default `False` | pass |
| `grep seed_citation_denial src/relay/main.py` | no match (production never arms it) |
| `git diff --quiet .github/workflows/ci.yml src/relay/mcp_server.py` | clean (frozen files byte-unchanged) |
| `_execute_guarded` return arity | still `tuple[str, bool]` |
| `grep -c 'async with' src/relay/agent.py` | 0 |
| All new tests pin `settings.voyage_api_key = None` | yes, via the existing `keyword_baseline` fixture — no outbound Voyage call, no spend |

No existing test was modified.

## Deviations from Plan

**1. [Rule 3 — Blocking] Mutation A run as `retrieved_ids: []` rather than key-deletion.**
- **Found during:** Task 3 mutation testing
- **Issue:** The plan specified deleting the `"retrieved_ids"` key from the denial payload. Doing so raises `KeyError` at `agent.py:417`, where the guardrail *event* reads the same key — the test fails, but before the recovery path executes, so it does not demonstrate the starvation property the mutation exists to demonstrate.
- **Fix:** Ran the faithful variant (`"retrieved_ids": []`) which keeps the event emitter working and starves the fake exactly as intended. Both variants are recorded in the test's inline `# mutation:` comment.
- **Files modified:** `tests/test_evals.py` (comment only)
- **Commit:** 5d5ec26

**2. [Rule 2 — Correctness] The discard is applied after the grow loop, not inside it.**
- **Found during:** Task 1
- **Issue:** The plan's sketch showed the drop guarded inside the grow-step. Because each hit contributes its whole `anchors` list, a second hit from the same doc could re-add the dropped id, silently disarming the probe.
- **Fix:** Placed the hook after the `for hit in ...` loop completes, so the discard is final. Reason documented in an inline comment.
- **Files modified:** `src/relay/agent.py`
- **Commit:** f8f4b19

**3. [Rule 2 — Correctness] Arming is not exposed via argparse.**
- **Found during:** Task 2
- **Issue:** "Provide the wiring so the paid dispatch can arm one case" could be read as adding a CLI flag. A CLI flag on `relay.evals` could be passed to the threshold-gated run, converting a report-only probe into a CI failure — directly against D-03/D-08.
- **Fix:** Kept the arming as a programmatic `run_case(..., seed_citation_denial=True)` call. Rationale recorded in the `run_case` docstring.
- **Files modified:** `src/relay/evals.py`
- **Commit:** d1bec46

## Not Done (deliberate)

The **paid real-model recovery dispatch was not run.** Per the hard constraints this plan proves the *mechanism* only. What is now established: given a citation denial naming valid ids, a client that reads that payload can recover to a terminal `send_reply`. What is **not** established: that a real Claude model does so. That remains VALIDATION row 8 — a manual `evals.yml` dispatch to be reported honestly either way (`guard="citation"` → `resolution via=send_reply`, or `ended_without_action`).

`.github/workflows/evals.yml` was not modified; the plan's `files_modified` scoped this plan to the three source files.

## Known Stubs

None.

## Threat Flags

None. The hook adds no network surface, no schema change, and no new trust boundary — it narrows one in-memory per-run set and is unreachable from `main.py`.

## Commits

| Commit | Type | Description |
|---|---|---|
| dd60065 | test | failing contract test for the hook (RED) |
| f8f4b19 | feat | the `seed_citation_denial` hook on `run_ticket` (GREEN) |
| d1bec46 | feat | thread the flag through `run_case` |
| 5d5ec26 | test | the hook-dependent mechanism test |

## Self-Check: PASSED

- `src/relay/agent.py` — FOUND, contains `seed_citation_denial`
- `src/relay/evals.py` — FOUND, contains `seed_citation_denial=seed_citation_denial`
- `tests/test_evals.py` — FOUND, contains `test_seed_denial_hook_denies_then_fake_recovers`
- Commits dd60065, f8f4b19, d1bec46, 5d5ec26 — all FOUND in `git log`

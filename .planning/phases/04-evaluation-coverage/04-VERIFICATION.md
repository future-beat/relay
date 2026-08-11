---
phase: 04-evaluation-coverage
verified: 2026-08-11T16:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 4: Evaluation Coverage Verification Report

**Phase Goal:** The eval harness measurably proves the retrieval and guardrail claims, not just asserts them
**Verified:** 2026-08-11T16:00:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Method

This is a heavy post-review fix cycle (3 Criticals + 9 Warnings from `04-REVIEW.md`, all
claimed fixed). Rather than trust the SUMMARY correction blocks, I:

1. Read the final state of every touched file (`evals.py`, `retrieval_eval.py`,
   `retrieval_report.py`, `agent.py`, `evals.yml`, `tests/test_evals.py`) directly, not
   the diffs or the SUMMARY prose.
2. Ran the full free suite and lint myself (not trusting the reported `288 passed`).
3. Independently re-applied four of the reviewer's named mutations myself (not
   re-running the SUMMARY's mutation table — writing my own sed-based reversions of
   CR-01, CR-02, and the SEC-04/citation-subset checks), confirmed each fails the
   relevant test, then confirmed the tree was restored byte-clean and the full suite
   was still green afterward.
4. Cross-checked git commit timestamps against the eval artifact file mtimes to confirm
   the paid runs cited as evidence actually happened after the fix commits landed.
5. Did not run any paid eval myself (per instructions) — paid-run evidence rests on the
   already-produced JSON artifacts, which I read and cross-checked directly rather than
   on the SUMMARY's transcription of them (except the one probe with no artifact, noted
   below).

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Running the eval harness reports recall@k and MRR for a labeled retrieval set alongside the existing ticket results | VERIFIED | `run_evals()` embeds `retrieval_metrics` (`src/relay/evals.py:433`), computed by `retrieval_eval.py`'s pure `score_rows`/`recall_from_scores`/`mrr_from_scores` over `evals/retrieval.jsonl` (12 rows, 11 scored). `print_summary` renders it (`evals.py:456`). Delivery is now test-covered (`test_run_evals_report_carries_the_retrieval_metrics_block`, closing WR-07 — deleting `retrieval_metrics` from the report used to leave the suite green). Real paid numbers exist: `eval_results/retrieval-20260811T020114Z.json` and the `retrieval_metrics` block inside `eval_results/eval-20260811T071058Z.json` (semantic recall@1/MRR = 1.00, locator@1 = 0.90) |
| 2 | The 12-ticket suite passes at or above its pre-change baseline and stays above the CI threshold | VERIFIED | Gate unchanged: `main()` still checks only `report["pass_rate"] < args.threshold` (`evals.py:518`), and `retrieval_metrics` contributes to no field feeding it (confirmed by reading, matches review finding #3 "Verified as correct"). CI's gated step now runs `ANTHROPIC_API_KEY` only (keyword mode, matching the historical 0.8 baseline) per WR-08's fix, with a dedicated test (`test_the_gated_eval_step_does_not_receive_the_voyage_key`) asserting the workflow file's shape. Paid evidence: `eval_results/eval-20260811T071058Z.json` reads `pass_rate: 1.0, passed: 12, cases: 12` — file mtime (15:10) is after the KB-gap-fix commit `0b74821` (15:09:20), confirming the evidence postdates the fix it's evidence for |
| 3 | A prompt-injection golden case fails if the server-side `ticket_id` guard is removed | VERIFIED | `test_injection_ticket_binding_guard_fires` (`tests/test_evals.py:766`) asserts the `guardrail` event fires AND `SELECT COUNT(*) FROM replies WHERE ticket_id=99` is 0 AND the run's own ticket is written. I independently mutated `agent.py` (replaced the `denied_by == "ticket_binding"` check with `False`) and reran this test: it failed (`assert 0 == 1`, guard never fired). Reverted; `git diff --quiet` confirmed clean |
| 4 | A deterministic citation-faithfulness check (no LLM judge) fails if a reply cites an unretrieved chunk | VERIFIED | `test_citation_faithful_cited_subset_retrieved` (`tests/test_evals.py:828`) asserts `cited ⊆ retrieved` over an 8-result composed report, with a non-vacuity pin (`len(cited) >= 5`) and a fabricated-citation negative control. I independently mutated `extract_outcome` (neutered the `retrieved.update(...)` calls) and reran: it failed (`refund-window: cited ['billing.md'] but retrieved []`). Reverted; `git diff --quiet` confirmed clean. No LLM judge involved — pure set-membership arithmetic |

**Score:** 4/4 truths verified

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|---|---|---|---|---|
| EVAL-01 | 04-01 | Labeled retrieval set + recall@k/MRR, no regression | SATISFIED | `evals/retrieval.jsonl` (12 rows) + `retrieval_eval.py` + report field, all free-tier tested; document-level granularity and query-source dependence disclosed in the module docstring and report payload (`granularity`, `query_source`, `ticket_derived`) per the WR-01/WR-02 fixes |
| EVAL-02 | 04-02 | Prompt-injection golden case asserts SEC-04 guard fires | SATISFIED | `test_injection_ticket_binding_guard_fires`, mutation-verified independently (above) |
| EVAL-03 | 04-03 | Deterministic citation-faithfulness check, no LLM judge | SATISFIED | `test_citation_faithful_cited_subset_retrieved` + D-08 seeding hook mechanism tests, mutation-verified independently (above) |

No orphaned requirements — `.planning/REQUIREMENTS.md` maps only EVAL-01/02/03 to Phase 4, all three claimed by plans 04-01/02/03.

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `evals/retrieval.jsonl` | 12 labeled query→id rows | VERIFIED | Present, all non-empty `relevant` ids validate against `kb/index.json` doc/anchor names |
| `src/relay/retrieval_eval.py` | Pure recall@k/MRR/locator_precision module | VERIFIED | Present; scores via the shipped `retrieve()`, never re-ranks; single-pass `score_rows` feeds all derived metrics (WR-03/WR-06 fix) |
| `src/relay/evals.py::retrieval_metrics`/`safe_retrieval_metrics` | Report-only field, mode read from `retrieve()` not from credential presence, exception-isolated | VERIFIED | `_score_block` derives `mode` via `observed_mode(scores)`; `safe_retrieval_metrics` wraps it in `try/except` (WR-04 fix). Confirmed by my own CR-01 mutation |
| `src/relay/evals.py::extract_outcome`/`CaseResult` | Records `guardrails`, `denial_recovery`, `seeded_denial` | VERIFIED | Fields present; `denial_recovery()` distinguishes `not_denied`/`recovered`/`unrecovered`; `run_case` forwards `seed_citation_denial` and it is now test-covered (`test_run_case_forwards_the_seed_denial_flag`, closing CR-03's untested arming path) |
| `src/relay/agent.py` — `seed_citation_denial` hook | Sticky, run-lifetime drop set; never offers a fabricated id | VERIFIED | `seeded_drops: set[str]` subtracted after every grow-step (not one-shot); no sentinel injected — confirmed by reading and by my CR-02 mutation |
| `src/relay/retrieval_report.py` | Standalone report-only semantic-recall entry point | VERIFIED | Present, `continue-on-error`-safe by construction (`safe_retrieval_metrics`), test-covered |
| `.github/workflows/evals.yml` | Gated step keyword-only; semantic recall in a decoupled report-only step; threshold passed via env not shell interpolation | VERIFIED | Confirmed by reading the file directly; `THRESHOLD` passed via `env:`, gated step has no `VOYAGE_API_KEY`, report-only step has `continue-on-error: true` |
| `kb/billing.md` + `kb/index.json` | Re-upgrade content gap closed, index rebuilt | VERIFIED | New sentences present in `billing.md`; index loads with `unavailable_reason=None` (fresh, not stale) and its embedded doc text matches the new KB content |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `run_evals()` | `retrieval_metrics()` | `asyncio.to_thread(safe_retrieval_metrics)` at `evals.py:423` | WIRED | Off the event loop (WR-03 fix), exception-isolated (WR-04 fix), test-covered |
| `run_ticket(seed_citation_denial=)` | `run_case(seed_citation_denial=)` | explicit forward at `evals.py:353` | WIRED | Now test-covered both directions (CR-03 fix); no argparse flag exists (deliberate, D-08) |
| `main.py`/`mcp_server.py` | `seed_citation_denial` | (absence) | NOT_WIRED (intentional) | Package-wide `rglob` containment test (`test_no_production_module_references_the_seed_denial_hook`) + runtime test that an unarmed run never logs the seed event (WR-09 fix) |
| `.github/workflows/evals.yml` gated step | `VOYAGE_API_KEY` | (absence) | NOT_WIRED (intentional) | WR-08 fix; asserted by `test_the_gated_eval_step_does_not_receive_the_voyage_key` |

### Behavioral Spot-Checks (self-run, not trusting SUMMARY)

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full free suite | `.venv/bin/python -m pytest -q` | 288 passed | PASS |
| Lint | `.venv/bin/ruff check src tests` | All checks passed | PASS |
| CR-01 regression (mode-label reversion) | mutated `evals.py`, reran `test_report_mode_is_keyword_when_a_keyed_run_has_no_usable_index` | FAILED as required, then restored clean | PASS (mutation caught) |
| CR-02 regression (one-shot discard) | mutated `agent.py`, reran `test_seed_denial_survives_a_second_search` | FAILED as required (`[] == ['citation']`), then restored clean | PASS (mutation caught) |
| EVAL-02 regression (guard removed) | mutated `agent.py`, reran `test_injection_ticket_binding_guard_fires` | FAILED as required (`0 == 1`), then restored clean | PASS (mutation caught) |
| EVAL-03 regression (retrieved-ids never populated) | mutated `evals.py`, reran `test_citation_faithful_cited_subset_retrieved` | FAILED as required, then restored clean | PASS (mutation caught) |
| Post-mutation tree state | `git status --short` | only pre-existing untracked planning docs, no source diff | PASS |

### Anti-Patterns Found

None. `grep -n "TBD\|FIXME\|XXX\|TODO\|HACK\|PLACEHOLDER"` over every file touched by this phase returns nothing.

### Timestamp / Ordering Check (paid-evidence validity)

| Fix commit | Timestamp | Paid artifact it's evidence for | Artifact mtime | Order OK? |
|---|---|---|---|---|
| `de65e45` (CR-01) | 2026-08-11 01:06:40 | `retrieval-20260811T020114Z.json` | 10:01 | after |
| `aeeccd6` (CR-02) | 2026-08-11 01:08:45 | same | 10:01 | after |
| `a15c7d4` (CR-03) | 2026-08-11 01:11:54 | same | 10:01 | after |
| `f0f8049`..`f649388` (WR-05/03/04/07/08/01/02/09) | 09:45–09:55 | `eval-20260811T020257Z.json` (11/12) | 10:02 | after |
| `0b74821` (KB gap + workflow hardening) | 15:09:20 | `eval-20260811T071058Z.json` (12/12) | 15:10 | after |

All cited paid evidence postdates the fixes it is offered as evidence for.

## Not independently verifiable — flagged, not a gap

**The real-model citation-denial recovery probe (`04-03-SUMMARY.md`, "Paid real-model
recovery probe — RUN 2026-08-11") has no persisted JSON artifact.** Unlike the semantic
recall numbers and the 11/12 → 12/12 graded runs (both backed by files in
`eval_results/` that I read and cross-checked directly), this specific run — armed via
a direct `run_case(..., seed_citation_denial=True)` call outside the normal CLI paths —
left no file on disk to audit; the transcript in the SUMMARY is the only record. This is
exactly the class of claim this phase exists to stop shipping unfalsifiably, so it is
worth naming even though it does not gate the phase: the *mechanism* it demonstrates
(a real model reading `retrieved_ids` out of a denial payload and retrying) is already
proven independently by the mutation-tested fake-client tests (`test_seed_denial_hook_denies_then_fake_recovers`,
`test_seed_denial_survives_a_second_search`), and the D-08 probe is explicitly
paid-optional / non-gating by design (it is not one of the four ROADMAP success
criteria — it closes a Phase 3 finding, not a Phase 4 requirement). Recommendation: next
time this probe is run, save its output as an `eval_results/*.json` artifact the way the
other paid runs are, rather than transcribing console output into a SUMMARY.

This does not affect the phase's pass status: EVAL-03's requirement ("deterministic...
no LLM judge") is satisfied by the free, mutation-tested subset check regardless of
whether the real-model probe artifact exists.

### Human Verification Required

None. All four ROADMAP success criteria are deterministically verifiable and were
verified against the actual code (including by independent mutation), not by trusting
SUMMARY claims. The gap noted above is informational, not a human-verification item —
there is nothing a human needs to click through or observe; a future paid run should
simply persist its artifact.

### Gaps Summary

No gaps. All three Criticals and nine Warnings from `04-REVIEW.md` are fixed in the
code as it stands (confirmed by reading, not by trusting the SUMMARY's CORRECTION
blocks), the fixes are load-bearing (confirmed by independently re-deriving four of
them via my own mutations, not the reviewer's or the SUMMARY's), the full free suite
passes (288, self-run) with clean lint, and the paid evidence cited postdates every fix
it is offered as evidence for. The one thing not independently auditable — the
real-model recovery probe's transcript — is flagged above but does not block the phase
goal, which is fully met by the free, deterministic, mutation-verified checks.

---

_Verified: 2026-08-11T16:00:00Z_
_Verifier: Claude (gsd-verifier)_

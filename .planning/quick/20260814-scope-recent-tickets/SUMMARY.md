---
slug: scope-recent-tickets
date: 2026-08-14
status: complete
---

# Summary: stop `lookup_customer` putting other people's words in the model's context

Executed from `SCOPE.md`. Branch `fix/recent-tickets-scope` off `main` @ `3619e27`.

## What changed

| Commit | Change |
|---|---|
| `1f49789` | `lookup_customer` selects `id, status, created_at` — `subject` is gone |
| `22ef08f` | the withheld-harvest takes free text only, never Relay's own vocabulary (NF-4) |
| `e0e7410` | a test pins the judge's context to the agent's own payload |
| `1d12195` | corrected the comments the payload change made false |

`subject` was the only free-text field in the payload. Everything else is enumerable or a
fictional constant from `SEED_CUSTOMERS`. The Try-it subject box is editable, so
`recent_tickets` had been accumulating strangers' typed text and handing it to the model,
which restated it into prose published on the keyless `GET /runs/{uid}`.

## The security property

**The anonymous walk is now closed STRUCTURALLY for third-party ticket text.**
`GET /metrics` -> harvest `run_uid` -> keyless `GET /runs/{uid}` returns no other
visitor's typed words, because those words never enter the model's context. Proven at the
source (the lookup returns that person's ticket *by id* and no text), not at the response.

The executor made a judgement worth recording: three drill-down tests used the
presence-then-absence idiom — assert the third party's subject reached the raw
`run_events` rows, then assert it is absent from the response. That anti-vacuity half
became **false by construction**, and the failure *was* the fix landing. It replaced them
with `_prove_the_earlier_subject_never_entered_the_run`, noting in the docstring that
presence-then-absence proves a mask, and the claim here is that there is nothing to mask.

The looked-up customer's name, plan and signup date are still covered only by the literal
mask — a floor, porous to paraphrase. Those are four fictional personas hardcoded in a
public repo.

## Eval measurement (paid, 3 runs, ~$0.83 total)

| Run | Code | Pass | Mean quality | Agent cost |
|---|---|---|---|---|
| baseline | unchanged `3619e27` | 11/12 | 4.92 | $0.2870 |
| after #1 | changed | 11/12 | 5.0 | $0.2774 |
| after #2 | changed | **12/12** | 5.0 | $0.2679 |

Reports under `eval_results/{baseline,after,after2}-recent-tickets.json/`.

**No evidence of degradation.** Case-level diff of baseline -> after #1 showed three
moves: `pro-pricing` improved (grounded False->True, q4->5 — it was the baseline's only
failure), and two category reassignments toward `account`, one of which (`webhooks-on-pro`)
failed. A second sample on the same code returned 12/12 with that case correct, so the
category miss was run-to-run variance rather than a systematic drift from the smaller
context. Both after-runs beat the baseline on quality.

**The risk named in SCOPE.md did not materialise.** All four escalation cases
(`refund-monthly`, `2fa-lockout`, `key-suspended`, `salesforce-integration`) passed in all
three runs at quality 5 with identical actions. Removing prior-subject text left the
escalate-if-frustrated signal — count, recency, unresolved — intact.

`evals.py` needed no code change to keep its "give the judge exactly what the agent could
see" comment true: it calls the tool, so the judge's context tracked the payload
automatically. What was missing was anything enforcing it; that is now a test.

## Carried forward

- **NF-2 (the `authored` unmasking oracle) is unexercised, not fixed.** With no subject in
  the payload, `authored` has nothing to exempt and its documented mutation no longer reds.
  Recorded in the test docstring rather than deleted; the argument stays in place for the
  next tool that returns visitor-authored text. The `withheld_from_run` docstring's
  overstatement — that whole-matching makes the oracle impossible — was corrected: it
  blocks bulk unmasking, which is a different claim.
- **NF-1 (sub-token matching: `Hi Mia,` survives when the literal is `Mia Torres`)** is
  untouched, per scope. Now a correctness bug over fictional constants rather than a
  disclosure risk.
- NF-3 (five unguarded properties of the mask) untouched.

## Verification

`pytest -q` -> **424 passed** (floor 420); `ruff check src tests` clean. Mutations run and
confirmed red for every task; the orchestrator independently re-ran the payload mutation
(restore `subject`), which reds 6 tests across `test_tools.py`, `test_evals.py` and the
drill-down suite.

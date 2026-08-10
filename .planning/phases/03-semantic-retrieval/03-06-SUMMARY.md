---
phase: 03-semantic-retrieval
plan: 06
subsystem: retrieval
tags: [calibration, evals, acceptance-gate, retrieval, cost]

# Dependency graph
requires:
  - phase: 03-semantic-retrieval plan 03
    provides: "kb/index.json — the committed vectors the floor is measured against"
  - phase: 03-semantic-retrieval plan 05
    provides: "citation guard + degradation notice, the last behaviour change before the gate"
  - phase: 02-async-data-layer (2f0c39a)
    provides: "the keyword-scorer BEFORE tree the diff is taken against"
provides:
  - "Calibrated settings.retrieval_floor = 0.30, measured against the golden set (D-04)"
  - "Acceptance evidence: 12-case before/after eval diff, 11/12 -> 12/12, no regressions"
  - "Measured cosine table for the golden queries against the committed index"
affects: [phase 4 evals, phase 5 dashboard, any future kb/index rebuild]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Threshold constants are measured outputs with the measurement recorded in the comment beside them, not tuned literals"

key-files:
  created: []
  modified:
    - src/relay/config.py

key-decisions:
  - "retrieval_floor = 0.30 — above every measured off-topic score (max 0.2543) and below the covered-topic band (0.34-0.63)"
  - "The two covered queries measured below 0.30 are not starved: the keyword half of the hybrid union (D-05) still returns their doc, verified by simulation"
  - "The shipped 0.55 placeholder was inert — only 1 of 12 golden queries cleared it, so semantic ranking would have been silently unused"
  - "Voyage query cost stays out-of-band and is NOT tracked by RunBudget this phase (a stated decision, RESEARCH Pitfall 5)"

requirements-completed: [RAG-01, RAG-04, RAG-05]

# Metrics
duration: 25min
completed: 2026-08-10
---

# Phase 3 Plan 06: Floor Calibration and Acceptance Eval Summary

**`retrieval_floor` is 0.30 — measured, not guessed — and the paid 12-case before/after diff went 11/12 (92%) to 12/12 (100%) with zero case regressing, zero errors, and the semantic path live on all 12 runs.**

## The chosen floor: 0.30

Measured with real Voyage query embeddings (`input_type="query"`) against the committed
`kb/index.json` (`voyage-4-lite`, 512-dim, 3 docs). Cosines over L2-normalized vectors.

**Off-topic / uncovered band — must fall BELOW the floor:**

| Query | account | api | billing | top | keyword hits |
|---|---|---|---|---|---|
| `Salesforce integration` | 0.1951 | 0.1686 | 0.1428 | **0.1951** | none |
| `Salesforce CRM sync` | 0.2447 | 0.1549 | 0.1557 | **0.2447** | none |
| `third-party CRM integrations roadmap` | 0.1798 | 0.2125 | 0.2089 | **0.2125** | none |
| salesforce-integration subject+body (whole ticket) | 0.2205 | 0.2543 | 0.1590 | **0.2543** | account:6, billing:4, api:3 |
| control: `how do I bake sourdough bread` | 0.1636 | 0.1756 | 0.1325 | **0.1756** | none |
| control: `kubernetes cluster autoscaling` | 0.1057 | 0.2000 | 0.1472 | **0.2000** | none |

**Covered band — must sit ABOVE the floor (score on the correct doc, model-style query):**

| Case | Query | Score on correct doc |
|---|---|---|
| rate-limits-pro | `API rate limits Pro plan` | 0.6333 (api) |
| pro-pricing | `Pro plan pricing` | 0.5367 (billing) |
| password-reset | `password reset` | 0.5268 (account) |
| webhooks-on-pro | `webhooks availability plan` | 0.5205 (api) |
| 2fa-lockout | `two-factor authentication lost recovery codes lockout` | 0.5001 (account) |
| refund-monthly | `refund policy billing charge` | 0.4606 (billing) |
| key-suspended | `API key suspended` | 0.4080 (api) |
| downgrade-data-loss | `downgrade plan data retention projects` | 0.3946 (billing) |
| sso-config | `SAML SSO configuration` | 0.3876 (account) |
| data-export | `export data` | 0.3408 (account) |
| enterprise-sla | `uptime SLA guarantee` | **0.2659** (api; billing 0.2575) |

**Why 0.30 and not the literal gap.** Read strictly, the plan's window is "between salesforce's
nearest-doc score and the lowest genuinely-relevant score" = **(0.2543, 0.2659)** — 0.012 wide.
That is too narrow to be robust against a model-generated query phrasing. 0.30 was chosen instead
because the one covered query below it (`uptime SLA guarantee`, 0.2659 — a near-uniform 0.2659 /
0.2583 / 0.2575 across all three docs, i.e. the embedding does not really distinguish them) still
gets `billing.md` through the **keyword half of the hybrid union** (D-05). Simulating `retrieve()`'s
union at floors 0.25 / 0.28 / 0.30 / 0.33 / 0.35 / 0.55 confirms this: `enterprise-sla` returns
`billing` at every floor ≥ 0.28, and the only thing 0.30 removes anywhere is marginal noise docs
(`api`+`account` on enterprise-sla at 0.25, `account` on key-suspended at ≤ 0.28). 0.30 buys ~0.046
of margin over the highest off-topic score for the cost of zero starved cases.

**The 0.55 placeholder was actively wrong.** Only `rate-limits-pro` (0.6333) clears it. Every other
golden query would have produced **zero** semantic hits and been ranked keyword-only — the phase's
entire semantic upgrade would have shipped silently inert while all tests passed. This is the concrete
thing D-04's "calibrate, don't guess" caught.

## Before/after 12-case eval diff (the acceptance gate)

Identical model (`claude-sonnet-5`), identical `--concurrency 4 --threshold 0.8`, identical
`evals/golden.jsonl` and `kb/*.md` (`git diff 2f0c39a..HEAD -- kb evals` is `kb/index.json` only).
BEFORE ran on a read-only checkout of `2f0c39a` (Phase 2 tip, keyword scorer, no `retrieval.py`).

| Case | Before action | Before grounded | Before q | Before | After action | After grounded | After q | After |
|---|---|---|---|---|---|---|---|---|
| rate-limits-pro | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |
| refund-monthly | create_escalation | True | 4 | PASS | create_escalation | True | 5 | PASS |
| password-reset | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |
| 2fa-lockout | create_escalation | True | 5 | PASS | create_escalation | True | 5 | PASS |
| webhooks-on-pro | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |
| pro-pricing | send_reply | **False** | 4 | **FAIL** | send_reply | **True** | 5 | **PASS** |
| downgrade-data-loss | send_reply | True | 5 | PASS | send_reply | True | **4** | PASS |
| sso-config | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |
| data-export | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |
| key-suspended | create_escalation | True | 5 | PASS | create_escalation | True | 5 | PASS |
| salesforce-integration | create_escalation | True | 5 | PASS | create_escalation | True | 5 | PASS |
| enterprise-sla | send_reply | True | 5 | PASS | send_reply | True | 5 | PASS |

| | Before | After |
|---|---|---|
| Pass rate | 92% (11/12) | **100% (12/12)** |
| Mean quality | 4.83 | 4.92 |
| Agent cost (excl. judge) | $0.2711 | $0.2939 |
| Harness/agent errors | 0 | 0 |
| Exit code at `--threshold 0.8` | 0 | 0 |

**Every expected action matched in both runs. No case regressed. Category was `ok` for all 24 runs.**

## Honest reading of the two deltas

**`pro-pricing` FAIL → PASS is very likely model nondeterminism, not a retrieval win.** The before
failure was not a retrieval miss — `billing.md` was retrieved in both runs (it is the top doc at
0.5367 semantically and the top keyword hit). The judge failed it on one invented sentence about
*upgrade* behaviour ("none of your existing projects or data will be affected — everything just gets
unlocked"), which the KB only documents for *downgrade*. In the after run the model simply did not
write that sentence. Nothing in this phase's changes targets that claim. **Do not read 92% → 100% as
"semantic retrieval fixed a case."** The defensible claim is the one the gate actually asks for:
**no case regressed, and the after run is at or above baseline on every axis.**

**`downgrade-data-loss` quality 5 → 4 is judge noise** on a case that passed both times, same action,
same grounding.

**A single 12-case run per side is a small sample.** Both deltas are within the run-to-run variance
you would expect from an LLM-graded suite of this size; the strong statement here is the absence of
regressions, not the +8pp.

## The 03-05 risk this run was the only real test of

03-05 flagged that a fake client reads the citation denial's *payload*, not its *prose*, so nothing
proved a **real** model recovers from the denial instead of dying `ended_without_action`.

**Result: no `ended_without_action` in any of the 12 after-run cases (`error` is `null` for all 12).**

But the honest version is narrower than "the risk is cleared": `logging.lastResort` prints WARNING+
to stderr with no logging configured, and the captured stderr for the after run contains **zero**
`guardrail.citation_unretrieved` lines. **The citation denial never fired**, so the real-model recovery
wording was not exercised at all — the risk did not land, and it also was not tested. It stays open
until a run actually trips the guard. What this run does prove is that adding the citation
instruction to `SYSTEM_PROMPT` did not itself cost any case its terminal action.

Same evidence, other direction: zero `retrieval.degraded` and zero `retrieval.voyage_failed` lines,
so **all 12 after-run cases went through the live semantic path**, not the keyword fallback. The diff
is a real before/after on retrieval, not a no-op.

## Residual risk the floor cannot cover

The floor gates the *semantic* half only. `salesforce-integration` returns `[]` because its queries
have **no keyword hits at all** — not because 0.30 is doing the work alone. If the model passes the
whole ticket body as the query instead of a topical phrase, that ticket scores keyword hits
(`account:6, billing:4, api:3`) and the union returns docs regardless of the floor. On a 3-doc corpus
this is mostly harmless (the model reads the doc and still escalates, as it did in both runs), but on
a larger KB the keyword half is the weaker link, not the floor.

## Voyage cost decision (restated, not an oversight)

Voyage query embeddings are **out-of-band** and **not** counted by `RunBudget` or `/metrics` this
phase. Indexing is once-off; per-query cost is effectively $0 inside the free tier. This is the
documented T-03-17 `accept` disposition (RESEARCH Pitfall 5) — a decision, not an accident. The
per-run dollar ceiling still applies to the Claude half of every run.

## Deviations from Plan

### Auto-fixed Issues

None. No Rule 1/2/3 fixes were needed.

### Judgement calls beyond the plan text

**1. Floor chosen outside the plan's literal window**
- **Found during:** Task 1 calibration
- **Issue:** the plan's stated window (between salesforce's nearest-doc score and the lowest relevant
  score) measured out at (0.2543, 0.2659) — 0.012 wide, knife-edge against query phrasing variance.
- **Resolution:** picked 0.30, above that window, after simulating the union at six candidate floors
  and confirming the one case it moves below-floor (`enterprise-sla`) still gets its doc via the
  keyword half. Documented in the `config.py` comment and above.
- **Commit:** `199f822`

**2. Calibration used two query shapes, not one**
- Golden `subject` / `subject+body` are not what `search_docs` actually receives (the model sends a
  topical phrase), so a second pass measured 16 model-style queries plus two deliberately off-topic
  controls. Both passes are reported; the floor was picked against the model-style band.

## Verification

- `python -m relay.evals --concurrency 4 --threshold 0.8` → exit 0, 12/12 (after run, current tree)
- `.venv/bin/python -m pytest -q` → **195 passed** (VOYAGE_API_KEY not read by the suite)
- `.venv/bin/ruff check src tests` → clean
- `settings.retrieval_floor == 0.30`; the comment no longer says "placeholder"

## Known Stubs

None.

## Threat Flags

None. No new endpoints, auth paths, file access, or schema changes. T-03-16 (miscalibrated floor)
is mitigated by the measured calibration above; T-03-17 (Voyage cost out-of-band) is the documented
accept; T-03-18 (paid overspend) held — two 12-case runs, $0.565 total agent cost.

## Self-Check: PASSED

- `src/relay/config.py` — FOUND (`retrieval_floor: float = 0.30`, no "Placeholder")
- `.planning/phases/03-semantic-retrieval/03-06-SUMMARY.md` — FOUND
- Commit `199f822` — FOUND in `git log`
- Eval reports written: BEFORE `/tmp/relay-baseline/eval_results/eval-20260810T065449Z.json`,
  AFTER `eval_results/eval-20260810T065817Z.json` (gitignored, kept on disk)

---

## Post-fix acceptance re-run (2026-08-10, HEAD `3b69e75`)

The original 12-case eval ran at `199f822`, **before** CR-01..CR-04, WR-02, WR-04 and
WR-10 landed. CR-04 rewrote the keyword half of the hybrid union that this phase's floor
calibration depends on, so the verifier correctly ruled the old artifact stale evidence
for the shipped code. Re-run against current HEAD:

**`eval_results/eval-20260810T090955Z.json` — 12/12 (100%), mean quality 4.92, $0.2774.**

Every expected action matched; `salesforce-integration` still escalates, so CR-04's
phrasing fix did not break the empty-result signal the floor was calibrated for.

### WR-10's instrumentation answered the open question on its first run

The 03-05 risk — "the citation guard never fired, so we cannot tell whether the model
recovers, or whether it ever cites at all" — is resolved:

| action | cases citing valid ids |
|---|---|
| `send_reply` | **8/8** |
| `create_escalation` | 0/4 (escalations send no reply, so nothing to cite) |

All 12 ran `retrieval.mode: semantic`, zero degraded. So the earlier silence was the
guard **correctly staying quiet on eight cases of valid citations**, not a decorative
guard on a model that never cites. RAG-04 is live in production.

**Still untested:** whether a real model *recovers* from an actual denial. No denial
fired, because no citation was invalid — which is the good outcome, but it means the
recovery wording remains proven only against fakes. Forcing a real denial needs the
eval-only seeding hook described in 03-REVIEW.md WR-10.

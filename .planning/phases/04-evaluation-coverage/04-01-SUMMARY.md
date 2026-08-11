---
phase: 04-evaluation-coverage
plan: 01
subsystem: evals
tags: [eval, retrieval, metrics, recall, mrr, EVAL-01]
requires:
  - src/relay/retrieval.py::retrieve
  - src/relay/retrieval.py::load_index
  - kb/index.json
provides:
  - evals/retrieval.jsonl
  - src/relay/retrieval_eval.py::{load_labels,scored_labels,first_relevant_rank,recall_at_k,mrr}
  - src/relay/evals.py::retrieval_metrics
  - report field run_evals()["retrieval_metrics"]
affects:
  - src/relay/evals.py
  - .github/workflows/evals.yml
  - tests/test_evals.py
tech-stack:
  added: []
  patterns: [pure-metric-module, report-only-field, keyword-pinned-free-tests]
key-files:
  created:
    - evals/retrieval.jsonl
    - src/relay/retrieval_eval.py
  modified:
    - src/relay/evals.py
    - .github/workflows/evals.yml
    - tests/test_evals.py
decisions:
  - "Metrics call the shipped retrieve() and never re-rank (D-03/Don't Hand-Roll)"
  - "Rows with relevant: [] are excluded from the recall/MRR denominator (11 of 12 scored)"
  - "recall@1 and MRR lead the printed line; recall@3 is a wiring tripwire only (D-09)"
  - "Free suite pins keyword mode; semantic recall is paid-only (D-10)"
metrics:
  duration_min: 24
  tasks: 3
  completed: 2026-08-11
requirements: [EVAL-01]
---

# Phase 4 Plan 01: Retrieval Eval Set and Metrics Summary

EVAL-01 landed: a 12-row labeled query→relevant-id set, pure `recall@k`/`MRR` functions
that score the *shipped* `retrieve()`, a report-only mode-labeled `retrieval_metrics`
field in the eval harness, and `VOYAGE_API_KEY` wired into the paid dispatch.

## What Was Built

| Task | Commit | Output |
|------|--------|--------|
| 1. Labeled set + pure metric module | `42b9e47` | `evals/retrieval.jsonl` (12 rows), `src/relay/retrieval_eval.py` |
| 2. Report-only field + workflow secret | `55b9bd8` | `evals.py::retrieval_metrics`, `print_summary` line, `evals.yml` env |
| 3. Free keyword-mode tests | `7e1b91c` | 3 tests in `tests/test_evals.py` |

`retrieval_eval.py` exports `load_labels`, `scored_labels`, `first_relevant_rank`,
`recall_at_k`, `mrr`. It prints nothing, persists nothing, and gates nothing — the
caller owns the numbers. Every score comes from `retrieve(index, row["query"],
key=key, max_results=k)`; ranking is never reimplemented. A result counts as a hit
when the doc name / located `id` / any anchor intersects the row's `relevant` set —
the same accept-set union `agent.py` builds for the citation guard.

## Measured Numbers (tracked evidence)

**Keyword mode** (what the free suite and a keyless paid run report), 11 scored rows:

| metric | value |
|--------|-------|
| recall@1 | **0.9091** |
| recall@3 | 0.9091 |
| MRR | **0.9091** |

recall@1 == recall@3 == MRR means every hit lands at rank 1 and the single miss is a
total miss, not a rank-2 near-hit. The miss is `pro-pricing` ("Pro plan pricing" →
`billing.md`): the keyword scorer returns nothing relevant for it in the top 3.

**Semantic mode** (measured locally, see cost note below): recall@1 = **1.00**,
recall@3 = 1.00, MRR = **1.00** — semantic retrieval recovers the `pro-pricing` miss.
This is the delta D-09/D-10 predicted: recall@3 saturates in both modes and carries no
signal, while recall@1/MRR separate keyword (0.91) from semantic (1.00).

Report-only per D-03. The only hard gate remains `pass_rate < args.threshold` at
`evals.py:302-304`, verified unchanged.

> **CORRECTION (post-review, `5612d90`).** These figures are **document-level recall
> over a three-document corpus**, not chunk-level. WR-01 showed the `#anchor` half of
> every label is inert to them: `_accept_set` unions doc + located id + every anchor
> (mirroring the citation guard), so a result matches iff the *document* does, and
> pointing all ten anchors at the wrong section leaves 0.9091 untouched. "recall@1
> 0.91" reads "did retrieve() put the right one of three files first". The payload now
> carries `granularity: "document"` and a `locator_precision@1` figure — the one metric
> the anchors move, and the only measurement of `_locate_heading`, which picks the id
> the model is told to cite. Keyword mode: **0.70** curated, **1.00** ticket-derived.
>
> The numbers are also **query-source dependent, by about ±0.18** (WR-02): the label
> `query` strings are hand-authored, keyword-friendly rewrites of the golden tickets,
> not the text the agent composes and sends. Same labels, same retriever, keyword mode:
> golden `subject` alone 0.8182, these curated queries 0.9091, `subject + body` 1.0000.
> The report now names its `query_source` and carries a `ticket_derived` block beside
> the curated one, so the gap is on the page instead of assumed away.

## Mutation Verification

Every test was run under its named falsifying mutation; all four failed as required,
and all sources were restored (`git diff --stat` clean afterward).

| Mutation | Test | Result |
|----------|------|--------|
| `billing.md#refunds` → `billing.md#store-credit` (bogus id) | `test_retrieval_labels_well_formed` | **FAILED** as required |
| `retrieve()` stubbed to return `[]` | `test_recall_and_mrr_over_labeled_set` | **FAILED** as required |
| `recall_at_k` forced to return `0.0` | `test_soft_floor_recall3_positive` | **FAILED** as required |
| `scored_labels` returns all rows (empty-relevant INCLUDED in denominator) | `test_recall_and_mrr_over_labeled_set` | **FAILED** as required |

The fourth mutation is the one this phase's review history demanded: the negative row
(`salesforce-integration`, `relevant: []`) is asserted out of the denominator via
`len(scored_labels(mixed)) == len(mixed) - 1` plus `recall_at_k(index, mixed, 3) == 1.0`
— counting it would read 0.5 and silently understate every reported number.

## Verification

- `.venv/bin/python -m pytest -q` → **259 passed** (floor 256, +3 new).
- `.venv/bin/ruff check src tests` → clean.
- `git diff --quiet .github/workflows/ci.yml` → exit 0 (**frozen, byte-unchanged**).
- `git diff --quiet src/relay/mcp_server.py` → exit 0 (**frozen, byte-unchanged**).
- All 11 non-empty `relevant` ids verified against `kb/index.json` doc names and
  `doc#slug` anchors, both by construction and by `test_retrieval_labels_well_formed`.

## Unverified: the VOYAGE_API_KEY repo secret

`.github/workflows/evals.yml` now passes `VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}`
to the paid dispatch. **Whether that repo secret actually exists is unverified** — it is
not observable from this working copy, and no claim is made that it is set. If it is
absent, GitHub injects an empty string, `settings.voyage_api_key` is falsy, and the paid
run reports **keyword-mode** recall (~0.91) rather than semantic (~1.00). That is the
documented fallback, not a failure. Confirming or adding the secret is a repo-settings
action outside this plan.

> **CORRECTION (post-review, `f4361a6`).** The secret is **no longer passed to the paid
> dispatch**, and passing it there was WR-08. It does not only feed
> `retrieval_metrics` — the same key feeds every `search_docs` call in all 12 golden
> runs, flipping the whole graded suite from keyword to semantic ranking. The
> `pass_rate < 0.8` gate (D-04, "unchanged") would then have been judging a retrieval
> configuration that has never been run, so neither a pass nor a failure could be
> attributed to any code change. The graded step now carries `ANTHROPIC_API_KEY` only,
> keeping the retrieval mode its 0.8 baseline was actually observed in. Semantic recall
> is still measured, by a new `python -m relay.retrieval_report` step in its own
> process — `continue-on-error`, no Anthropic spend, and unable to fail the job (D-03).

> **CORRECTION (post-review, `de65e45`).** This section originally claimed "the `mode`
> field in the report says which one you got, so a keyword number can never be misread
> as a semantic one." **That was false when written.** CR-01 of `04-REVIEW.md` proved
> `mode` was derived from credential *presence* (`"semantic" if key else "keyword"`), not
> from what `retrieve()` actually did — so a keyed run with an unusable index reported
> keyword numbers under a `"semantic"` label, which is precisely the failure that reaches
> CI. The claim is true only as of `de65e45`, where the label is read back out of
> `retrieve()` and a mixed run reports `mixed` rather than silently picking one.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] Exported `scored_labels` and `first_relevant_rank`**
- **Found during:** Task 1
- **Issue:** The plan specified only `recall_at_k`/`mrr`/`load_labels`, but Task 3's
  denominator-exclusion assertion needs to observe the scored-row set directly.
  Asserting the exclusion only through a recall value would let a wrong denominator
  hide behind a coincidentally-equal number.
- **Fix:** Promoted the two helpers to public names with docstrings explaining why
  empty-relevant rows are excluded.
- **Files modified:** `src/relay/retrieval_eval.py`
- **Commit:** `42b9e47`

**2. [Rule 3 - Blocking] ruff import-order fix in `tests/test_evals.py`**
- **Found during:** Task 3
- **Issue:** New imports tripped `I001`.
- **Fix:** `ruff check --fix` reordered the block; no logic change.
- **Commit:** `7e1b91c`

`mrr()` also takes a keyword-only `k: int = 3` (the plan's signature listed only `key`);
this is additive and keyword-only, so the planned call shape is unchanged.

## Cost Note (honest disclosure)

The Task 2 verification command was run without pinning `key=None` and picked up a real
`VOYAGE_API_KEY` from the local `.env`, issuing **real Voyage query-embedding calls**
(stated here as ~11; the actual count was 33, because scoring ran one pass per metric —
see WR-03. Single-pass scoring in `de65e45` makes ~11 correct going forward)
(fractions of a cent). That is where the semantic numbers above come from — they are
genuine measurements, not projections. No test suite makes such a call: all three new
tests pin `settings.voyage_api_key = None` via a `keyword_baseline` fixture, so
conftest's autouse `_no_outbound_http` guard is never tripped and the free suite bills
nothing. Subsequent local checks were pinned to keyword mode.

## Known Stubs

None.

## Threat Flags

None. `VOYAGE_API_KEY` is referenced only as `${{ secrets.VOYAGE_API_KEY }}` — never
echoed, never inlined (T-04-01-I mitigated). Label tampering is caught by
`test_retrieval_labels_well_formed` validating every id against `kb/index.json`
(T-04-01-T mitigated). No packages were installed.

## Follow-On

The phase-gate paid `evals.yml` dispatch (VALIDATION row 4) is still outstanding and is
deliberately not gated by this plan: it should confirm the 12-case suite stays ≥ 0.8 and
record semantic recall@1/MRR into the artifact. If the Voyage secret is missing, that
artifact will read `"mode": "keyword"` — check that field before reporting the run's
recall as semantic.

> **CORRECTION (post-review, `f4361a6`).** After WR-08 the paid dispatch produces **two**
> artifacts, and the semantic numbers are in the second one. `eval-*.json` comes from
> the graded run, which is keyword by design (`key_configured: false`) — that is the
> configuration its 0.8 threshold was measured under. `retrieval-*.json` comes from the
> separate report-only step and is where semantic recall lands, when and only when the
> repo secret exists. Read `mode` on each; do not read the graded artifact's recall as
> semantic, and do not read the report-only step's failure as a suite failure.

## Self-Check: PASSED

- `evals/retrieval.jsonl` — FOUND
- `src/relay/retrieval_eval.py` — FOUND
- `.planning/phases/04-evaluation-coverage/04-01-SUMMARY.md` — FOUND
- Commits `42b9e47`, `55b9bd8`, `7e1b91c` — FOUND in `git log`

---

## Paid runs 2026-08-11 — measured numbers, and one finding

### Keyword vs semantic, side by side (curated queries, document-level)

| | recall@1 | MRR | recall@3 | locator@1 |
|---|---|---|---|---|
| keyword | 0.909 | 0.909 | 0.909 | 0.70 |
| **semantic** | **1.00** | **1.00** | **1.00** | **0.80** |

Ticket-derived queries score 1.00 recall@1 and 1.00 locator@1 in semantic mode. This is the
keyword-vs-semantic comparison deferred as a v2 item — now with real numbers rather than an
assertion. Report: `eval_results/retrieval-20260811T020114Z.json`.

Read `recall@1` as *the right document ranked first* (see the WR-01 correction — the metric is
document-level by design); `locator@1` is the anchor-sensitive one.

### 12-case suite: 11/12 (92%), above the 0.8 gate

`eval_results/eval-20260811T020257Z.json`, $0.2778. **This local run used semantic retrieval**
because `.env` supplies `VOYAGE_API_KEY`; the WR-08 decoupling applies to the CI workflow, where
the gated step gets `ANTHROPIC_API_KEY` only and therefore runs keyword mode. Local and CI are
*not* the same configuration — check the report's `mode` field before comparing runs.

### The one failure is a KB content gap, not a code defect

`downgrade-data-loss` failed on `grounded`. Retrieval was correct and undegraded, and
`billing.md#upgrades-and-downgrades` *was* retrieved; the model cited a valid id but invented:

> "You'd regain full access to them if you upgrade back to Pro later"

Phase 3's before-run failure (`pro-pricing`) was the same invention in the same direction. The KB
documents what happens on **downgrade** and is silent on whether data is restored on
**re-upgrade**, so the model fills the silence — landing on a different case each run, which is
why it has read as judge noise. **Fixing this is a `kb/billing.md` edit, not a retrieval change**
— and it would require rebuilding `kb/index.json` (the hash gate will catch it if not).

### KB gap closed — re-run confirms the diagnosis

`kb/billing.md` gained two sentences stating that read-only projects are never deleted and that
upgrading restores write access automatically. Index rebuilt (`c77e0dca`). Re-run:

**12/12 (100%), mean quality 5.0, $0.2877** — `eval_results/eval-20260811T071058Z.json`.
`downgrade-data-loss` now grounds. `locator@1` rose 0.80 → 0.90 (the new text gives the locator a
better-matching section for upgrade questions).

That settles it: the failure was a **content gap, not judge noise and not a code defect**. Two
phases had read it as nondeterminism because it surfaced on a different case each run.

**The first wording broke the escalation signal, and the CR-04 test caught it.** The draft ended
"with no support request needed" — the word *support* keyword-matched off-topic queries
("any plan to support Salesforce?", "Mars colonization support"), so they stopped returning `[]`
and stopped escalating. Two `test_an_uncovered_ask_returns_nothing_however_it_is_phrased` cases
failed. Reworded to "immediately and automatically". Worth recording: **the empty-result
escalation signal is sensitive to ordinary KB vocabulary**, and on a 3-doc corpus one common word
is enough to move it. That test is the thing standing between a KB edit and a silent escalation
regression.

Policy note: filling the gap meant *choosing* the re-upgrade policy for a fictional product. The
stated policy (restore on upgrade) is what the existing "locks read-only rather than deleting
them" already implies — it was not chosen to match the model's guess, though it does.

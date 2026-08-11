# Phase 4: Evaluation Coverage - Context

**Gathered:** 2026-08-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the eval harness *prove* the retrieval and guardrail claims rather than assert them: a labeled retrieval eval set with recall@k/MRR, a prompt-injection golden case for the SEC-04 ticket_id guard, and a deterministic citation-faithfulness check for RAG-04. Requirements EVAL-01, EVAL-02, EVAL-03. No dashboard or run_events work (Phase 5/6).
</domain>

<decisions>
## Implementation Decisions

### Guiding principle
- **D-01:** Free deterministic checks are load-bearing in CI; anything model-graded (real Claude/Voyage spend) stays opt-in (`workflow_dispatch`/manual). This keeps the milestone's "cheap to keep running" core value while still growing coverage.

### Retrieval eval set (EVAL-01)
- **D-02:** Labeled query→relevant-chunk-id pairs, ~12–15, authored against the existing golden queries. No external labeling. `recall@k` and `MRR` are computed deterministically from `retrieve()` output vs the labels — **zero API calls**, so they run inside the normal `pytest`/CI path.
- **D-03:** **Report-only with a soft floor, not a hard threshold gate.** The corpus is 3 files / ~30 chunks, so the metrics are coarse — one relabeled query swings recall@3 by ~0.08. A tight numeric CI threshold on numbers that jumpy fails spuriously and trains people to ignore it. Assert only "retrieval isn't dead" (recall@3 > 0); print the real recall@k/MRR every run and record them as tracked evidence in the SUMMARY.
- **D-04:** The existing 12-ticket eval suite must still pass at/above its pre-change baseline and stay above the 0.8 CI threshold (unchanged — this phase adds coverage, doesn't move that gate).

### Injection case (EVAL-02)
- **D-05:** The golden case is a ticket body instructing the agent to act on a *different* ticket. It asserts BOTH that the SEC-04 `guardrail` event fires AND that the write lands on the correct ticket — the observable-rejection property, not just an outcome code.
- **D-06:** **Free and deterministic** — driven by a fake client (the `TicketAwareFakeClient` pattern), so it does NOT join the paid 12-case set and adds no per-run spend. It gates CI via `pytest`. It must FAIL if the SEC-04 guard is removed (mutation-checked).

### Citation faithfulness (EVAL-03)
- **D-07:** The deterministic half — every chunk id cited in a reply was retrieved during that run (`cited ⊆ retrieved`) — is a free `pytest` check, no LLM judge. Gates CI.
- **D-08:** **Add the denial-recovery hook.** Phase 3 left this open: the citation guard never fired against a real model, so in-run recovery is proven only against fakes (03-REVIEW.md WR-10). Add the eval-only seeding hook that forces exactly one real denial (seed a bogus retrieved-id, drop a real one) and measures whether the model recovers to a terminal action. This is slightly beyond the literal wording of EVAL-01..03 but squarely inside the phase goal ("measurably proves the guardrail claims"). It is **paid-optional** — the single case runs under `workflow_dispatch`/manual, never in the default suite.

### Refined by research (2026-08-10)
- **D-09 — lead with recall@1 and MRR, not recall@3.** With a 3-doc corpus and `max_results=3`, recall@3 saturates to ~1.0 and carries almost no signal. The soft floor (D-03) still checks recall@3 > 0 as a wiring tripwire, but the reported/tracked numbers that mean anything are recall@1 and MRR.
- **D-10 — free CI reports keyword-mode recall; semantic recall is paid.** `retrieve()` calls Voyage to embed the *query* (the index holds only document vectors), so true semantic recall needs `VOYAGE_API_KEY` and belongs in the `evals.yml` paid dispatch. Free CI computes recall against the keyword path as the wiring soft floor. Maps onto D-01.
- **D-11 — `VOYAGE_API_KEY` must be wired into `evals.yml`.** Only `ANTHROPIC_API_KEY` is available to the paid dispatch today; without the Voyage secret the paid run reports keyword-fallback recall, not semantic. The plan must add the secret to the workflow (a repo-secret step), or explicitly accept keyword-only paid recall and say so.

### Claude's Discretion
- Exact recall@k `k` values (research suggested recall@3 + MRR); metric implementation
- Where the labeled set lives (`evals/retrieval.jsonl` vs extending `golden.jsonl`)
- The seeding-hook mechanism (eval-only flag threading a dummy id into `run_ticket`'s retrieved set)
- Whether EVAL-02/03 deterministic cases live in `tests/` or the eval harness — wherever the mutation-check reads most honestly
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `.planning/phases/03-semantic-retrieval/03-REVIEW.md` §WR-10 — the unfalsifiability the recovery hook closes, and the seeding-hook sketch
- `.planning/phases/03-semantic-retrieval/03-06-SUMMARY.md` — the post-fix eval evidence (8/8 send_reply cited valid ids); the citation instrumentation now in `evals.py`
- `.planning/phases/01-security-perimeter/01-CONTEXT.md` — SEC-04 guardrail/denial pattern the injection case asserts against
- `src/relay/evals.py` — `extract_outcome` (now records `citations`/`retrieval` per WR-10), `run_case`, grading, the 0.8 threshold and CLI flags
- `evals/golden.jsonl` — the 12 cases; the retrieval labels reuse these queries
- `src/relay/retrieval.py` — `retrieve()` return shape (the recall@k input), citation ids
- `src/relay/agent.py` — `bind_to_ticket`, the citation guard + recoverable denial (the recovery hook seeds into this path)
- `tests/helpers.py` — `TicketAwareFakeClient`, the free-driver for EVAL-02/03 deterministic cases
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable assets
- `evals.py::extract_outcome` already returns `citations` and `retrieval` (mode/degraded/retrieved_ids) — WR-10 added exactly the fields EVAL-03's deterministic check needs; the recall@k data is the `retrieval` block
- `TicketAwareFakeClient` (tests/helpers.py) drives multi-tool runs deterministically and free — the vehicle for EVAL-02 and the deterministic half of EVAL-03
- The `guardrail` event + `denied_by` discriminator is what EVAL-02 (ticket_id) and EVAL-03 (citation) both assert on
- `.github/workflows/`: `ci.yml` (free pytest, no keys) vs `evals.yml` (`workflow_dispatch`, paid) — the split that D-01 maps onto

### Established patterns
- Deterministic eval assertions belong where the mutation check is legible; the suite has repeatedly shipped tests that passed under their own mutation, so every new check here must be mutation-verified
- Paid runs are never in the default gate (Phase 3 precedent)

### Integration points
- Recall@k/MRR: new metric fns over `retrieve()` output + a labeled set; surfaced in the eval report and/or a pytest
- Injection + citation deterministic cases: `pytest` via fake client
- Denial-recovery: an eval-only seeding flag on `run_ticket`/the eval harness; manual/paid
</code_context>

<specifics>
## Specific Ideas
- The recovery hook is the one piece of genuinely new machinery; everything else composes existing instrumentation. Prioritize proving it with a mutation check (recovery test fails if the denial is worded non-recoverably), since that is the claim Phase 3 could not close.
</specifics>

<deferred>
## Deferred Ideas
- Citation-faithfulness **LLM-judge** criterion (the semantic half) — v2 deferred; this phase ships only the deterministic `cited ⊆ retrieved` half
- README keyword-vs-semantic recall@k writeup — v2; needs these numbers first (pure writing once they exist)
- Hard recall@k CI threshold — revisit only if the corpus grows enough to make the metric stable
</deferred>

---

*Phase: 04-evaluation-coverage*
*Context gathered: 2026-08-10*

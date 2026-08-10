# Phase 4: Evaluation Coverage - Discussion Log

> Audit trail only. Decisions live in CONTEXT.md.

**Date:** 2026-08-10
**Phase:** 04-evaluation-coverage
**Mode:** user requested recommendations; all four accepted as-is.

## Retrieval eval set (EVAL-01)
Recommended & accepted: ~12–15 self-labeled query→chunk pairs; recall@k/MRR deterministic and free; **report-only with a soft floor** (recall@3 > 0), not a hard numeric CI gate — the ~30-chunk corpus makes the metric too jumpy for a tight threshold. Real numbers recorded as evidence.

## Denial-recovery case (EVAL-03 extension)
Recommended & accepted: add the eval-only seeding hook that forces one real citation denial and measures recovery — closes Phase 3's WR-10 open question. Paid-optional, manual only.

## Injection case (EVAL-02)
Recommended & accepted: assert the SEC-04 `guardrail` event fires AND the write lands on the correct ticket; free/deterministic via fake client; gates CI; not in the paid set; mutation-checked against guard removal.

## Gates & spend (D-01)
Recommended & accepted: deterministic checks (EVAL-02, EVAL-03 structural half, recall@k soft floor) gate CI; anything model-graded (paid 12-case, recovery case) stays `workflow_dispatch`/manual.

## Deferred
LLM-judge citation criterion; README recall@k writeup; hard recall@k threshold.

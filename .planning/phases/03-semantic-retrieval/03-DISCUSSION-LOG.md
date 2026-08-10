# Phase 3: Semantic Retrieval - Discussion Log

> Audit trail only. Decisions live in CONTEXT.md.

**Date:** 2026-08-10
**Phase:** 03-semantic-retrieval
**Areas discussed:** chunk-vs-cite granularity, escalation floor, eval spend, citation trust, fallback trigger

## Chunk vs cite granularity
- Whole-file embed, heading-level cite ✓ | Heading-level chunks | Doc-level only
- Chose whole-file + heading-level citation: honors research's anti-regression stance while satisfying SC-2's `{doc}#{heading}` shape as a best-effort locator.

## Escalation floor
- Calibrate against golden set ✓ | Conservative fixed default
- The eval suite is the acceptance test; floor picked empirically so escalation cases still escalate.

## Eval spend
- Full 12-case before/after ✓ | Subset only
- Per-case diff to catch escalation regressions; real Voyage+Claude spend approved.

## Citation trust (RAG-04)
- Reject like ticket_id binding ✓ | Strip uncited claims silently
- Reuse SEC-04 guardrail pattern; observable rejection over silent stripping.

## Fallback trigger (RAG-05)
- Key unset OR API error ✓ | Key unset only
- Both cold-start/CI (no key) and live outage fall back to keyword scorer, degradation surfaced.

## Claude's Discretion
- Index script location, kb_sha256 mechanics, httpx vs voyageai SDK, exact floor value, test structure.

## Deferred
- Reranker; recall@k eval set (Phase 4); README comparison (v2).

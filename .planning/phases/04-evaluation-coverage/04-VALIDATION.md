---
phase: 4
slug: evaluation-coverage
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-08-10
---

# Phase 4 — Validation Strategy

> Derived from `04-RESEARCH.md` § Validation Architecture. This is a composition
> phase — every guard/event/field the requirements assert on already exists and was
> read line-by-line; the one new mechanism is the D-08 seeding hook.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_evals.py -q` |
| **Full free suite** | `.venv/bin/python -m pytest -q` (256 passing at Phase 3 close) |
| **Paid (manual)** | `python -m relay.evals --concurrency 4 --threshold 0.8` (evals.yml, workflow_dispatch) |
| **Lint gate** | `ruff check src tests` |

---

## Sampling Rate

- **After every task commit:** `.venv/bin/python -m pytest tests/test_evals.py -q`
- **After every wave:** `.venv/bin/python -m pytest -q && ruff check src tests` — full free suite green
- **Phase gate:** free suite green; paid `evals.yml` dispatch run once to (a) confirm the 12-case suite ≥ 0.8 and (b) capture semantic recall@1/MRR + one real-model recovery into the artifact
- **Max feedback latency:** 10 seconds (free); the paid run is a phase-gate step, not per-commit

---

## Per-Task Verification Map

| Req ID | Behavior | Test Type | Automated Command | File Exists | Status |
|--------|----------|-----------|-------------------|-------------|--------|
| EVAL-01 | recall@1/@3 + MRR over `retrieve()` from the labeled set | unit (keyword) | `pytest tests/test_evals.py -k recall -x` | ❌ W0 | ⬜ pending |
| EVAL-01 | soft floor `recall@3 > 0` (D-03, not a hard gate) | unit (keyword) | `pytest tests/test_evals.py -k soft_floor -x` | ❌ W0 | ⬜ pending |
| EVAL-01 | labeled set well-formed; every id exists in `kb/index.json` | unit | `pytest tests/test_evals.py -k retrieval_labels -x` | ❌ W0 | ⬜ pending |
| EVAL-01 | 12-case suite does not regress below `--threshold 0.8` | integration (paid) | `python -m relay.evals --threshold 0.8` | ✅ existing gate | ⬜ pending |
| EVAL-02 | injection → `guard="ticket_binding"` event fires AND victim ticket unwritten | integration (free, FakeClient) | `pytest tests/test_evals.py -k injection -x` | ❌ W0 | ⬜ pending |
| EVAL-03 | `cited ⊆ retrieved` for every case in a produced report | unit (free) | `pytest tests/test_evals.py -k citation_faithful -x` | ⚠️ partial | ⬜ pending |
| EVAL-03 | seeding hook drops a real id → guard denies → fake recovers | unit (free) | `pytest tests/test_evals.py -k seed_denial -x` | ❌ W0 | ⬜ pending |
| EVAL-03 | **real** model recovers from a seeded denial | integration (paid, report-only) | `python -m relay.evals` with hook armed | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Metric-honesty note (D-09):** with a 3-doc corpus and `max_results=3`, recall@3 saturates to
~1.0 and carries almost no signal — it is a wiring tripwire only. The reported numbers that mean
something are **recall@1 and MRR**. Lead with those in the SUMMARY.

**Free vs paid recall (D-10):** `retrieve()` calls Voyage only to embed the *query*, so semantic
recall needs `VOYAGE_API_KEY` and runs in the paid dispatch. Free CI computes keyword-mode recall
as the soft-floor wiring check — the labeled ids still validate against `kb/index.json` with no key.

**EVAL-02 falsifiability:** the test must FAIL when the `denied_by="ticket_binding"` branch is
deleted (or `!=` flipped to `==`). It seeds real victim rows so an unguarded write actually lands —
the DB assertion, not just the event, is the honest check.

---

## Wave 0 Requirements

- [ ] `evals/retrieval.jsonl` — labeled query→chunk-id set (EVAL-01), ids validated against `kb/index.json`
- [ ] `recall_at_k` / `mrr` metric functions (in `evals.py` or a small `retrieval_eval.py`)
- [ ] `tests/test_evals.py` additions: recall/MRR, soft-floor, label-well-formedness, EVAL-02 injection, EVAL-03 subset, D-08 seeding-hook mechanism
- [ ] `src/relay/agent.py`: opt-in `seed_citation_denial` keyword-only param on `run_ticket` (default off)
- [ ] `src/relay/evals.py`: semantic recall@k/MRR as a **report-only** field; thread the arming flag into the paid recovery case
- [ ] `.github/workflows/evals.yml`: add `VOYAGE_API_KEY` to the paid job (or explicitly accept keyword-only paid recall). **`ci.yml` unchanged** — deterministic tests ride the existing `pytest -q` step
- [ ] No framework install — pytest/pytest-asyncio/numpy already present

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Semantic recall@1/MRR on the real index | EVAL-01 | Needs `VOYAGE_API_KEY` (query embedding) | Paid `evals.yml` dispatch; record recall@1/MRR in the artifact |
| A real model recovers from a seeded citation denial | EVAL-03 / D-08 | Real Claude spend; no fake can prove persuasion | Paid dispatch with the hook armed; confirm the run reaches a terminal action after the denial |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending

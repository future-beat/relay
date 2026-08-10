---
phase: 3
slug: semantic-retrieval
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-08-10
---

# Phase 3 — Validation Strategy

> Derived from `03-RESEARCH.md` § Validation Architecture. The retrieval design was
> verified against the actual SEC-04 code the citation guard mirrors; test-breakage
> was enumerated by grepping every `send_reply`/`search_docs` call in the suite.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` → `[tool.pytest.ini_options]` |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_retrieval.py tests/test_index.py tests/test_guardrails.py -x -q` |
| **Full suite command** | `.venv/bin/python -m pytest -q` (must pass with `VOYAGE_API_KEY` unset) |
| **Baseline** | 150 passed; `ruff check src tests` clean |
| **Acceptance gate (paid, separate)** | `python -m relay.evals --concurrency 4 --threshold 0.8`, 12 cases, before/after diff |
| **Lint gate** | `ruff check src tests` |

---

## Sampling Rate

- **After every task commit:** `pytest tests/test_retrieval.py tests/test_index.py tests/test_guardrails.py -x -q`
- **After every plan wave:** `pytest -q && ruff check src tests` — full suite green with `VOYAGE_API_KEY` unset (proves the CI/cold-start keyword baseline)
- **Phase gate:** full suite green **and** the D-11 before/after eval diff recorded (chosen floor + per-case table) as acceptance evidence
- **Max feedback latency:** 10 seconds (unit); the paid eval is a phase-gate step, not per-commit

---

## Per-Task Verification Map

| Req ID | Behavior | Test Type | Automated Command | File Exists | Status |
|--------|----------|-----------|-------------------|-------------|--------|
| RAG-01 | cosine ranking + floor + `input_type="query"` → ranked whole-file results | unit | `pytest tests/test_retrieval.py -x` | ❌ W0 | ⬜ pending |
| RAG-01 | `search_docs` stays sync & runs off the loop (Phase 2 seam) | unit (exists) | `pytest tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop -x` | ✅ | ⬜ pending |
| RAG-02 | `kb_sha256` staleness gate fails on a KB edit without rebuild | unit | `pytest tests/test_index.py::test_index_matches_kb -x` | ❌ W0 | ⬜ pending |
| RAG-02 | full suite green with **no** `VOYAGE_API_KEY` (zero-Voyage CI path) | integration | `VOYAGE_API_KEY= pytest -q` | ✅ (add no-key assertion) | ⬜ pending |
| RAG-03 | result carries `{doc, heading, id, text, score}`, `id = {doc}#{heading}` | unit | `pytest tests/test_retrieval.py -k citation_id -x` | ❌ W0 | ⬜ pending |
| RAG-03 | off-topic query returns `{"results": []}` (floor) | unit | `pytest tests/test_retrieval.py -k empty -x` | ❌ W0 | ⬜ pending |
| RAG-04 | `send_reply` citing an unretrieved id is denied, run recovers in-run | unit | `pytest tests/test_guardrails.py -k citation -x` | ❌ W0 | ⬜ pending |
| RAG-04 | citing a retrieved id succeeds; `[] ⊆ retrieved` passes (back-compat, D-12) | unit | `pytest tests/test_guardrails.py -k citation -x` | ❌ W0 | ⬜ pending |
| RAG-05 | Voyage failure → keyword results + `notice` degradation event, run not ended | unit | `pytest tests/test_retrieval.py -k degrade -x` | ❌ W0 | ⬜ pending |
| all (D-11) | 12-case eval pass rate ≥ pre-change baseline, per-case diff | e2e (paid) | `python -m relay.evals --concurrency 4 --threshold 0.8` | ✅ harness | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Critical — the `ended_without_action` trap (from research + milestone Pitfall 3):** a rejected
`send_reply` leaves `resolved_via=None` → `ended_without_action` → eval `action_ok` fails. The
RAG-04 denial MUST be recoverable in-run (model retries with a valid citation), exactly as SEC-04's
ticket_id denial is. `test_run_recovers_after_binding_denial` is the template. This is why the
before/after eval diff is the phase's primary acceptance test, not a regression guard.

**Floor calibration is narrower than "all escalations":** only the off-topic case
(`salesforce-integration`) needs the floor to return `[]`. The other escalation cases escalate
because the model reads doc text and decides a human is needed — the floor must **not** starve them.

---

## Wave 0 Requirements

- [ ] `tests/test_retrieval.py` — cosine, floor→empty, hybrid union, keyword fallback/degrade, citation-id shape (RAG-01/03/05)
- [ ] `tests/test_index.py` — `kb_sha256` staleness gate (RAG-02), runs inside the existing CI `pytest` step (no workflow edit, no key)
- [ ] `tests/test_guardrails.py` — citation-guard tests mirroring the binding tests (RAG-04)
- [ ] `tests/test_tools.py` — `search_docs` result-shape + `send_reply` optional `citations` (back-compat via defaulted param)
- [ ] No-`VOYAGE_API_KEY` assertion (keyword baseline)
- [ ] `kb/index.json` committed before `test_index.py` can pass — the build script is a prerequisite artifact, not a test
- [ ] `pip install -e ".[dev]"` after adding `numpy`/`httpx` to `pyproject.toml`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real Voyage retrieval quality | RAG-01 | Needs a live key + real corpus | Build the index with a real `VOYAGE_API_KEY`, run a paraphrased-query smoke check |
| 12-case eval before/after | D-11 | Real Voyage+Claude spend | `python -m relay.evals` on the pre-change tree and again after; diff per-case, record the chosen floor |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending

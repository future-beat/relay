---
phase: 03-semantic-retrieval
plan: 02
subsystem: retrieval
tags: [voyage, embeddings, numpy, cosine, httpx, hybrid-search, citations]

# Dependency graph
requires:
  - phase: 03-semantic-retrieval plan 01
    provides: numpy/httpx dependencies and the Voyage settings (voyage_api_key/model/dim, retrieval_floor)
  - phase: 02-async-safe-data-layer
    provides: the asyncio.to_thread tool-execution seam that lets this module stay synchronous
provides:
  - "src/relay/retrieval.py — kb_sha256/headings/slug helpers, load_index, sync Voyage query embedding, numpy cosine ranking with a similarity floor, hybrid keyword+semantic union"
  - "The {doc, heading, id, text, score} citation result shape (the Phase 4/5/6 join key)"
  - "(results, mode, degraded) contract: mode in {semantic, keyword}, degraded=True only when Voyage was tried and failed"
affects: [03-03 build_index script, 03-04 search_docs swap, 03-05 citation guard, 03-06 floor calibration, phase 4 evals, phase 5 dashboard trace]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Sync-only retrieval module: blocking httpx.post runs inside the existing to_thread seam, never an async def"
    - "Degrade-never-raise: one narrow `# noqa: BLE001` catch around the Voyage call, typed catches everywhere else"
    - "Sentinel default (_FROM_SETTINGS) so key=None means 'no credential' while an omitted arg means 'read settings now'"

key-files:
  created:
    - src/relay/retrieval.py
    - tests/test_retrieval.py
  modified: []

key-decisions:
  - "score is always the cosine when the semantic path ran — including for keyword-only union hits — so one result list never mixes two incomparable scales"
  - "load_index falls back to a keyword-only Index built by reading kb/*.md from disk, so keyword search still works when index.json is missing entirely"
  - "Heading locator scores each ##-section body against the query; deterministic fallbacks are first-heading, then heading=None with id == the bare doc name"

patterns-established:
  - "Index dataclass with matrix=None as the keyword-only mode marker"
  - "kb_sha256 lives in retrieval.py so the builder (03-03) and the CI staleness gate import the same function"

requirements-completed: [RAG-01, RAG-03, RAG-05]

# Metrics
duration: 21min
completed: 2026-08-10
---

# Phase 3 Plan 02: Hybrid Semantic Retriever Summary

**A synchronous whole-file retriever: numpy cosine over the committed Voyage index with a similarity floor, unioned with the phase-1 keyword scorer, degrading to keyword-only on a missing/stale index or any Voyage failure without ever raising into a run.**

## Performance

- **Duration:** ~21 min
- **Tasks:** 2 (3 commits — test → feat → test)
- **Files created:** 2 (319 + 272 lines)
- **Test suite:** 150 → 168 passing; `ruff check src tests` clean

## Accomplishments

- `src/relay/retrieval.py`: `kb_sha256` / `headings` / `slug` helpers (shared with the 03-03 builder and the CI staleness gate), `load_index`, `retrieve`, and a sync `_embed_query` that posts one query to Voyage with `input_type="query"`, a 10s timeout and exactly one manual retry.
- Ranking is `matrix @ q` over an L2-normalized `(N, 512)` float32 matrix with `np.argsort` for order — no hand-rolled dot loops, no vector DB.
- `retrieve()` returns `(results, mode, degraded)`. Union of above-floor semantic hits and keyword hits, deduped by doc (D-05); empty union returns `[]`, preserving the escalation signal (D-03).
- Result shape is `{doc, heading, id, text, score}` with `id = "{doc}#{slug(heading)}"` and `text` the whole file (D-01/D-02/D-06).
- Every degradation path lands on the keyword scorer: unset key (baseline, `degraded=False`), Voyage timeout/HTTP error/malformed response (`degraded=True`), missing / malformed / hash-mismatched / model- or dim-mismatched `index.json` (keyword-only Index, warning logged).
- 18 Voyage-free tests, green with `VOYAGE_API_KEY` unset; an autouse `_no_network` fixture makes any unmocked `httpx.post` raise, so a future test cannot silently start calling the real API.

## Task Commits

1. **Task 1 (RED): failing tests for the hybrid retriever** — `089ca22` (test)
2. **Task 1 (GREEN): hybrid semantic retriever with keyword fallback** — `d8c70e3` (feat)
3. **Task 2: full test coverage — union, citation ids, index fallbacks, key hygiene** — `b3b96fe` (test)

No REFACTOR commit: the GREEN implementation needed no cleanup pass.

## Files Created/Modified

- `src/relay/retrieval.py` — the whole retrieval surface: hashing, heading parsing/slugging, index load with fallbacks, sync Voyage query embedding, cosine ranking with floor, hybrid union, citation result shape.
- `tests/test_retrieval.py` — 18 unit tests covering RAG-01/03/05, the D-02 no-chunk guard, the D-09 `input_type` guard, and index-staleness degradation.

## Decisions Made

- **`score` semantics under hybrid union.** When the semantic path ran, a keyword-only union hit reports its (below-floor) cosine rather than its raw term count, so the returned list is internally comparable. In pure keyword mode `score` is the term count. Documented inline.
- **`_FROM_SETTINGS` sentinel for `key`/`floor`.** `retrieve(index, q)` reads `settings.voyage_api_key`/`settings.retrieval_floor` at call time (so `monkeypatch.setattr(settings, ...)` works, and 03-06's calibrated floor lands with no call-site change), while an explicit `key=None` still means "no credential, keyword only". A plain `None` default would have conflated those.
- **`load_index` on a missing file still returns usable docs**, read straight from `kb/*.md`, rather than an empty Index. Otherwise a missing artifact would silently disable search entirely instead of degrading to keyword.
- **Typed catches outside the Voyage call.** `load_index` catches `(OSError, ValueError, KeyError, TypeError)` by name rather than adding a second broad except, keeping the codebase's one-sanctioned-`BLE001`-per-boundary convention intact.

## Deviations from Plan

None — plan executed exactly as written. Two additions beyond the enumerated task list, both within the plan's own threat model rather than new scope:

- A test asserting the Voyage API key never reaches the log on failure (`T-03-04`).
- A test asserting a wrong-width embedding response degrades instead of ranking on garbage (`T-03-05`).

## Test Honesty Assessment

The plan asked whether each load-bearing test would fail if the behavior were removed. Checked individually:

| Test | Would fail if behavior removed? |
|------|-------------------------------|
| `..._sends_input_type_query` | Yes — reads `input_type` out of the captured `httpx.post` kwargs; flipping it to `"document"` fails immediately. |
| `test_results_return_whole_files_never_chunks` (D-02) | Yes — compares `result["text"].encode("utf-8")` to `kb/billing.md` bytes through the real `load_index` → `retrieve` path. Any slicing, truncation, or chunk-join inside `retrieve` fails it. |
| `..._puts_the_on_topic_doc_first_and_drops_below_floor` | Yes — asserts `mode == "semantic"` and `score ≈ 1.0`; deleting the semantic path yields `mode == "keyword"` and a term-count score. |
| `..._below_the_floor_returns_empty_results` | Yes — removing the floor returns all 3 docs. |
| `..._degrades_to_keyword_and_never_raises` | Yes — asserts `degraded is True` *and* `len(calls) == 2`; dropping the retry or the flag fails. |
| `..._keyword_baseline_not_a_degradation` | Yes — asserts `degraded is False` in keyword mode, so conflating "no key" with "Voyage failed" fails. |
| `test_hybrid_union_keeps_a_keyword_only_hit...` | Yes — semantic-only retrieval drops `api.md` from the result docs. |
| `test_voyage_failure_never_logs_the_api_key` | Yes on the leak assertion; it also asserts the warning event name so it cannot pass by logging nothing at all. |

**One honest caveat.** The D-02 no-chunk test proves `retrieve()` returns whatever full text `index.json` carries; it cannot prove the *builder* stored whole files, because the test writes its own index. A builder-side chunking regression is 03-03's to guard (its index.json `text` is compared against the KB by the `kb_sha256` gate only at the hash level). Worth a line in 03-03's tests.

`test_citation_id_falls_back_to_the_bare_doc_when_there_are_no_headings` is the weakest of the set — it exercises a synthetic doc that does not exist in the real KB. It is a contract test for the documented fallback, not evidence about production data.

## Issues Encountered

- Ruff's isort classified `relay.retrieval` as third-party while the module did not yet exist (RED phase), then re-sorted it into the first-party block once it did. Resolved by `ruff check --fix` after each step; final state is clean.
- The module docstring originally contained the literal string `async def` while explaining why there isn't one, which trips the plan's `grep -c 'async ' == 0` gate. Reworded — the gate now returns 0.

## Verification

- `grep -c 'async ' src/relay/retrieval.py` → `0`
- `env -u VOYAGE_API_KEY .venv/bin/python -m pytest -q` → 168 passed
- `.venv/bin/python -m pytest -q -k "empty or degrade or citation_id" tests/test_retrieval.py` → 5 passed
- `.venv/bin/ruff check src tests` → clean

## Known Stubs

None. `settings.retrieval_floor = 0.55` remains the documented placeholder from 03-01; this plan consumes it but does not calibrate it (03-06 owns that), and every floor-sensitive test passes the floor explicitly.

## Next Phase Readiness

Ready for:
- **03-03** — imports `kb_sha256` and `headings` from this module to stamp `kb/index.json`; must write `meta.{model, output_dimension, input_type_document, kb_sha256}` and `docs[].{doc, headings, text, embedding}` exactly as `load_index` reads them.
- **03-04** — `build_registry` calls `load_index(kb_dir)` once and the `search_docs` closure captures it; the tool wraps `retrieve()` and serializes `{"results": [...], "retrieval_mode": mode, "degraded": degraded}`.
- **03-05** — `result["id"]` is the value the citation guard's `retrieved_ids` set accumulates.
- **03-06** — `retrieve(..., floor=...)` is already parameterized; calibration only changes `settings.retrieval_floor`.

## Self-Check: PASSED

- `src/relay/retrieval.py` — FOUND
- `tests/test_retrieval.py` — FOUND
- `089ca22`, `d8c70e3`, `b3b96fe` — all FOUND in `git log`

---
*Phase: 03-semantic-retrieval*
*Completed: 2026-08-10*

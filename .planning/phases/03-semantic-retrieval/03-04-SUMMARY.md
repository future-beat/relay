---
phase: 03-semantic-retrieval
plan: 04
subsystem: tools
tags: [retrieval, tool-registry, citations, guardrails, back-compat]

# Dependency graph
requires:
  - phase: 03-semantic-retrieval plan 02
    provides: "retrieval.load_index / retrieval.retrieve and the {doc, heading, id, text, score} result shape"
  - phase: 02-async-safe-data-layer
    provides: "the asyncio.to_thread tool seam that lets search_docs stay a sync Callable[..., str]"
provides:
  - "search_docs delegating ranking to the hybrid retriever behind an unchanged {\"results\": [...]} envelope, plus additive retrieval_mode/degraded keys"
  - "build_registry's load-once index seam: one load_index(kb_dir) at startup, captured by the search_docs closure"
  - "SendReplyInput.citations — an optional, validated list[str] and the matching tool-schema property + executor parameter"
affects: [03-05 citation guard, 03-06 floor calibration, phase 4 evals, phase 5 dashboard trace]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Load-once-capture: expensive read-only state is loaded in build_registry and captured by the closure, exactly as conn/kb_dir already were"
    - "Optional-with-default tool argument: a new model-facing field lands as default_factory=list + a defaulted executor param, so no existing call site is touched"

key-files:
  created: []
  modified:
    - src/relay/tools.py
    - src/relay/guardrails.py
    - tests/test_tools.py

key-decisions:
  - "search_docs omits key=/floor= and lets retrieve()'s _FROM_SETTINGS sentinel read settings at call time — behaviourally identical to passing them, but 03-06's calibrated floor needs no call-site change"
  - "citations is validated but not persisted this phase; the DB schema stays untouched until something reads it back"
  - "citations is capped at 20 entries — an untrusted model-supplied collection gets a bound like every other input in guardrails.py"

patterns-established:
  - "The tool envelope grows additively (retrieval_mode/degraded next to results) rather than nesting, so existing consumers that read result[\"results\"] are unaffected"

requirements-completed: [RAG-01, RAG-03, RAG-04]

# Metrics
duration: 14min
completed: 2026-08-10
---

# Phase 3 Plan 04: Tool-Surface Swap Summary

**`search_docs` now ranks through the hybrid retriever while returning the same `{"results": [...]}` envelope of whole files, and `send_reply` accepts an optional, validated `citations` list — both changes land with zero edits to any pre-existing call site.**

## Performance

- **Duration:** ~14 min
- **Tasks:** 2 (4 commits — test → feat, test → feat)
- **Files modified:** 3
- **Test suite:** 168 → 176 passing; `ruff check src tests` clean

## Accomplishments

- `search_docs(index, query, max_results=3)` delegates to `retrieval.retrieve` and serializes `{"results": [...], "retrieval_mode": mode, "degraded": degraded}`. The `results` key and its full-file `text` are byte-compatible with phase 1 (D-01/D-02) — ranking is the only variable that moved.
- Each result now carries the `{doc, heading, id, text, score}` citation shape (D-06), the join key Phase 4's evals and Phase 5's trace will consume.
- `build_registry` calls `retrieval.load_index(kb_dir)` once and the `search_docs` closure captures the `Index`, so no tool call re-reads or re-parses the embedding matrix from disk.
- `search_docs` stayed a plain sync function: `grep -c 'async def search_docs\|async def send_reply' src/relay/tools.py` → `0`, and `tests/test_lifecycle.py`'s off-loop probe plus the registry-wide `iscoroutinefunction` assertion both still pass.
- `SendReplyInput.citations: list[str] = Field(default_factory=list, max_length=20)` — an omitted argument validates to `[]`, so `[] ⊆ retrieved` will always pass 03-05's guard (D-12).
- `send_reply(db, ticket_id, body, citations=())` and the schema's optional `citations` array property; `required` stays `["ticket_id", "body"]`, `additionalProperties` stays `False`.
- 8 new tests in `tests/test_tools.py` (11 → 15 in-file), covering the result shape, the whole-file guarantee against `kb/billing.md` on disk, the empty-result escalation signal, the load-once seam, the sync contract, and citations back-compat both ways.

## Task Commits

1. **Task 1 (RED): failing tests for the search_docs semantic swap** — `f9abb2f` (test)
2. **Task 1 (GREEN): swap search_docs onto the hybrid retriever** — `2df7f9f` (feat)
3. **Task 2 (RED): failing tests for send_reply optional citations** — `288ed86` (test)
4. **Task 2 (GREEN): add optional citations to send_reply** — `ae1092b` (feat)

No REFACTOR commits — neither GREEN implementation needed a cleanup pass.

## Files Created/Modified

- `src/relay/tools.py` — `search_docs` rewritten as a delegation to `retrieval.retrieve` (the phase-1 keyword scorer and the now-unused `re` import are gone, having moved into `retrieval._keyword_hits` in 03-02); `send_reply` grew a defaulted `citations` parameter; `build_registry` loads the index once and both closures were updated.
- `src/relay/guardrails.py` — `SendReplyInput` gained the optional `citations` field.
- `tests/test_tools.py` — new result-shape / whole-file / load-once / sync-contract / citations tests, plus an autouse fixture pinning the keyword baseline.

## Decisions Made

- **Consumed 03-02's `_FROM_SETTINGS` sentinel instead of re-reading settings at the call site.** The plan's action text said `retrieve(index, query, key=settings.voyage_api_key, floor=settings.retrieval_floor)`. Both forms read settings at call time (the read is inside the function body, so `monkeypatch.setattr(settings, ...)` works either way), but omitting the arguments is exactly what the sentinel was built for and keeps 03-06's floor calibration a one-line settings change with no call-site edit. Documented inline at the call.
- **`citations` is validated, not stored.** No `replies.citations` column this phase: nothing reads it back yet, and adding a column would be a schema change (Rule 4 territory) for no consumer. The value's job is to be checked against `retrieved_ids` in 03-05.
- **The envelope grows sideways, not downwards.** `retrieval_mode`/`degraded` sit next to `results` rather than wrapping it, so anything already reading `result["results"]` — including the model's own habits from the phase-1 prompt — is untouched.
- **The `citations` schema description names the failure.** It tells the model to cite only ids that were returned *and* that a made-up id is rejected, so 03-05's denial message is a reminder rather than a surprise.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing validation] Bounded the citations list**
- **Found during:** Task 2
- **Issue:** `citations: list[str] = Field(default_factory=list)` as literally specified accepts an unbounded, model-supplied list. Every other field in `guardrails.py` carries a cap (`body` at 10,000, `query` at 500, `email` at 254); an uncapped collection at the same trust boundary is the odd one out, and threat `T-03-09` names malformed model-supplied citations explicitly.
- **Fix:** Added `max_length=20`. No real reply cites more than a handful of the 3 KB docs, and 03-05's subset check runs over this list.
- **Files modified:** `src/relay/guardrails.py`
- **Commit:** `ae1092b`

**2. [Rule 3 - Blocking/test hygiene] Pinned a keyword baseline in tests/test_tools.py**
- **Found during:** Task 1
- **Issue:** `search_docs` now reads `settings.voyage_api_key` (via `retrieve`) rather than being dependency-free. Today the tests are safe because `kb/index.json` does not exist in this worktree, so `index.matrix is None` short-circuits before any HTTP call. Once 03-03's `index.json` lands, a developer with `VOYAGE_API_KEY` in their `.env` would have these assertions silently start ranking on live Voyage output — and making a network call from a unit test.
- **Fix:** An autouse `_keyword_baseline` fixture in `tests/test_tools.py` sets `settings.voyage_api_key = None`. These tests assert the tool's envelope and the keyword baseline, not Voyage's ordering.
- **Files modified:** `tests/test_tools.py`
- **Commit:** `f9abb2f`

### Plan-text deviation (no functional difference)

`retrieve()` is called without explicit `key=`/`floor=` — see Decisions Made. Same runtime behaviour, one less thing for 03-06 to edit.

## Back-Compat Gate (D-12)

Validated in this plan, not deferred to 03-05 as the plan review flagged:

- `PYTHONPATH=src pytest -q` → **176 passed** (floor 168).
- `git diff --name-only bda7262 -- tests/test_guardrails.py tests/test_lifecycle.py tests/test_observability.py tests/test_mcp.py tests/test_db.py tests/helpers.py` → **prints nothing**. All six files with citation-less `send_reply` call sites pass unmodified.
- The MCP path is included: `tests/test_mcp.py` drives `call_mcp_tool(..., "send_reply", {"ticket_id": 1, "body": "hi"})` through the same validator and closure.

## Test Honesty Assessment

Would each new test fail if its behavior were removed?

| Test | Would fail if behavior removed? |
|------|-------------------------------|
| `..._results_carry_the_citation_shape` | Yes — `set(hit) == {doc, heading, id, text, score}` fails on the old `{doc, content}` shape, and the `retrieval_mode`/`degraded` asserts fail on the old envelope. Verified red before the swap. |
| `..._returns_whole_files_never_chunks` | Yes on a chunking regression in `retrieve` or a `text` slice in the tool — compares against `kb/billing.md` read from disk. Inherits 03-02's caveat: it cannot prove the *builder* stored whole files (03-03 owns that). |
| `..._reads_the_index_once_not_per_call` | Yes — counts real `load_index` invocations across 3 executes; a per-call load gives 3, no load at all gives 0. It was red at 0 before the wiring existed. |
| `..._no_match` | Yes for the escalation signal (D-03) and for the envelope key. |
| `..._stays_synchronous` | Weak on its own — it duplicates `tests/test_lifecycle.py:255-261`'s registry-wide sweep. Kept as a local tripwire next to the code it constrains, not as new coverage. |
| `..._still_resolves_without_citations` | Yes — asserts `validated["citations"] == []` *and* that the DB row moved to `resolved`, so a merely-parsing-but-not-executing implementation fails. Red before the field existed. |
| `..._accepts_citations` | Yes — a dropped executor parameter raises `TypeError` on the `**validated` splat. |
| `..._rejects_non_string_citations` | Yes — `list[str]` is what rejects a bare string; typing it `Any` or `list` would pass. |
| `..._schema_declares_citations_optional` | Yes in both directions: it fails if the property is missing *and* if `citations` is ever added to `required`, which is the D-12 regression that matters. |

**Honest caveat.** No test here exercises the *semantic* path — every assertion runs with `voyage_api_key=None` and no `index.json`, so what is proven is "the tool correctly delegates and serializes", not "the ranking improved". The semantic path's own correctness is covered by `tests/test_retrieval.py` (03-02) with mocked Voyage responses; the end-to-end quality claim belongs to 03-06's calibration and Phase 4's evals.

## Notes for Later Plans

- **A shared keyword-baseline guard is probably worth having.** `tests/test_lifecycle.py`, `test_guardrails.py`, `test_observability.py` and `test_mcp.py` all drive `search_docs` through the registry. They are safe today (no `index.json` in this worktree) and safe in CI (no key), but once 03-03's index is merged, a developer with `VOYAGE_API_KEY` set would have those four files making live HTTP calls. A conftest-level autouse fixture — or the `_no_network` fixture from `tests/test_retrieval.py` promoted to `tests/conftest.py` — would close it. Deliberately not done here: `conftest.py` is shared and 03-03 was running in parallel.
- **03-05** consumes `result["id"]` from this envelope for `retrieved_ids`, and reads `citations` off the validated `send_reply` input. Both are in place; only the guard is missing.
- **03-06** changes `settings.retrieval_floor` alone — no edit to `tools.py` is needed.

## Known Stubs

None. `citations` is deliberately not persisted (see Decisions Made) rather than stubbed — the parameter is accepted and validated, and no code path pretends to store or read it.

## Verification

- `PYTHONPATH=src pytest tests/test_tools.py -q` → 15 passed
- `PYTHONPATH=src pytest -q` → 176 passed
- `grep -c 'async def search_docs\|async def send_reply' src/relay/tools.py` → `0`
- `ruff check src tests` → clean
- `SendReplyInput.model_fields["citations"].is_required()` → `False`; `SendReplyInput(ticket_id=1, body="x"*20).citations` → `[]`

## Self-Check: PASSED

- `src/relay/tools.py` — FOUND (contains `retrieval.retrieve` and `retrieval.load_index`)
- `src/relay/guardrails.py` — FOUND (contains `citations`)
- `tests/test_tools.py` — FOUND (contains `citations`)
- `f9abb2f`, `2df7f9f`, `288ed86`, `ae1092b` — all FOUND in `git log`

---
*Phase: 03-semantic-retrieval*
*Completed: 2026-08-10*

---
phase: 03-semantic-retrieval
plan: 03
subsystem: retrieval
tags: [voyage, embeddings, httpx, index-artifact, ci-gate, pytest]

# Dependency graph
requires:
  - phase: 03-semantic-retrieval (plan 03-02)
    provides: "relay.retrieval kb_sha256/headings/slug, load_index meta contract, VOYAGE_URL"
provides:
  - "scripts/build_index.py — offline Voyage index builder (input_type='document', one call for the whole KB)"
  - "tests/test_index.py — CI staleness gate (kb_sha256) plus builder unit tests, zero Voyage calls"
  - "--check mode for verifying the committed artifact locally without a key"
affects: [03-04 citations, 03-06 floor calibration, deployment]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Offline build artifact committed to the repo and shipped by the existing COPY kb ./kb"
    - "Interception-based assertion: verify the outbound request body, not the value the writer echoes back"
    - "Builder raises (BuildError) where the runtime degrades — inverse error posture by layer"

key-files:
  created:
    - scripts/build_index.py
    - tests/test_index.py
  modified: []

key-decisions:
  - "input_type is asserted off the intercepted Voyage request body, never off meta.input_type_document — a swapped literal matches itself"
  - "Builder raises BuildError and exits 1 rather than degrading; retrieval.py degrades, the builder must not"
  - "Response vectors are re-ordered by Voyage's `index` field instead of trusting response order"
  - "The staleness gate fails (not skips) when kb/index.json is absent — a missing artifact is the failure it exists to catch"

patterns-established:
  - "Single source of truth: builder and CI gate both import relay.retrieval.kb_sha256; no second hash implementation exists"
  - "Mutation-verified test: any assertion guarding a silent-degradation constant must be shown to fail under the wrong value"

requirements-completed: []  # RAG-02 completes only when kb/index.json is committed (Task 2, human-gated)

# Metrics
duration: ~18min
completed: 2026-08-10
---

# Phase 3 Plan 03: Offline Index Builder and CI Staleness Gate Summary

**`scripts/build_index.py` embeds the whole KB in one `input_type="document"` Voyage call and stamps `kb/index.json` with `kb_sha256`; `tests/test_index.py` proves the request body carries `document` (mutation-verified) and fails CI when `kb/*.md` drifts from the artifact.**

## Performance

- **Duration:** ~18 min
- **Tasks:** 1 of 2 complete (Task 2 is a `checkpoint:human-action` — needs a real `VOYAGE_API_KEY`)
- **Files created:** 2

## Accomplishments

- Offline builder that makes the only Voyage-calling path in the repo explicit, one-shot, and reproducible.
- The D-09 index-side trap is now closed by a test that watches the wire, not the writer's own output.
- Builder fails loudly on the three ways it could otherwise write a wrong index: no key, unknown model, wrong-width vectors.
- Staleness gate runs inside the existing `pytest -q` CI step — no workflow edit, no key, no network.

## Task Commits

1. **Task 1: Write scripts/build_index.py and tests/test_index.py** — `58f07c8` (feat)
2. **Task 2: Build and commit kb/index.json with a real VOYAGE_API_KEY** — NOT DONE, human-gated checkpoint

## Files Created/Modified

- `scripts/build_index.py` — reads `sorted(kb/*.md)`, embeds every whole file in one Voyage request with `input_type="document"`, `settings.voyage_model` @ `settings.voyage_dim`, writes `kb/index.json` with `meta{model, output_dimension, input_type_document, kb_sha256}` and one `docs` entry per file. Imports `kb_sha256`/`headings`/`VOYAGE_URL`/`INDEX_FILENAME` from `relay.retrieval`. `--check` verifies the artifact with no key. Writes only under `kb/`, never `/data`.
- `tests/test_index.py` — 10 tests: the staleness gate, the artifact shape check, the `input_type` interception test, meta stamping, keyless failure, `main()` write path, `--check` staleness detection, wrong-width rejection, unknown-model surfacing. Autouse `_no_network` fixture makes any unmocked `httpx.post` an assertion error.

## The Blocker This Plan Closed (mutation check — RUN and CONFIRMED)

The review blocker: `input_type="document"` was only ever going to be written as a static literal into `meta`, and reading it back proves nothing — a swapped literal matches itself. A build-time typo would silently degrade recall, crash nothing, and even survive 03-06's floor calibration.

`test_build_index_embeds_with_input_type_document` monkeypatches `httpx.post`, stubs the key via `settings`, and asserts `calls[0]["json"]["input_type"] == "document"` on the captured request. Executed mutation:

1. Changed the builder's embed call site to `"input_type": "query"`.
2. `test_build_index_embeds_with_input_type_document` **FAILED**:
   `AssertionError: the index-time embed request must carry input_type='document' (D-09); it carried 'query' — recall would degrade silently`
3. Under the SAME mutation, `test_build_index_stamps_meta_from_the_shared_hash_function` (which reads `meta["input_type_document"]`) **PASSED** — empirical proof that a meta-read-back assertion is blind to exactly this bug.
4. Reverted; the call site is back to `DOCUMENT_INPUT_TYPE` and the test passes.

The mutation is genuinely load-bearing: the interception test fails, the meta test does not.

## Decisions Made

- **`BuildError` + exit 1, never a partial write.** `retrieval.py` deliberately never raises (a Voyage outage must not end a run); the builder is the inverse, because a quietly-wrong index is a permanent silent recall loss rather than a transient one.
- **Vectors re-ordered by Voyage's `index` field.** Trusting response order would silently pair `billing.md`'s text with `api.md`'s vector — a failure with no error and no visible symptom.
- **The gate fails, not skips, on a missing `kb/index.json`.** Skipping would let a repo with no committed artifact go green, which is the exact drift RAG-02 exists to prevent.
- **`--check` reuses `settings` for model/dim** so a config change without a rebuild is caught locally as well as at `load_index`.

## Deviations from Plan

None — plan executed as written. The plan's optional `--check` mode was implemented (it was cheap and gives the same signal locally that CI gives).

## Issues Encountered

- `ruff` `EXE001` on the `#!/usr/bin/env python` shebang; fixed with `chmod +x scripts/build_index.py` (the file is a runnable script, so the executable bit is the correct resolution rather than dropping the shebang).

## Test Status

- `ruff check src tests scripts` — clean.
- Full suite: **176 passed, 2 failed** (baseline floor 168 passed, so +8 new passing).
- The 2 failures are `test_index_matches_kb` and `test_committed_index_has_one_full_width_embedding_per_doc`, both asserting `kb/index.json` exists. They are RED **by design** until Task 2 builds and commits the artifact — the plan's acceptance criteria state this explicitly. They turn green the moment the artifact lands; no code change is needed.

## User Setup Required

**Yes — Task 2 is blocked on a secret Claude cannot obtain.** `VOYAGE_API_KEY` from the Voyage AI dashboard (API Keys). See the checkpoint below.

## Next Phase Readiness

- The builder and gate are done and committed; 03-04 (citations) and 03-06 (floor calibration) are unblocked on everything except real vectors.
- **RAG-02 is not complete** until `kb/index.json` is built and committed — the artifact is the requirement, and the builder alone does not satisfy it.
- 03-06's floor calibration is meaningless against a keyword-only index; it needs the real artifact first.

## Self-Check

- `scripts/build_index.py` — FOUND
- `tests/test_index.py` — FOUND
- Commit `58f07c8` — FOUND
- `kb/index.json` — MISSING (expected; Task 2 human-gated, deliberately not fabricated)

## Self-Check: PASSED

All artifacts this agent was able to produce exist and are committed. `kb/index.json` is absent by design, not by omission.

---
*Phase: 03-semantic-retrieval*
*Completed (Task 1 of 2): 2026-08-10*

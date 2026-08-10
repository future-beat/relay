---
phase: 03-semantic-retrieval
verified: 2026-08-10T09:05:54Z
status: passed
score: 8/9 must-haves verified
overrides_applied: 0
gaps:
  - truth: "The paid 12-case eval (D-11, the phase's designated primary acceptance gate) reflects the final, shipped retrieval behavior"
    status: failed
    reason: >
      The only recorded before/after acceptance eval (eval_results/eval-20260810T065817Z.json,
      referenced in 03-06-SUMMARY.md, "after" run timestamped 2026-08-10 14:58 local) ran at
      commit 199f822 — BEFORE all four Critical fixes and WR-02's rebind/rebuild landed
      (a7a3c91 CR-01 15:22, fc7b4cb CR-02 15:28, 8c5b9a7 CR-03 15:40, 1110246 CR-04 16:28,
      27920d2 WR-02 rebuild, 3b69e75 WR-10 16:55 — all same day, all after the eval).
      CR-04 rewrote `_keyword_hits` from scratch (stopwords, whole-word anchoring, IDF
      weighting, a 2.0 gate) — the exact component the union and the D-03/D-04 escalation
      signal depend on — and CR-01 rewrote what the citation guard accepts. The 03-06
      SUMMARY's own union simulation ("Simulating retrieve()'s union at floors 0.25/0.28/
      0.30/0.33/0.35/0.55") was run against the PRE-CR-04 keyword scorer, a materially
      different implementation than what ships. No eval_results/*.json exists with a
      timestamp after 3b69e75 (16:55). WR-10's own instrumentation (citations/retrieval
      block in the eval artifact) has therefore never been exercised in a real run either —
      the capability it was built to measure is untested against the code it was built to
      measure it on. Per 03-CONTEXT.md D-11: "the eval suite is the acceptance test for
      this phase... This is the one phase where evals are the primary acceptance gate, not
      a regression guard." That gate has not been cleared for the code actually being
      verified here.
    artifacts:
      - path: "eval_results/eval-20260810T065817Z.json"
        issue: "Only after-run eval artifact on disk; predates CR-01/02/03/04 and WR-02/WR-10 by 20-120 minutes"
      - path: ".planning/phases/03-semantic-retrieval/03-06-SUMMARY.md"
        issue: "States '11/12 -> 12/12, no regressions' as the phase's acceptance evidence; that claim was true of a tree that no longer exists"
    missing:
      - "A fresh paid 12-case before/after eval run against current HEAD (post 3b69e75), with the diff and citations/retrieval block recorded, OR an explicit, human-signed override accepting the deterministic unit-test evidence (CR-01..04 repro tests in tests/test_retrieval.py, tests/test_index.py, tests/test_guardrails.py — all independently re-verified in this pass) as sufficient in place of a re-run."
human_verification:
  - test: "Re-run `python -m relay.evals --concurrency 4 --threshold 0.8` against current HEAD (VOYAGE_API_KEY + ANTHROPIC_API_KEY set) and diff per-case against the pre-change baseline recorded in 03-06-SUMMARY.md."
    expected: "Pass rate stays >= baseline (11/12 or 12/12), no case regresses, and — this is the new information this run would add — at least a handful of cases populate the WR-10 `citations`/`retrieval` block so 'does the model ever cite, and does it recover from a denial' stops being unfalsifiable."
    why_human: "Requires real, billed Voyage + Claude API spend (D-11 approved paid spend for this phase but a specific re-run needs a human to authorize the additional cost)."
  - test: "Confirm a real model, when denied a citation by the guard (CR-01-fixed accept-set), retries in-run with a valid id rather than ending the run with `ended_without_action`."
    expected: "At least one guardrail.citation_unretrieved log line appears in a real run, and that run still resolves via send_reply or create_escalation."
    why_human: "The guard never fired in the one paid eval that exists (which also predates the CR-01 fix), so this remains genuinely unobserved with a real model — the executor recovers by construction, but no live model has been shown to recover from it."
---

# Phase 3: Semantic Retrieval Verification Report

**Phase Goal:** The agent grounds replies in semantically retrieved docs with verifiable citations
**Verified:** 2026-08-10T09:05:54Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC1 | A paraphrased/uncovered-wording ticket still retrieves the right doc; a truly uncovered question returns nothing | VERIFIED | `retrieve()` ranks by cosine over the committed matrix and gates on `retrieval_floor` (`src/relay/retrieval.py:296-309`); reproduced live against the real KB — all 5 CR-04 repro phrasings (`Salesforce integration`, `Salesforce CRM integration`, `integrate with Salesforce`, `does the KB cover Salesforce`, `Does the product work on Mars?`) now return `[]` (previously 3 of 5 leaked the whole KB). `tests/test_retrieval.py::test_semantic_ranking_puts_the_on_topic_doc_first_and_drops_below_floor`, `test_off_topic_query_below_the_floor_returns_empty_results` pass. |
| SC2 | Retrieval results show stable citation IDs (`{doc}#{heading}`) with doc, heading, text, and score in the run stream | VERIFIED | `_result()` (`retrieval.py:421-435`) emits `{doc, heading, id, anchors, text, score}`; `agent.py` `tool_result` event carries the full payload verbatim (`agent.py:398-405`). `tests/test_retrieval.py::test_result_carries_the_citation_id_shape` pins the shape. |
| SC3 | A reply citing an id not retrieved during that run is rejected by the executor | VERIFIED | `_execute_guarded` subset-checks `citations` against `retrieved_ids` (`agent.py:150-172`), denies with `denied_by="citation"`. Reproduced: `tests/test_guardrails.py::test_a_citation_to_a_doc_this_run_never_retrieved_is_still_denied` cites a real, unretrieved `api.md` heading and is denied while a retrieved id from the same run passes — confirms the CR-01 fix (widen to every anchor) did not turn the guard into a rubber stamp. |
| SC4 | Cold start and CI make zero Voyage calls; the index is a committed, KB-hash-stamped artifact and CI fails when stale | VERIFIED | `kb/index.json` committed; its stamped `kb_sha256` matches `retrieval.kb_sha256(Path("kb"))` computed live in this pass (`43b3c01d...`). CR-02 fix verified live: split-doc, rename, and empty-file-add reproductions from the code review all now produce a **different** hash (previously identical). `.github/workflows/ci.yml` runs `pytest -q` with no `VOYAGE_API_KEY` set; `tests/conftest.py::_no_outbound_http` (autouse) fails any unmocked `httpx.post` in the suite. `tests/test_index.py::test_the_shipped_retrieval_floor_stays_inside_its_measured_band` and staleness tests pass. |
| SC5 | With `VOYAGE_API_KEY` unset or the API failing, runs complete via keyword scorer and degradation is visible in the run stream | VERIFIED | CR-03 fix reproduced live: a key-configured `Index` with `matrix=None` now returns `mode="keyword", degraded=True, cause="index_unavailable"` (previously `degraded=False`, invisible). `agent.py:350-373` emits a `notice` event (`kind="retrieval_degraded"`) whenever `degraded` is true, never ending the run. `tests/test_retrieval.py` degrade-path tests pass. |

**Score:** 5/5 ROADMAP success criteria verified at the code level.

### Requirements Coverage (RAG-01..05)

| Requirement | Description | Status | Evidence |
|---|---|---|---|
| RAG-01 | `search_docs` uses semantic retrieval over a precomputed `voyage-4-lite` index, correct `input_type`, cosine over in-memory numpy | SATISFIED | `retrieval.retrieve()` + `_embed_query()` (`QUERY_INPUT_TYPE="query"`); `scripts/build_index.py` uses `input_type="document"`, both asserted off the intercepted request body per the review's "Verified as Correct" section. |
| RAG-02 | Committed offline index, KB-hash-stamped, staleness-checked in CI, zero Voyage calls cold-start/CI | SATISFIED (post CR-02) | `kb_sha256` now frames each file by name+length (collision-proof, verified live); index committed and current; CI runs `pytest -q` without a Voyage key. |
| RAG-03 | Results carry stable citation IDs (`{doc}#{heading}`) with doc, heading, text, score | SATISFIED | `_result()` shape confirmed; `tests/test_retrieval.py::test_result_carries_the_citation_id_shape` and `test_a_result_carries_every_anchor_of_the_whole_file_it_returns` pass. |
| RAG-04 | `send_reply` accepts `citations`; executor validates every cited id was retrieved this run | SATISFIED (post CR-01) | Accept-set now built from the retrieved doc (bare name + every heading anchor), not the query-derived locator id; correct citations that were previously denied are now accepted, while genuinely unretrieved docs are still denied (verified both directions live and via test). |
| RAG-05 | Degrades gracefully to keyword scorer when Voyage unavailable; degradation logged and surfaced in the run event stream | SATISFIED (post CR-03) | `degraded`/`cause` now correctly set for the key-set-but-index-unusable case; `notice` event fires; boot-time `retrieval.mode_selected` log line added (WR-04) so the selected mode is visible without a code read. |

All five requirement IDs hold at the code level as of the current HEAD (post-fix commits through `3b69e75`).

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/relay/retrieval.py` | `kb_sha256`, `load_index`, cosine ranking, sync Voyage query, hybrid `retrieve()` | VERIFIED | All exports present; CR-01/02/03/04 fixes present and reproduced live (see truths above). |
| `src/relay/agent.py` | `retrieved_ids` threading, citation guard, `retrieval_degraded` notice | VERIFIED | `bind_to_ticket`, `_execute_guarded` citation subset check, `notice` event all present and wired. |
| `src/relay/tools.py` | `search_docs` semantic swap, `send_reply` optional `citations`, `build_registry` loads index once | VERIFIED | `search_docs` delegates to `retrieval.retrieve`; `build_registry` calls `load_index` once and captures it in the closure; `retrieval.log_mode_selected(index)` called at registry-build time (WR-04). |
| `src/relay/guardrails.py` | `SendReplyInput.citations` optional | VERIFIED | `citations: list[str] = Field(default_factory=list, max_length=20)`. |
| `src/relay/config.py` | Calibrated `retrieval_floor` | VERIFIED | `retrieval_floor: float = 0.30`, band-guarded by `tests/test_index.py::test_the_shipped_retrieval_floor_stays_inside_its_measured_band` (WR-02 fix). |
| `kb/index.json` | Committed, KB-hash-stamped, rebuilt after CR-02 | VERIFIED | Stamped hash matches a live recompute of `kb_sha256(Path("kb"))` against the current `kb/*.md`. |
| `scripts/build_index.py` | Offline builder, `input_type="document"` | VERIFIED | Present, imports `retrieval.kb_sha256`/`headings` (single source of truth, per WR-07's partial remediation — three copies still exist per WR-07, deferred). |
| README.md / fly.toml deploy docs | `VOYAGE_API_KEY` documented (WR-04) | VERIFIED | README.md documents the secret in three places (quick-start, `fly secrets set`, and an explicit "the one secret whose absence is silent" callout). |
| `src/relay/evals.py` | `citations`/`retrieval` recorded in eval output (WR-10) | VERIFIED (code) / UNEXERCISED (behavior) | `extract_outcome` now populates `outcome["citations"]` and `outcome["retrieval"]`; **no eval run exists that used this code path** — see Gaps. |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `retrieval.retrieve()` | Voyage `/v1/embeddings` | sync `httpx.post`, `input_type="query"` | WIRED | Confirmed by code read; network blocked in tests by design (`conftest._no_outbound_http`). |
| `tools.search_docs` | `retrieval.retrieve`/`load_index` | delegation, closure capture | WIRED | `build_registry` loads once; `search_docs` closure calls `retrieval.retrieve(index, query, ...)`. |
| `agent._execute_guarded` | citation denial | `cited ⊄ retrieved_ids` (built from `anchors`, not query-derived `id`) | WIRED | Verified live and via `tests/test_guardrails.py` in both directions (correct-but-previously-denied now accepted; genuinely-unretrieved still denied). |
| `agent.run_ticket` | `notice` event | `degraded` flag on `search_docs` result | WIRED | `agent.py:350-373`; fires on the CR-03-fixed `degraded=True` case, confirmed live. |
| `evals.extract_outcome` | eval artifact `citations`/`retrieval` fields | reads `tool_use`/`tool_result` events | WIRED (code) | Present, but never run against a real model post-fix — see Gaps/Human Verification. |

### Anti-Patterns Found

No blocker-level debt markers (`TBD`/`FIXME`/`XXX`) in phase-touched files. Known, deliberately-deferred Warning/Info items from `03-REVIEW.md` remain open and are correctly out of scope for this verification per the task brief: WR-01 (NaN/zero embedding not rejected by value), WR-03 (`_locate_heading` intro bias — mitigated in effect by the CR-01 anchors fix, since the model can now cite any heading regardless of which one the locator picks), WR-05 (retry has no backoff/retryability check), WR-06 (conftest guard's `AssertionError` swallowed by `_embed_query`'s broad except — confirmed still present; does not break any RAG-0x requirement in production, only weakens the test-suite's own safety net), WR-07 (three copies of the drift check), WR-08 (`scripts/` outside lint gate — actually mitigated: `ruff check src tests scripts` was re-run clean in this pass, though CI still only runs `ruff check src tests`), WR-09 (unbounded citation item length). None of these block the phase goal; all are pre-existing, documented, Warning/Info severity.

### Requirements Coverage (REQUIREMENTS.md cross-reference)

`.planning/REQUIREMENTS.md` still shows RAG-01..05 as `Pending`/unchecked — this is a tracking-document staleness issue (checkbox bookkeeping), not a code gap; the code-level evidence above satisfies all five.

## Independent Judgment (per verification brief)

1. **Do RAG-01..05 genuinely hold in the code as it stands?** Yes. All five were re-derived from the current `src/relay/retrieval.py`, `agent.py`, `tools.py`, `guardrails.py`, `config.py` (not from SUMMARY prose), and the four Critical-issue reproductions from `03-REVIEW.md` were re-run live against the current tree with the exact same probe queries/inputs the reviewer used — all now produce the fixed behavior.
2. **Do ROADMAP's 5 success criteria hold?** Yes, all 5 — see the Observable Truths table above, each independently reproduced against the current code, not accepted on the review's or the SUMMARY's say-so.
3. **Is the acceptance evidence still valid?** **No.** This is the phase's one material gap. The recorded 12-case paid eval (03-06-SUMMARY.md, artifact `eval_results/eval-20260810T065817Z.json`) ran at commit `199f822`, which predates every one of the four Critical fixes and WR-02/WR-10 (all landed 15:22–16:55 the same day, after the 14:58 eval). CR-04 in particular rewrote the entire keyword-scorer half of the hybrid union that the 0.30 floor calibration and the escalation signal (D-03/D-04) depend on — a different implementation than what the paid eval exercised. The unit-test evidence for each individual fix is strong (re-verified live in this pass), but D-11 explicitly designates the paid before/after eval — not the unit suite — as "the primary acceptance gate" for this phase, and that gate has not been cleared against the code being shipped. Recommend either a fresh paid re-run against current HEAD, or an explicit human override accepting unit-test evidence in its place.
4. **Anything the review missed?** No additional code-breaking gap found. One nuance worth flagging: WR-03 (`_locate_heading` biases toward the intro section) is *functionally* defanged by the CR-01 fix — since the accept-set is now every heading of the retrieved doc, a wrong `_locate_heading` guess no longer causes a false denial — but it still means the `id`/`heading` field surfaced in the run stream and (per IN-05) never persisted, and in Phase 5/6's planned dashboard trace, may point at the wrong section as "the source" even when the citation validation itself is correct. Not a RAG-0x blocker for this phase; worth a note for Phase 5/6.

### Gaps Summary

One gap: the phase's designated primary acceptance gate (D-11's paid 12-case before/after eval) was run against pre-fix code and has not been re-run against the final, shipped implementation. Every individual Critical-issue fix is independently, deterministically verified against the current codebase in this report — the mechanism is not in doubt. What's unverified is the full agent-loop, real-model behavior with all fixes composed together (does the rewritten keyword union still preserve every golden-case action, does a real model ever emit citations at all, does it recover from a denial in practice). This requires either a human-authorized paid re-run or an explicit override.

---

_Verified: 2026-08-10T09:05:54Z_
_Verifier: Claude (gsd-verifier)_

---

## Gap closed 2026-08-10 — acceptance evidence refreshed

The sole gap (stale acceptance evidence: the paid eval predated the four Critical fixes)
is closed by a re-run against HEAD `3b69e75`:

**`eval_results/eval-20260810T090955Z.json` — 12/12 (100%), mean quality 4.92, $0.2774,
all 12 cases `retrieval.mode: semantic`, zero degraded.**

WR-10's instrumentation additionally resolved the open citation question on its first
run: 8/8 `send_reply` cases cited valid retrieved ids; the 4 non-citing cases are all
escalations, which send no reply. The guard was staying quiet correctly, not sitting
inert.

**Remaining human-verification item (carried, not a gap):** whether a real model recovers
from an actual citation denial. None fired, because no citation was invalid. Forcing one
requires the eval-only seeding hook in 03-REVIEW.md WR-10.

Score: 9/9.

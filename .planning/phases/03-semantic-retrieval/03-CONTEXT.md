# Phase 3: Semantic Retrieval - Context

**Gathered:** 2026-08-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace `search_docs`'s keyword scorer with semantic retrieval over a committed Voyage embeddings index, yielding stable citation IDs and executor-validated citations, with a keyword fallback when Voyage is unavailable. Requirements RAG-01..RAG-05. No dashboard, run_events, or eval-set work here (Phase 4/5).
</domain>

<decisions>
## Implementation Decisions

### Retrieval granularity (the central tension)
- **D-01:** **Embed and return whole files, cite at heading level.** Ranking is document-level (all 3 KB files are far under Voyage's 32K context); the tool's *output* stays byte-compatible with today (full doc text), which is what isolates retrieval quality as the only changed variable. Headings are parsed so a citation can point to `billing.md#refunds`, but the `#heading` is a best-effort locator *within* the matched doc, not a separate retrieval unit.
- **D-02:** **Do not chunk.** Milestone research measured that splitting a 381-word KB into chunks returns *less* grounding context than today and drives `grounded: false` below the 0.8 eval threshold. Chunking is rejected, not deferred.

### Escalation preservation
- **D-03:** **Keep the empty-result path via a similarity floor.** Cosine always returns a top-k, so an off-topic query gets a plausible-but-irrelevant doc unless a floor gates it. Below the floor, return `{"results": []}` — the same signal that today pushes the model to `create_escalation`. Escalation-expecting golden cases (e.g. `refund-monthly`) are the ones this protects.
- **D-04:** **Calibrate the floor against the golden set**, not a guessed constant. The eval suite is the acceptance test for this phase; pick the threshold empirically so escalation cases still escalate.

### Hybrid retrieval
- **D-05:** **Union of keyword hits and above-floor semantic hits**, not a replacement. Strictly dominates either alone at this corpus size and keeps a working path when Voyage is unreachable. Keyword-only hits still get a citation id (doc-level, best-effort heading).

### Citations
- **D-06:** Retrieval results carry `{doc, heading, id: "{doc}#{heading}", text, score}` (RAG-03). Design this return shape once — it is the join key across the reply tool, the eval harness (Phase 4), and the dashboard trace (Phase 5/6).
- **D-07:** `send_reply` gains a structured `citations: [id]` argument; the executor **rejects** a reply citing any id not retrieved during this run (RAG-04) — same guardrail pattern as SEC-04's ticket_id binding: model-visible denial, agent self-corrects, distinct `guardrail` event with a `citation` guard discriminator. NOT silent stripping.
  - **Watch:** milestone research Pitfall 3 — a rejected `send_reply` leaves `resolved_via=None` → `ended_without_action` → eval `action_ok` fails. The denial wording must be recoverable enough that the model retries with a valid citation in-run, exactly as SEC-04's denial does. This is why the eval before/after diff is mandatory.

### Index artifact & fallback
- **D-08:** The index is a **committed offline artifact** (`kb/index.json` or similar), built by a script, stamped with a `kb_sha256`. CI fails when the hash does not match the current KB (RAG-02). Cold start and CI make **zero** Voyage calls — the artifact is read, never rebuilt at boot.
- **D-09:** `voyage-4-lite` at `output_dimension=512`; correct `input_type` — `"document"` at index time, `"query"` at search time (a classic silent-recall bug if swapped).
- **D-10:** **Fallback on key-unset OR any API failure/timeout** → keyword scorer, with the degradation surfaced as a run event (RAG-05). Covers both the CI/cold-start path (no key) and a live Voyage outage (the demo must keep working).

### Eval acceptance
- **D-11:** Run the **full 12-case** eval suite before and after, same model, diff per-case. This is the one phase where evals are the primary acceptance gate, not a regression guard. Real Voyage+Claude spend is approved.

### Resolved after research (orchestrator decisions, 2026-08-10)
- **D-12 — `citations` is OPTIONAL, subset-validated.** D-07 rejects a reply citing an id *not retrieved*; that is subset-validation (`cited ⊆ retrieved`), not "must cite ≥1". Making the argument optional (`default_factory=list`) keeps ~7 existing test files green (empty ⊆ retrieved always passes) and confines the SEC-04-style recoverable denial to the real failure — a hallucinated source. Requiring a citation would break those files and needlessly sharpen the `ended_without_action` eval trap. Optional it is.
- **D-13 — citation `heading` is a best-effort lexical locator.** For a whole-file result, derive the heading by best-overlap against the doc's `##` sections, parsed once at index-build time; `id = "{doc}#{slug}"`. A keyword-only hybrid hit gets the same doc-level id with its best-fit heading. This is a locator, not a retrieval unit (consistent with D-01).
- **D-14 — degradation surfaces as a distinct `notice` event**, not overloaded onto `guardrail`. Both are additive and zero-cost; a separate type keeps "we fell back to keyword search" semantically distinct from "a guard fired," which matters for the Phase 5/6 trace.
- **Floor nuance (from research):** only the off-topic case (`salesforce-integration`) needs the floor to return `[]`; the other escalation cases escalate because the model reads doc text and decides a human is needed. So calibrate the floor to catch genuinely-off-topic queries **without starving** the read-then-escalate cases — D-04's calibration target is narrower than "all escalations."

### Claude's Discretion
- Index build script location/name and `kb_sha256` mechanics (research recommends `httpx` over the heavyweight `voyageai` SDK — honor unless a blocker emerges)
- numpy in-memory cosine (no vector DB — locked out of scope); `numpy>=2.3,<3` for the 3.11 floor
- Exact floor value (output of D-04's calibration)
- Test structure following existing conventions
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone research (retrieval-specific)
- `.planning/research/PITFALLS.md` §"highest-risk change" — the measured chunking/floor/hybrid regressions and the 381-word corpus facts; the eval-as-acceptance argument
- `.planning/research/STACK.md` — `voyage-4-lite`/512-dim/`input_type`/200M-free-tier; `httpx` over `voyageai`; `numpy` version floor; sqlite-vec rejected
- `.planning/research/FEATURES.md` — citation shape as the RAG↔reply↔eval↔dashboard join key

### Codebase
- `src/relay/tools.py` — `search_docs` (the swap target; keep its output contract) and `send_reply` (gains `citations`)
- `src/relay/agent.py` — `_execute_guarded`, `bind_to_ticket` (Phase 1/gap-closure), the `guardrail` event pattern the citation guard mirrors
- `.planning/phases/01-security-perimeter/01-CONTEXT.md` — D-09/D-11 (the ticket_id denial pattern being reused for citations)

### Deferred debt that intersects this phase
- `.planning/phases/02-async-safe-data-layer-graceful-shutdown/02-DEFERRED.md` — WR-01 `transaction()` nest-safety was CLOSED in gap closure; safe to write during a run now
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable assets
- `search_docs(kb_dir, query, max_results)` returns `json.dumps({"results": [...]})` — the swap changes ranking, not the envelope
- The `guardrail` SSE event + `denied_by` discriminator (Phase 1) is the exact template for citation rejection
- `bind_to_ticket()` (Phase 1 gap closure) shows the "bake the run's constraint into the executor at construction" pattern — the citation validator needs the run's retrieved-id set in the same way

### Established patterns
- Tool executors return JSON strings, stay synchronous (`ToolSpec.execute` sync contract preserved through Phase 2); the Voyage query call must not make `search_docs` a coroutine — offload like Phase 2's `to_thread` seam if a network call lands on the loop
- Graceful degradation surfaced as a run event, not swallowed

### Integration points
- Index build: new script, committed artifact under `kb/`; CI staleness gate in the existing workflow
- `search_docs` reads the artifact at startup/first-use (no per-call disk re-read — a pre-existing perf note)
- `send_reply` citation validation in `_execute_guarded`, run's retrieved-id set threaded from `run_ticket`
</code_context>

<specifics>
## Specific Ideas
- The floor calibration and the before/after eval diff are the same activity — do them together, and record the chosen floor + the per-case diff in the SUMMARY as the acceptance evidence.
</specifics>

<deferred>
## Deferred Ideas
- Reranker — rejected at this corpus size (milestone Out of Scope); revisit only if recall@k shows a precision problem
- Retrieval eval set with recall@k/MRR — that's EVAL-01, Phase 4, and depends on the citation shape locked here
- README keyword-vs-semantic comparison writeup — v2 deferred, needs Phase 4's numbers
</deferred>

---

*Phase: 03-semantic-retrieval*
*Context gathered: 2026-08-10*

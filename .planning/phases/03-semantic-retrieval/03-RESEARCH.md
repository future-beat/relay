# Phase 3: Semantic Retrieval - Research

**Researched:** 2026-08-10
**Domain:** Voyage embeddings RAG retrofit into a hand-written FastAPI + SQLite Claude agent — offline index artifact, in-memory numpy cosine, hybrid keyword+semantic, executor-validated citations
**Confidence:** HIGH (codebase claims read from source; Voyage request/response shape confirmed against official docs; test-breakage surface enumerated by grepping the suite; the two subtle reconciliations — D-01/D-06 heading-cite and D-07 citation guard — reasoned against the exact code that already implements the SEC-04 analogue)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Retrieval granularity**
- **D-01:** Embed and return whole files, cite at heading level. Ranking is document-level (all 3 KB files are far under Voyage's 32K context); the tool's *output* stays byte-compatible with today (full doc text). Headings are parsed so a citation can point to `billing.md#refunds`, but the `#heading` is a best-effort locator *within* the matched doc, not a separate retrieval unit.
- **D-02:** Do not chunk. Splitting a 381-word KB returns *less* grounding context and drives `grounded: false` below the 0.8 eval threshold. Rejected, not deferred.

**Escalation preservation**
- **D-03:** Keep the empty-result path via a similarity floor. Below the floor, return `{"results": []}` — the same signal that today pushes the model to `create_escalation`.
- **D-04:** Calibrate the floor against the golden set, not a guessed constant. The eval suite is the acceptance test.

**Hybrid retrieval**
- **D-05:** Union of keyword hits and above-floor semantic hits, not a replacement. Keyword-only hits still get a citation id (doc-level, best-effort heading).

**Citations**
- **D-06:** Retrieval results carry `{doc, heading, id: "{doc}#{heading}", text, score}` (RAG-03). Design this return shape once — it is the join key across the reply tool, the eval harness (Phase 4), and the dashboard trace (Phase 5/6).
- **D-07:** `send_reply` gains a structured `citations: [id]` argument; the executor **rejects** a reply citing any id not retrieved during this run (RAG-04) — same guardrail pattern as SEC-04's ticket_id binding: model-visible denial, agent self-corrects, distinct `guardrail` event with a `citation` guard discriminator. NOT silent stripping.
  - **Watch:** a rejected `send_reply` leaves `resolved_via=None` → `ended_without_action` → eval `action_ok` fails. The denial wording must be recoverable so the model retries with a valid citation in-run, exactly as SEC-04's denial does. Eval before/after diff is mandatory.

**Index artifact & fallback**
- **D-08:** The index is a committed offline artifact (`kb/index.json` or similar), built by a script, stamped with a `kb_sha256`. CI fails when the hash does not match the current KB (RAG-02). Cold start and CI make **zero** Voyage calls.
- **D-09:** `voyage-4-lite` at `output_dimension=512`; correct `input_type` — `"document"` at index time, `"query"` at search time.
- **D-10:** Fallback on key-unset OR any API failure/timeout → keyword scorer, degradation surfaced as a run event (RAG-05).

**Eval acceptance**
- **D-11:** Run the full 12-case eval suite before and after, same model, diff per-case. Real Voyage+Claude spend is approved.

### Claude's Discretion
- Index build script location/name and `kb_sha256` mechanics (research recommends `httpx` over the `voyageai` SDK — honor unless a blocker emerges)
- numpy in-memory cosine (no vector DB — locked out of scope); `numpy>=2.3,<3` for the 3.11 floor
- Exact floor value (output of D-04's calibration)
- Test structure following existing conventions

### Deferred Ideas (OUT OF SCOPE)
- Reranker — rejected at this corpus size; revisit only if recall@k shows a precision problem
- Retrieval eval set with recall@k/MRR — EVAL-01, Phase 4, depends on the citation shape locked here
- README keyword-vs-semantic comparison writeup — v2 deferred, needs Phase 4's numbers
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| RAG-01 | `search_docs` uses semantic retrieval over a precomputed `voyage-4-lite` index (correct `input_type` index-vs-query, cosine over in-memory numpy) | Standard Stack (Voyage shape verified vs docs), Pattern 2 (query-time retrieval), Pattern 4 (sync-executor-in-to_thread), Code Example 2 |
| RAG-02 | Index is a committed offline artifact (script-built, KB-hash-stamped, staleness-checked in CI) — no Voyage call on cold-start or CI path | Pattern 1 (index artifact + `kb_sha256`), Code Examples 1 & 5, Pitfall 3, Environment Availability |
| RAG-03 | Retrieval results carry stable citation IDs (`{doc}#{heading}`) with doc, heading, text, score | Pattern 3 (D-01/D-06 heading reconciliation), Code Example 3, Open Question 1 |
| RAG-04 | `send_reply` accepts a structured `citations` argument; executor validates every cited id was retrieved during the run | Pattern 5 (citation guard mirrors SEC-04), Code Example 4, Pitfall 1 (ended_without_action trap), Pitfall 2 (test breakage) |
| RAG-05 | Retrieval degrades gracefully to keyword when Voyage is unavailable, logging + surfacing degradation in the run event stream | Pattern 6 (fallback + `retrieval_mode` event), Pitfall 4, Code Example 2 |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

| Constraint | Implication for this phase |
|------------|---------------------------|
| No orchestration framework — the visible hand-written loop is a feature | `search_docs` stays a plain function; numpy cosine is ~10 lines; no LangChain/vector-DB abstraction. Voyage is a single `httpx.post` |
| SSE event contract stays backward compatible; evals must keep passing | The new `retrieval_mode`/degradation event and the `citation` `guardrail` event are **additive** — `evals.extract_outcome` has no `else` branch, so unknown event types are ignored (verified `src/relay/evals.py:90-105`) |
| One container, no build step; Fly + GH Actions pipeline keeps working | Index is committed under `kb/` (shipped in the image via `COPY kb ./kb`, Dockerfile:10). Cold start reads it; no Voyage call, no build step |
| Naming: snake_case verb-first, `_`-prefixed private helpers, one concern per module | New module `src/relay/retrieval.py` for index load + cosine + Voyage client; `scripts/build_index.py` for the offline builder |
| Type hints mandatory, modern `X \| None`; keyword-only params past 2–3 args | `_execute_guarded(..., *, bound_ticket_id=UNBOUND, retrieved_ids: set[str] \| None = None)` |
| Errors: domain exceptions or model-readable strings; single sanctioned broad-except at the tool boundary | Citation denial returns a model-readable recoverable string, never raises. Voyage failure is caught in `search_docs` (a new, justified narrow catch) and degrades — never ends the run |
| Logging: `logger.info("event.name", extra={"ctx": {...}})`, dotted names | `retrieval.degraded`, `guardrail.citation_unretrieved` |
| Ruff `line-length = 100`, `ruff check src tests` in CI | Keep lines ≤ 100 |
| `RELAY_` env prefix has a per-key escape hatch (`validation_alias`) | `voyage_api_key: str \| None = Field(default=None, validation_alias="VOYAGE_API_KEY")` — Pitfall 5 |

## Summary

This phase swaps `search_docs`'s keyword scorer for hybrid keyword+semantic retrieval over a **committed** Voyage embeddings index, and adds an executor-validated `citations` argument to `send_reply`. The single most important architectural fact is that **Phase 2 already built the seam this phase needs**: tool execution is offloaded at the call site via `await asyncio.to_thread(execute_bound, ...)` (`src/relay/agent.py:246`), so `search_docs` can stay a **synchronous** `ToolSpec.execute` that makes a blocking `httpx.post` to Voyage without ever landing on the event loop and without triggering the async-contagion failure Pitfall 1 in milestone research warned about. There is no need for an async client, and there must not be one — `tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop` and the MCP sync path both assert the sync contract.

The two subtle reconciliations are both variations on code that already exists. **D-01/D-06 (whole-file embed, heading-level cite):** embed one vector per `.md` file, rank at doc level, return full file text (byte-compatible with today), and derive the citation `heading` as a *best-effort locator* — the `##` section within the matched doc whose text best overlaps the query, forming `id = "{doc}#{slug}"`. Headings are parsed at index-build time and stored in the artifact so query time needs no re-parse. **D-07 (citation guard):** thread a per-run `retrieved_ids: set[str]` into `_execute_guarded` exactly the way `bind_to_ticket` threads the run's ticket id, and on `send_reply` reject any cited id not in that set with a *recoverable* message — the identical pattern (recoverable denial + `guardrail` event + `denied_by` discriminator + run continues) that already ships for SEC-04 and is already tested (`tests/test_guardrails.py::test_run_recovers_after_binding_denial`).

The highest-leverage design call in the whole phase is **making `citations` optional (default `[]`) with subset-validation**, not required. D-07's locked wording — "rejects a reply citing any id *not retrieved*" — is subset validation, not "must cite ≥1". Optional-with-default keeps ~7 existing test files green (every scripted `send_reply` that omits citations validates as `[] ⊆ retrieved` and passes), preserves the SEC-04 denial only for the real failure (citing a hallucinated source), and leaves grounding-strength enforcement where it already lives — the eval judge's `grounded` boolean. Requiring citations is the alternative; it is more impressive as a demo artifact but breaks those seven files and sharpens the `ended_without_action` trap. Recommend optional.

**Primary recommendation:** New `src/relay/retrieval.py` (load `kb/index.json` once at `build_registry` time, numpy cosine, sync Voyage query via `httpx`, keyword fallback) + `scripts/build_index.py` (offline, `input_type="document"`, stamps `kb_sha256`) + a `tests/test_index.py` staleness gate that runs inside the existing `pytest -q` CI step (zero Voyage calls). Thread `retrieved_ids` through `_execute_guarded` beside `bound_ticket_id`; make `citations` optional and subset-validate. Calibrate the floor and run the D-11 before/after eval diff as one activity.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Embedding index build (Voyage `input_type="document"`) | Build-time script (`scripts/build_index.py`) | — | Runs offline on the maintainer's machine; commits the artifact. Never at cold start or in CI (D-08) |
| Index artifact storage | Repo / Docker image (`kb/index.json`, `COPY kb ./kb`) | — | Immutable, versioned with the code; **never** the Fly volume (stale-vector trap, milestone Pitfall 11) |
| Query embedding (Voyage `input_type="query"`) | API / Backend (`retrieval.py`, inside `to_thread`) | — | On the hot path of one tool call; offloaded off the event loop by the Phase 2 seam |
| Similarity ranking + floor | API / Backend (numpy, in-process) | — | 3×512 matrix; brute-force cosine is exact and instant; no vector DB (out of scope) |
| Hybrid union (keyword + semantic) | API / Backend (`search_docs`) | — | Keyword scorer already lives here; union keeps a working path when Voyage is down (D-05/D-10) |
| Citation id derivation (heading locator) | Build-time (heading parse) + query-time (best-fit pick) | — | Headings parsed once at build; the per-query locator is lexical, no extra embedding |
| Citation validation (cited ⊆ retrieved) | API / Backend (`agent._execute_guarded`) | — | The single choke point every tool call passes through; mirrors SEC-04 |
| Retrieved-id accumulation | API / Backend (`agent.run_ticket`, per-run `set`) | — | Must be per-run, never on the shared `app.state.registry` (cross-run race, milestone Pitfall 2) |
| Degradation signal | API / Backend emits `AgentEvent` → SSE | Browser (Phase 5/6 renders) | This phase only needs the event to exist; rendering is later |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `numpy` | `>=2.3,<3` (NOT `>=2.5`) | In-memory `(3, 512)` float32 matrix + cosine | Exact, instant, zero-infra for a 3-vector corpus. `numpy>=2.5` requires Python 3.12+ and breaks the project's `requires-python = ">=3.11"` floor [CITED: .planning/research/STACK.md, verified 2026-08-06] |
| `httpx` | `>=0.28,<1` (promote from dev extra to runtime) | Single POST to Voyage `/v1/embeddings` | Already transitively installed — `anthropic>=0.60` hard-depends on `httpx<1,>=0.25.0`. Use the **sync** `httpx.Client`/`httpx.post`; the call runs inside the Phase-2 `to_thread` seam [CITED: STACK.md; anthropic dependency confirmed] |
| Voyage embeddings API | model `voyage-4-lite`, `output_dimension=512` | Document + query embeddings | Current generation; 200M free tokens/month makes this demo effectively $0.00 (D-09) [VERIFIED: docs.voyageai.com/reference/embeddings-api, 2026-08-10] |

**Voyage request/response shape** [VERIFIED against official docs, 2026-08-10]:
```
POST https://api.voyageai.com/v1/embeddings
Authorization: Bearer $VOYAGE_API_KEY
Content-Type: application/json
{"input": ["..."], "model": "voyage-4-lite", "input_type": "query", "output_dimension": 512}
→ {"object":"list",
   "data":[{"object":"embedding","embedding":[...512 floats...],"index":0}],
   "model":"voyage-4-lite","usage":{"total_tokens": N}}
```
- `input_type` allowed values: `null` (default), `"query"`, `"document"`. Use `"document"` at index build, `"query"` at search (D-09).
- `voyage-4-lite` supported `output_dimension`: 2048, 1024, 512, 256 — 512 is valid.
- `input` accepts a string or an array of strings; batching all 3 docs in one index-build call is fine.

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `hashlib` (stdlib) | — | `kb_sha256` over sorted `kb/*.md` bytes | Index staleness stamp (D-08/RAG-02) |
| `json` (stdlib) | — | Read/write `kb/index.json` | The committed artifact — human-diffable, no binary in git |
| `re` (stdlib) | — | Parse `##` headings, slugify, keyword scorer | `search_docs` already uses `re` |
| `pydantic-settings` | 2.x (installed) | `RELAY_`-prefixed `voyage_api_key` with `validation_alias="VOYAGE_API_KEY"` | Extend the existing `Settings` class; do not add a second config mechanism (Pitfall 5) |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Raw `httpx` | `voyageai` SDK 0.5.0 | Pulls `requests` + `aiohttp` + `tokenizers` + `pillow` + `langchain-text-splitters` for one POST; duplicates the httpx stack `anthropic` ships. Off-brand for a "no framework" project [CITED: STACK.md] |
| numpy in-memory | `sqlite-vec` | Local Python `sqlite3` has `enable_load_extension=False`; cannot load in dev. Pure overhead below ~1k vectors [CITED: STACK.md, verified locally 2026-08-06] |
| `kb/index.json` (single file, embeddings as JSON arrays) | `kb/index.npz` + `kb/index.json` split (STACK's suggestion) | At 3 vectors, one JSON file is simpler, human-diffable, avoids committing a binary blob, and loads in one read. `.npz` earns its place only at hundreds+ of vectors. **Recommend single JSON.** |
| `voyage-4-lite` | `voyage-4` / `output_dimension=1024` | Only if the eval diff shows recall regression *after* confirming `input_type` is correct. Make model+dim config-driven so it's a one-line change [CITED: STACK.md variant guidance] |

**Installation:**
```bash
pip install "numpy>=2.3,<3"
# httpx is promoted from the [dev] extra to runtime dependencies (already transitive via anthropic)
```

`pyproject.toml` `[project] dependencies` additions:
```toml
"numpy>=2.3,<3",   # NOT >=2.5 — that requires Python 3.12+, project floor is 3.11
"httpx>=0.28,<1",  # promoted from dev extra; already transitive via anthropic
```
Remove `httpx>=0.27` from the `[dev]` extra (or leave it — a duplicate lower bound is harmless, but promoting to runtime is the correct expression).

**Version verification note:** `numpy` and `httpx` versions are carried from `.planning/research/STACK.md` (PyPI-verified 2026-08-06) and the Voyage model/endpoint/shape is re-confirmed against official docs today (2026-08-10). See Package Legitimacy Audit for the slopcheck caveat.

## Package Legitimacy Audit

> slopcheck and `pip`/`pip index` are **not available** in this research environment (no Python-on-PATH except a bare 3.14 interpreter without numpy/pip). Per protocol, packages are marked `[ASSUMED]` for version currency and the planner should keep a `checkpoint:human-verify` before the install task. Both packages are, however, first-party-adjacent and low-risk: `numpy` is the canonical scientific-Python package and `httpx` is already a transitive runtime dependency of the project's existing `anthropic` dependency (not a new supply-chain surface).

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| `numpy` | PyPI | Mature (2.x line) | Very high (top-10 PyPI package) | github.com/numpy/numpy | not run (unavailable) | **Approved** `[ASSUMED]` version — verify `>=2.3,<3` resolves on the CI 3.12 target |
| `httpx` | PyPI | Mature | Very high | github.com/encode/httpx | not run (unavailable) | **Approved** `[ASSUMED]` — already in the tree via `anthropic>=0.60` (`httpx<1,>=0.25.0`) |

**Packages removed due to slopcheck `[SLOP]` verdict:** none
**Packages flagged as suspicious `[SUS]`:** none
**Cross-ecosystem note:** both are PyPI packages consumed by a Python project — no npm/PyPI confusion vector.

## Architecture Patterns

### System Architecture Diagram

```
  BUILD TIME (offline, maintainer's machine — the ONLY Voyage-calling path)
  ┌──────────────────────────────────────────────────────────────────┐
  │ scripts/build_index.py                                            │
  │   read kb/*.md ─▶ parse ## headings ─▶ POST /v1/embeddings        │
  │      input_type="document", model, output_dimension=512           │
  │   compute kb_sha256(sorted kb/*.md bytes)                         │
  │   write kb/index.json {meta, docs:[{doc,headings,text,embedding}]}│
  └───────────────────────────┬──────────────────────────────────────┘
                              │ git commit kb/index.json  (COPY kb ./kb)
                              ▼
  CI (pytest -q, no VOYAGE key)     COLD START (Fly, no Voyage call)
  ┌──────────────────────────┐      ┌───────────────────────────────┐
  │ tests/test_index.py      │      │ lifespan → build_registry()   │
  │  recompute kb_sha256      │      │  retrieval.load_index() ONCE  │
  │  == index.json meta ?     │      │  (read kb/index.json; if hash │
  │   mismatch ▶ FAIL (RAG-02)│      │   mismatch or file missing ▶  │
  └──────────────────────────┘      │   keyword-only mode)          │
                                     └───────────────┬───────────────┘
                                                     ▼ closure captures
                                                       (index matrix, meta)
  QUERY TIME (per search_docs call, inside asyncio.to_thread — off-loop)
  ┌──────────────────────────────────────────────────────────────────┐
  │ search_docs(query)                                                │
  │   keyword_hits = existing scorer over kb/*.md                     │
  │   if VOYAGE key AND index loaded:                                 │
  │       try:  q = POST /v1/embeddings input_type="query" (httpx)    │
  │             scores = index_matrix @ normalize(q)                  │
  │             semantic_hits = [d for d,s in ... if s >= FLOOR]      │
  │             mode = "semantic"                                     │
  │       except (timeout/HTTP/parse):  semantic_hits=[]; degraded    │
  │   else: semantic_hits=[]; (mode="keyword", not "degraded")        │
  │   results = union(keyword_hits, semantic_hits)   ← whole-file text│
  │             each carries {doc, heading(best-fit), id, text, score}│
  │   return json {results, retrieval_mode}                          │
  └───────────────────────────┬──────────────────────────────────────┘
                              ▼ (result_json, is_error)  ← sync, arity unchanged
  agent.run_ticket loop:
     parse result once ─▶ if search_docs & not error:
                            retrieved_ids |= {r.id for r in results}
                          if result.retrieval_mode degraded ▶ emit run event (RAG-05)
     on send_reply ─▶ _execute_guarded(..., retrieved_ids=retrieved_ids)
                         cited = validated.get("citations", [])
                         missing = [c for c in cited if c not in retrieved_ids]
                         missing ▶ recoverable denial {denied_by:"citation"}  (D-07)
                                   run continues; model retries; guardrail event
```

### Recommended Project Structure
```
src/relay/
├── retrieval.py       # NEW  load_index(), cosine, sync Voyage query, keyword fallback
├── tools.py           # CHANGED  search_docs delegates ranking to retrieval.py; send_reply gains citations
├── agent.py           # CHANGED  retrieved_ids set threaded via bind_to_ticket/_execute_guarded; citation guard; degradation + citation guardrail events
├── guardrails.py      # CHANGED  SendReplyInput gains optional citations: list[str]
├── config.py          # CHANGED  voyage_api_key, voyage_model, voyage_dim, retrieval_floor
├── prompts.py         # CHANGED  instruct the model to cite retrieved ids in send_reply
└── db.py, models.py   # unchanged (models.py: AgentEvent.type comment may list the new event)
scripts/
└── build_index.py     # NEW  offline builder; input_type="document"; stamps kb_sha256
kb/
├── account.md, api.md, billing.md   # unchanged
└── index.json         # NEW committed artifact (shipped in image via COPY kb ./kb)
tests/
├── test_retrieval.py  # NEW  cosine, floor, hybrid union, keyword fallback, id shape
├── test_index.py      # NEW  kb_sha256 staleness gate (runs in existing CI pytest step)
├── test_tools.py      # CHANGED  search_docs result shape; send_reply citations arg
└── test_guardrails.py # CHANGED  add citation-guard tests (mirror the binding tests)
```

### Pattern 1: The committed index artifact + `kb_sha256` staleness gate
**What:** `scripts/build_index.py` reads `kb/*.md`, embeds each whole file with `input_type="document"`, and writes a single `kb/index.json`:
```json
{
  "meta": {"model": "voyage-4-lite", "output_dimension": 512,
           "input_type_document": "document", "kb_sha256": "<hex>"},
  "docs": [
    {"doc": "billing.md",
     "headings": ["Billing and Plans", "Refunds", "Upgrades and downgrades"],
     "text": "<full file text>",
     "embedding": [ ...512 floats... ]}
  ]
}
```
**Why single JSON, committed under `kb/`:** the Dockerfile already does `COPY kb ./kb` (Dockerfile:10), so the artifact ships in the image automatically — zero cold-start Voyage cost (D-08). **Never** write it to `/data` (the Fly volume outlives deploys and would serve vectors for KB text that no longer exists — milestone Pitfall 11).

**`kb_sha256`:** `hashlib.sha256` over the concatenation of `sorted(kb.glob("*.md"))` file bytes (exclude `index.json` itself). Same function is imported by both the builder (to stamp) and the staleness gate (to compare) so they cannot drift.

**CI staleness gate (RAG-02):** a pytest test — `tests/test_index.py::test_index_matches_kb` — recomputes the hash and asserts it equals `index.json`'s `meta.kb_sha256`. This runs inside the **existing** `pytest -q` step (`.github/workflows/ci.yml:22`) with **no** workflow edit and **no** Voyage key. Editing `kb/*.md` without rebuilding fails CI. (Optionally also give `build_index.py` a `--check` mode for local use; the pytest gate is the required one.)

**Runtime vs CI difference:** CI *fails* on mismatch; runtime *degrades* — `retrieval.load_index()` at startup, on missing file or hash mismatch, logs and falls back to keyword-only rather than crashing the demo (RAG-05). Two behaviors, same hash function.

### Pattern 2: Load once, query per-call — but keep the sync executor contract
**What:** `retrieval.load_index(kb_dir)` reads `kb/index.json` once and returns an in-memory object (L2-normalized `(N, 512)` float32 matrix + parallel metadata list). `build_registry` calls it at construction and the `search_docs` closure captures it — mirroring how the closure already captures `conn`/`kb_dir` (`src/relay/tools.py:96,140`). No per-call disk read (milestone perf trap).

**Query time** stays synchronous: `search_docs(...)` does a blocking `httpx.post` for the query embedding. This is safe because `agent.run_ticket` already wraps every tool call in `await asyncio.to_thread(execute_bound, ...)` (`src/relay/agent.py:246`) — verified, and asserted by `tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop` (thread-identity check). **Do not** make `search_docs` async: it would return a coroutine through the `Callable[..., str]` contract, break the MCP sync path (`call_mcp_tool`), and trip milestone Pitfall 1.

**Note on `build_registry` in evals:** `evals.run_case` calls `build_registry(conn, settings.kb_dir)` per case (12×), so `load_index` runs 12× per eval run. Reading a ~30 KB JSON 12 times is negligible; if it ever matters, memoize at module level keyed by `kb_dir`. Not worth doing now.

### Pattern 3: D-01/D-06 reconciliation — whole-file embed, heading-level cite
**The tension (Open Question 1):** D-01 embeds/returns whole files (one vector per doc), but D-06 requires `id = "{doc}#{heading}"`. A whole-doc result has *several* `##` headings, not one.

**Recommended resolution (best-effort locator):**
1. Rank at **doc level** using the whole-file embedding. Return the **full file text** (byte-compatible output, D-01).
2. For each returned doc, pick the `heading` as a *best-effort locator*: the `##` section whose text has the highest keyword overlap with the query. `id = f"{doc}#{slug(heading)}"` where `slug` lowercases and hyphenates (e.g. `billing.md#refunds`).
3. **Keyword-only hits (D-05):** identical heading-selection — pick the best-overlap section from the doc the keyword scorer matched. Same `id` format.
4. **Deterministic fallback:** if a doc has no `##` headings, or none overlap, use the doc's first heading, or `id = doc` with `heading = null`. State the chosen fallback in code.

This keeps the id anchored to a *real* heading that exists in the doc (RAG-03 "stable" = the anchor is real and reproducible), needs **no** per-heading embeddings (the locator is lexical), and reuses the parsed `headings` list already stored in `index.json`.

**Consequence for the citation guard:** the id the model must cite is whatever `search_docs` returned for that run. Because the guard checks `cited ⊆ retrieved` against the exact ids emitted this run, a query-dependent heading is fine — the model can only cite ids it was handed.

### Pattern 4: Similarity floor as the escalation-preserving gate (D-03/D-04)
Cosine over L2-normalized vectors ∈ [-1, 1]. Below `settings.retrieval_floor`, a semantic hit is dropped; if the union is empty, `search_docs` returns `{"results": []}` — the exact signal that pushes the model to `create_escalation` today.

**Calibration nuance (important, and non-obvious from the golden set):** of the four escalation-expecting cases, only **`salesforce-integration`** escalates *because the docs don't cover it* — that is the case the floor exists to protect (an off-topic query must return `[]`, not a confident-but-irrelevant doc). The other three escalations come from the model **reading doc text that says to escalate**: `refund-monthly` (`billing.md`: "refunds must be handled by a human billing agent"), `2fa-lockout` (`account.md`: lost 2FA + recovery codes → escalate priority high), `key-suspended` (`api.md`: sustained over-limit may suspend a key). For those three the retriever *should* return the relevant doc — the floor must **not** be so high it starves them. So calibrate the floor to sit **between** the score `salesforce-integration` gets against its nearest doc (must be below floor) and the lowest score a genuinely-relevant match gets (must be above floor). This is only measurable by running the eval — which is D-04/D-11's point.

### Pattern 5: The citation guard mirrors SEC-04 exactly (D-07)
**Thread a per-run `retrieved_ids` set the way `bind_to_ticket` threads the ticket id.** In `run_ticket`:
```python
retrieved_ids: set[str] = set()
execute_bound = bind_to_ticket(ticket["id"], retrieved_ids)   # set captured by reference
```
`bind_to_ticket(ticket_id, retrieved_ids=None)` closes over both and passes them to `_execute_guarded`. **Keep the returned executor's signature `(spec, name, raw_input, policy)`** — `tests/test_guardrails.py::test_the_agent_loop_takes_no_binding_argument_to_forget` asserts exactly those four params; `retrieved_ids` is a *constructor* arg, not an executor param, so that test stays green. Give it a default so `bind_to_ticket(TICKET["id"])` (used in that test) still works.

**Accumulate ids on the event loop, after the offloaded call returns** (single-threaded, no race). In the loop, after `payload = json.loads(result)`:
```python
if block.name == "search_docs" and not is_error:
    for r in payload.get("results", []):
        retrieved_ids.add(r["id"])
```
Because the set is the same object the closure holds, a later `send_reply` in a subsequent turn sees the accumulated ids.

**In `_execute_guarded`, after `validate_tool_input`, before `spec.execute`** (the choke point), add a check for `send_reply` — placed *after* the existing ticket-binding check:
```python
if (name == "send_reply" and retrieved_ids is not None):
    cited = validated.get("citations") or []
    missing = [c for c in cited if c not in retrieved_ids]
    if missing:
        return json.dumps({
            "error": (f"citation(s) {missing} were not retrieved in this run. "
                      f"Cite only ids returned by search_docs "
                      f"(retrieved this run: {sorted(retrieved_ids)}). "
                      f"Retry send_reply with valid citations."),
            "denied_by": "citation",
            "missing_citations": missing,
            "retrieved_ids": sorted(retrieved_ids),
        }), True
```
Then in `run_ticket`, add a `denied_by == "citation"` branch that emits a `guardrail` AgentEvent with `guard="citation"` **before** the `tool_result` event (cause-then-effect), mirroring the existing `binding_violation` branch (`src/relay/agent.py:250-277`). The MCP path passes no `retrieved_ids` (default `None`) → the check is skipped, exactly as `bound_ticket_id=UNBOUND` skips binding.

### Pattern 6: Graceful degradation as a run event (D-10/RAG-05)
`search_docs` sets `retrieval_mode` in its returned JSON:
- No `VOYAGE_API_KEY` or no index loaded → `"keyword"` (this is the **baseline** in CI/tests; **not** a degradation, no event).
- Voyage attempted but failed (timeout / HTTP error / parse error) → `"keyword"` **and** a `degraded: true` flag.

`run_ticket`, on a `search_docs` `tool_result` with `degraded: true`, emits an additive AgentEvent (recommend `type="notice"` with `data={"kind":"retrieval_degraded", ...}`, or reuse `type="guardrail"` with a distinct `guard` — planner's call; a new type is zero-cost because `main.py:234` serializes generically and `evals.extract_outcome` ignores unknown types). Also `logger.warning("retrieval.degraded", ...)`. Timeout ~10s + one retry inside `search_docs`; **never** let a Voyage failure end the run.

### Anti-Patterns to Avoid
- **Making `search_docs` (or any executor) `async`.** Breaks the sync `ToolSpec.execute` contract, the MCP path, and the no-coroutine guard test. Use sync `httpx` inside the existing `to_thread` seam.
- **Binding `retrieved_ids` onto `app.state.registry` or `build_registry`.** The registry is built once and shared by every concurrent run — a shared set would leak ids across runs and let one run cite another's docs. Per-run set, captured by the per-run `bind_to_ticket` closure (milestone Pitfall 2).
- **Requiring `citations` (min_length ≥ 1) without budgeting the test churn and the eval trap.** See Pitfall 1 and Pitfall 2 — recommend optional-with-subset-validation.
- **Chunking the KB.** D-02 locked out; measured to reduce grounding below the 0.8 gate.
- **Writing the index to `/data`** or **rebuilding it at startup.** Milestone Pitfall 11.
- **Same/omitted `input_type` at index and query.** Silent recall loss (D-09). `"document"` at build, `"query"` at search.
- **Changing `_execute_guarded`'s return arity** from `(str, bool)`. `mcp_server.call_mcp_tool` unpacks exactly two. Signal citation denial via a `denied_by` field, as SEC-04 already does.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Query embedding | A hand-rolled HTTP retry/backoff stack or the `voyageai` SDK | `httpx.post` with `timeout=` + one manual retry | One POST; the SDK's transitive weight is off-brand and large (STACK) |
| Cosine / top-k | Hand-written dot loops | `numpy`: `mat @ q_norm`, `np.argsort` | Exact, vectorized, ~3 lines; corpus is 3 vectors |
| Off-loop execution | New async plumbing | The existing `asyncio.to_thread(execute_bound, ...)` in `agent.py` | Phase 2 already built and tested it |
| Recoverable denial + `guardrail` event + `denied_by` discriminator | A new denial mechanism | Copy the SEC-04 ticket-binding path in `agent.py` verbatim, s/ticket_binding/citation/ | Already shipped, already tested, already eval-safe |
| SSE framing for the degradation/citation events | Special-case serialization | The generic `f"event: {event.type}\ndata: {json}\n\n"` (`main.py:234`) | Type-agnostic — a new `AgentEvent.type` needs zero serialization changes |
| Per-run constraint injection into the executor | Mutating registry state per run | The `bind_to_ticket` closure pattern | Cross-run-race-safe by construction |

**Key insight:** every genuinely new line in this phase is either numpy cosine (~10 lines), one `httpx.post` (~8 lines), heading-slug selection (~15 lines), the `kb_sha256` helper (~5 lines), and a citation branch that is a near-verbatim copy of the SEC-04 branch. If a task is writing a retrieval framework, a retry library, or a new denial mechanism, it is re-implementing something on this list.

## Common Pitfalls

### Pitfall 1: A rejected `send_reply` regresses the eval suite via `ended_without_action` (the D-07 "Watch")
**What goes wrong:** `agent.py:294` sets `resolved_via` only when `not is_error and block.name in TERMINAL_TOOLS`. A citation denial is `is_error=True` → `resolved_via` stays `None`; if the model then stops (`stop_reason=="end_turn"`), `run_ticket` yields `error: ended_without_action` (`agent.py:318`). `evals.run_case` computes `action_ok = outcome["action"] == case["expected_action"]` → the case fails; enough failures drop below the 0.8 CI gate.
**Why it happens:** the citation denial is a new state transition through code that assumed denials only came from dry-run policy.
**How to avoid:** phrase the denial as a **recoverable instruction** (include `retrieved_ids` and "retry with valid citations"), exactly as SEC-04 does — the model retries in-run and resolves normally. `tests/test_guardrails.py::test_run_recovers_after_binding_denial` is the template: assert (a) no premature resolution on the bad call, (b) a `guardrail` event fired, (c) the run still reaches `resolution` after retry.
**Warning signs:** a spike of `error:ended_without_action` in `runs.outcome`; eval pass rate dropping on previously-passing cases; the before/after eval diff (D-11) showing regressions concentrated on `send_reply` cases.

### Pitfall 2: `citations` required-vs-optional decides how many existing tests break
**What goes wrong:** if `SendReplyInput.citations` is **required** (min ≥ 1) and the `send_reply` executor signature grows a required `citations` param, then every scripted `send_reply` in the suite breaks. Grep found send_reply driven **without** citations in: `tests/helpers.py::TicketAwareFakeClient` (line 50), `tests/test_guardrails.py` (lines 75, 106, 223, 238, 258, 273, 305, 337), `tests/test_lifecycle.py` (429, 471, 506 + the concurrency path via `TicketAwareFakeClient`), `tests/test_observability.py` (27), `tests/test_mcp.py` (57, 75), `tests/test_tools.py` (49), `tests/test_db.py` (270). That is ~7 files.
**How to avoid (recommended):** make `citations` **optional** — `citations: list[str] = Field(default_factory=list)` on `SendReplyInput` — and give the executor a defaulted param: `send_reply(db, ticket_id, body, citations=())`. Then:
- `[] ⊆ retrieved` always passes → every citation-less scripted call behaves as today (guard doesn't fire).
- Direct calls like `registry["send_reply"].execute(ticket_id=…, body=…)` and `send_reply(db, ticket_id, body)` still work (defaulted param).
- MCP send_reply (no `retrieved_ids`) skips the subset check; Pydantic accepts the missing optional field.
This reduces forced breakage to **near zero** — only *new* tests (the citation guard) and the `search_docs` result-shape assertions need touching. It also matches D-07's locked wording ("rejects a reply citing any id *not retrieved*" = subset validation, not "must cite ≥1").
**The alternative (required citations):** stronger demo artifact, but breaks the ~7 files above and sharpens Pitfall 1. If chosen, budget a task to add a `search_docs` step + valid citations to every scripted `send_reply`. **Flag as a decision; recommend optional.**
**Note on grounding:** with optional citations, grounding-strength stays enforced by the eval judge's `grounded` boolean and the system prompt (Pattern in `prompts.py` — instruct the model to cite). The guard's job is narrow: catch *hallucinated* citations, not mandate citing.

### Pitfall 3: The index is rebuilt at cold start, or goes stale on the volume
**What goes wrong:** building at startup pays Voyage latency/cost on every scale-to-zero cold start and breaks the CI docker smoke test (no `VOYAGE_API_KEY`, only `ANTHROPIC_API_KEY=ci-placeholder` — `.github/workflows/ci.yml:32`). Persisting to `/data` serves vectors for KB text that no longer exists after a redeploy.
**How to avoid:** build offline, commit `kb/index.json`, ship it in the image (`COPY kb ./kb`). Hash-gate in CI (Pattern 1). Runtime reads, never rebuilds.
**Warning signs:** `/health` latency growing after the change; docker CI job timing out; an index file in `.gitignore` or on the volume.
**Verification (must hold):** the full suite passes with **no** `VOYAGE_API_KEY` set (CI has none — confirmed `VOYAGE_API_KEY NOT set` in this environment), and the docker smoke test comes up. Both depend on keyword fallback being the no-key baseline.

### Pitfall 4: `RELAY_` prefix silently renames the Voyage key
**What goes wrong:** adding `voyage_api_key: str | None = None` to `Settings` reads it from `RELAY_VOYAGE_API_KEY`, not `VOYAGE_API_KEY`. On Fly you'd `fly secrets set VOYAGE_API_KEY=...` and get `None` at query time — an unhelpful failure at first query, not startup.
**How to avoid:** mirror the existing `ANTHROPIC_API_KEY` escape hatch (`config.py:22`): `Field(default=None, validation_alias="VOYAGE_API_KEY")`. Pass the key explicitly to the `httpx` request. Add `VOYAGE_API_KEY=` to `.env.example` next to `ANTHROPIC_API_KEY`, and document `fly secrets set VOYAGE_API_KEY=...` in the README.
**Warning signs:** two spellings of the key across `fly.toml`, README, `config.py`; 401s from Voyage despite the secret being set.

### Pitfall 5: Voyage cost is invisible to `RunBudget` and `/metrics`
**What goes wrong:** `RunBudget` prices only Claude tokens (`guardrails.py:79-98`); the Voyage query cost is not tracked. Not a correctness bug at 200M free tokens/month, but the cost dashboard would silently exclude it.
**How to avoid:** document explicitly (README + a code comment) that embedding cost is out-of-band and effectively $0 within the free tier; index build is offline. Do **not** bolt embedding cost into `RunBudget` this phase — it's noise at this scale and would complicate the accumulator. Flag it so the omission is a stated decision, not an accident.

### Pitfall 6: `input_type` / model / dimension drift between index and query
**What goes wrong:** querying with a different `model` or `output_dimension` than the index was built with produces either a shape error or — worse — silently meaningless similarities.
**How to avoid:** store `model`, `output_dimension`, and the document `input_type` in `index.json` `meta`; at `load_index`, refuse (fall back to keyword + log) if the runtime `settings.voyage_model`/`voyage_dim` don't match the artifact. Pin all three in `config.py`.
**Warning signs:** all cosine scores near-identical or nonsensical; eval grounding collapses uniformly across cases.

## Code Examples

### Example 1: Offline index build (`scripts/build_index.py`)
```python
# Source: pattern from .planning/research/STACK.md §3; Voyage shape [VERIFIED docs 2026-08-10]
import hashlib, json, re
from pathlib import Path
import httpx

KB = Path("kb")
MODEL, DIM = "voyage-4-lite", 512

def kb_sha256(kb: Path = KB) -> str:
    h = hashlib.sha256()
    for p in sorted(kb.glob("*.md")):          # index.json is not *.md — excluded
        h.update(p.read_bytes())
    return h.hexdigest()

def headings(text: str) -> list[str]:
    return re.findall(r"^##?\s+(.*)$", text, flags=re.MULTILINE)

def embed(texts: list[str], input_type: str, key: str) -> list[list[float]]:
    r = httpx.post("https://api.voyageai.com/v1/embeddings",
                   headers={"Authorization": f"Bearer {key}"},
                   json={"input": texts, "model": MODEL,
                         "input_type": input_type, "output_dimension": DIM},
                   timeout=30.0)
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]
# main(): read docs, embed(texts, "document", key), write kb/index.json with meta.kb_sha256
```

### Example 2: Query-time retrieval (`retrieval.py`, sync — runs inside `to_thread`)
```python
# numpy cosine over the prebuilt matrix; httpx sync; keyword fallback on any failure
import numpy as np, httpx

def semantic_scores(index, query: str, key: str) -> np.ndarray | None:
    try:
        r = httpx.post("https://api.voyageai.com/v1/embeddings",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"input": [query], "model": index.model,
                             "input_type": "query", "output_dimension": index.dim},
                       timeout=10.0)
        r.raise_for_status()
        q = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
        q /= np.linalg.norm(q) or 1.0
        return index.matrix @ q            # matrix is pre-normalized at load
    except Exception:                      # noqa: BLE001 — degrade, never end the run
        return None                        # caller marks retrieval_mode degraded
```

### Example 3: Result shape (D-06) — the join key
```python
# Each result: whole-file text (D-01), heading a best-effort locator (Pattern 3)
{"doc": "billing.md", "heading": "Refunds", "id": "billing.md#refunds",
 "text": "<full billing.md text>", "score": 0.83}
# search_docs returns: {"results": [...], "retrieval_mode": "semantic"|"keyword", "degraded": bool}
```

### Example 4: Citation guard in `_execute_guarded` (mirrors SEC-04)
```python
# after validate_tool_input, after the ticket_binding check, before spec.execute
if name == "send_reply" and retrieved_ids is not None:
    cited = validated.get("citations") or []
    missing = [c for c in cited if c not in retrieved_ids]
    if missing:
        return json.dumps({
            "error": (f"citation(s) {missing} were not retrieved this run. "
                      f"Cite only ids from search_docs (retrieved: {sorted(retrieved_ids)}). "
                      f"Retry send_reply with valid citations."),
            "denied_by": "citation", "missing_citations": missing,
            "retrieved_ids": sorted(retrieved_ids),
        }), True
```

### Example 5: CI staleness gate (`tests/test_index.py`) — zero Voyage calls
```python
import json
from pathlib import Path
from relay.retrieval import kb_sha256   # the SAME function the builder stamps with

def test_index_matches_kb():
    meta = json.loads((Path("kb") / "index.json").read_text())["meta"]
    assert meta["kb_sha256"] == kb_sha256(), \
        "kb/*.md changed without rebuilding kb/index.json — run scripts/build_index.py"
```

## Runtime State Inventory

> This phase edits code and commits one new artifact. It is not a rename/migration, but the index artifact has real cross-lifecycle state worth an explicit inventory.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `kb/index.json` (new committed artifact). No existing datastore keys change | Build once offline, commit; regenerate whenever `kb/*.md` changes (the sha gate enforces it) |
| Live service config | Fly secret `VOYAGE_API_KEY` (new) — lives in Fly's secret store, not git | `fly secrets set VOYAGE_API_KEY=...`; document in README next to `ANTHROPIC_API_KEY` |
| OS-registered state | None — verified (no scheduler/daemon touches retrieval) | none |
| Secrets/env vars | `VOYAGE_API_KEY` read via `validation_alias` (code); `.env.example` gains a line | Add to `.env.example`; ensure `config.py` reads the un-prefixed name (Pitfall 4) |
| Build artifacts | `kb/index.json` is shipped in the Docker image (`COPY kb ./kb`, Dockerfile:10) — no separate build step | Confirm the docker smoke test comes up reading the committed index with no Voyage key |

**The canonical question — after every file is updated, what still holds old state?** The Fly volume must **not** hold an index (never written there). The only mutable coupling is `kb/*.md` ↔ `kb/index.json`, and the `kb_sha256` gate is the forcing function that keeps them in sync.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `search_docs` keyword term-counting over `kb/*.md` (`tools.py:48`) | Hybrid keyword + `voyage-4-lite` semantic, floor-gated | This phase | Ranking changes; output stays full-doc text (D-01) |
| `voyage-3.x` (legacy, no free tier) | `voyage-4` family (voyage-4-lite/4/4-large), Matryoshka dims 256/512/1024/2048, 32K context, 200M free tok/mo | Voyage 4 GA | Correct model choice is free and better [VERIFIED docs 2026-08-10] |
| Uncited replies | Structured `citations` on `send_reply`, executor-validated | This phase | Makes grounding machine-checkable (Phase 4 eval join key) |

**Deprecated/outdated:** `voyage-3.5`/`voyage-3-large` — legacy, no free tier; do not select. Do not add `sqlite-vec` (local `enable_load_extension=False`) or any vector DB (out of scope).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `numpy>=2.3,<3` and `httpx>=0.28,<1` are current/legit (slopcheck + pip unavailable this session) | Standard Stack / Package Audit | LOW — both are top-tier PyPI packages; httpx already transitive via anthropic. Planner should `pip index versions` before install |
| A2 | `voyage-4-lite` remains available at `output_dimension=512` and pricing/free-tier holds | Standard Stack / D-09 | MEDIUM — confirmed vs docs today; model catalogs move. `build_index.py` failing loudly on an unknown model is the safety net; model is config-driven |
| A3 | Best-effort lexical heading-locator satisfies D-06 "stable id" intent | Pattern 3 / Open Q1 | MEDIUM — a query-dependent heading is functionally safe for the guard but the planner/discuss should confirm the id-derivation rule |
| A4 | `citations` optional-with-subset-validation is the correct reading of D-07 | Pitfall 2 | MEDIUM — if the user wants *mandatory* citing, ~7 test files change and Pitfall 1 sharpens. Surface before planning |
| A5 | The floor sits between salesforce's nearest-doc score and true-match scores | Pattern 4 / D-04 | MEDIUM — only measurable by the eval; that is D-04/D-11's purpose |
| A6 | Local dev on Python 3.14 can install a `>=2.3,<3` numpy wheel | Environment | LOW — CI/Docker are 3.12 (fine); a dev on 3.14 may need the newest 2.x or to use 3.12 |

## Open Questions

1. **How is the citation `heading`/`id` derived for a whole-file result?** (D-01 vs D-06.)
   - Known: rank whole docs, return full text; id must be `{doc}#{heading}`.
   - Unclear: which heading, since a doc has several.
   - Recommendation: best-fit `##` section by keyword overlap → `id = {doc}#{slug(heading)}`; deterministic first-heading fallback (Pattern 3). Confirm the rule in discuss-phase.

2. **Are citations mandatory or optional on `send_reply`?**
   - Recommendation: optional + subset-validated (Pitfall 2). Confirm before planning — it changes the test-breakage scope.

3. **New event type for degradation — `notice`/`degraded`, or reuse `guardrail`?**
   - Either is additive and zero-cost. Recommendation: a distinct `type` (e.g. `"notice"` with `data.kind="retrieval_degraded"`) so the Phase 5/6 dashboard can style it separately from denials. Low stakes.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `numpy` | cosine ranking (RAG-01) | ✗ (local 3.14 venv) | — | Must be added to deps; installs on CI 3.12 |
| `httpx` | Voyage query (RAG-01) | likely (transitive via anthropic) | — | Promote to runtime dep; sync client |
| `VOYAGE_API_KEY` | index build + live semantic query | ✗ (not set locally; not in `.env`) | — | **Keyword fallback (D-10)** — this is the CI/cold-start baseline, not an error |
| Voyage API reachable | index build (offline), live query | assumed | `voyage-4-lite` | Keyword fallback at query time; build fails loudly if unreachable |
| Python (`python`/`pip`) on PATH | any local verification | ✗ (only bare `python3` 3.14, no pip/numpy) | 3.14.6 | Planner/executor runs in the project venv where `pip install -e .[dev]` is done |

**Missing dependencies with no fallback:** none block *execution* — `numpy`/`httpx` are added via `pip install -e .[dev]`; the CI/test path deliberately runs Voyage-free.
**Missing dependencies with fallback:** `VOYAGE_API_KEY` unset → keyword scorer (by design). The eval run (D-11) is the one activity that **requires** a real `VOYAGE_API_KEY` + `ANTHROPIC_API_KEY` and real spend (approved).

## Validation Architecture

> Nyquist enabled (`workflow.nyquist_validation` not disabled).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | `pytest>=8.0` + `pytest-asyncio>=0.23` (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths = ["tests"]`) |
| Quick run command | `pytest tests/test_retrieval.py tests/test_index.py -x -q` |
| Full suite command | `pytest -q` (then, gated separately, `python -m relay.evals --concurrency 4 --threshold 0.8`) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| RAG-01 | cosine ranking + floor + `input_type="query"` produces ranked whole-file results | unit | `pytest tests/test_retrieval.py -x` | ❌ Wave 0 |
| RAG-01 | `search_docs` stays sync & runs off the loop | unit (existing) | `pytest tests/test_lifecycle.py::test_tool_execution_runs_off_the_event_loop -x` | ✅ |
| RAG-02 | `kb_sha256` staleness gate fails on KB edit without rebuild | unit | `pytest tests/test_index.py::test_index_matches_kb -x` | ❌ Wave 0 |
| RAG-02 | full suite green with **no** `VOYAGE_API_KEY` (CI/cold-start path) | integration | `VOYAGE_API_KEY= pytest -q` | ✅ (suite exists; add no-key assertion) |
| RAG-03 | result carries `{doc, heading, id, text, score}`; id = `{doc}#{heading}` | unit | `pytest tests/test_retrieval.py -k citation_id -x` | ❌ Wave 0 |
| RAG-03 | off-topic query returns `{"results": []}` (floor) | unit | `pytest tests/test_retrieval.py -k empty -x` | ❌ Wave 0 |
| RAG-04 | `send_reply` citing an unretrieved id is denied, run recovers | unit | `pytest tests/test_guardrails.py -k citation -x` | ❌ Wave 0 (mirror the binding tests) |
| RAG-04 | citing a retrieved id succeeds; `[] ⊆ retrieved` passes (back-compat) | unit | `pytest tests/test_guardrails.py -k citation -x` | ❌ Wave 0 |
| RAG-05 | Voyage failure → keyword results + degradation event, run not ended | unit | `pytest tests/test_retrieval.py -k degrade -x` and a `run_ticket` degradation-event test | ❌ Wave 0 |
| all (D-11) | 12-case eval pass rate ≥ pre-change baseline, per-case diff | e2e (paid) | `python -m relay.evals --concurrency 4 --threshold 0.8` | ✅ harness exists (manual/`workflow_dispatch`) |

### Sampling Rate
- **Per task commit:** `pytest tests/test_retrieval.py tests/test_index.py tests/test_guardrails.py -x -q`
- **Per wave merge:** `pytest -q` (full suite, must be green with `VOYAGE_API_KEY` unset)
- **Phase gate:** full suite green **and** the D-11 before/after eval diff recorded (chosen floor + per-case table) as acceptance evidence before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_retrieval.py` — cosine, floor→empty, hybrid union, keyword fallback/degrade, citation-id shape (RAG-01/03/05)
- [ ] `tests/test_index.py` — `kb_sha256` staleness gate (RAG-02), runs in existing CI pytest step
- [ ] `tests/test_guardrails.py` — citation-guard tests mirroring `test_mismatched_ticket_id_is_denied` / `test_binding_denial_emits_guardrail_event` / `test_run_recovers_after_binding_denial` (RAG-04)
- [ ] Update `tests/test_tools.py` — `search_docs` result-shape assertions; `send_reply` `citations` arg (keep back-compat with defaulted param)
- [ ] Add a no-`VOYAGE_API_KEY` assertion (keyword baseline) — a fixture or an explicit `monkeypatch.setattr(settings, "voyage_api_key", None)` test
- [ ] `kb/index.json` must exist and be committed before `test_index.py` can pass — the build script is a prerequisite artifact, not a test
- [ ] Framework install: `pip install -e ".[dev]"` after adding `numpy`/`httpx` to `pyproject.toml`

## Security Domain

> `security_enforcement` not disabled → included.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth surface (Phase 1 owns it); `VOYAGE_API_KEY` is an outbound credential |
| V3 Session Management | no | Stateless; no sessions |
| V4 Access Control | yes (indirect) | The citation guard is an *integrity* control on model output (LLM06 excessive agency), reusing the SEC-04 choke point |
| V5 Input Validation | yes | `citations` validated by Pydantic (`list[str]`) then subset-checked against retrieved ids; query length already capped (`SearchDocsInput`, max 500) |
| V6 Cryptography | no | `hashlib.sha256` used only as a content fingerprint (not a security boundary) |
| V7 Errors & Logging | yes | Degradation/denial logged with dotted event names; **do not** log the `VOYAGE_API_KEY` or full doc text via `ctx` passthrough |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Model cites a fabricated source id (hallucinated grounding) | Spoofing / Tampering | Executor rejects `cited ⊄ retrieved` (RAG-04, Pattern 5) |
| Prompt-injected reply body still writes to the run's ticket | Elevation of privilege | Unchanged SEC-04 ticket binding — the citation check is *added after* it, not instead of it |
| Outbound API key leakage | Information disclosure | `validation_alias` + Fly secret; never in `ctx`/logs/query strings (Pitfall 4) |
| Voyage outage ends every run (availability) | Denial of service | Keyword fallback + timeout + surfaced degradation (D-10/RAG-05) |
| Stale vectors after KB edit | Tampering (silent) | `kb_sha256` gate: CI fails, runtime degrades (RAG-02) |

## Sources

### Primary (HIGH confidence)
- `docs.voyageai.com/reference/embeddings-api` — endpoint, request/response fields, `input_type` values, `voyage-4-lite` + `output_dimension=512` [VERIFIED 2026-08-10]
- Codebase read directly: `src/relay/agent.py` (`_execute_guarded`, `bind_to_ticket`, `to_thread` seam, SEC-04 guardrail branch), `tools.py` (`search_docs`/`send_reply`, `build_registry` closures), `guardrails.py` (`SendReplyInput`, `RunBudget`), `config.py` (`validation_alias` pattern), `db.py` (`Database` class — Phase 2), `main.py` (SSE framing, lifespan), `mcp_server.py` (sync `call_mcp_tool` arity), `evals.py` (`extract_outcome`, `run_case`), `prompts.py`
- Test suite grep + read: `tests/test_guardrails.py` (binding-test templates + signature assertion), `tests/test_lifecycle.py` (off-loop assertion), `tests/helpers.py`, `tests/conftest.py`, `tests/test_tools.py`, `tests/test_mcp.py`, `tests/test_db.py` — the citations back-compat surface
- `evals/golden.jsonl` (12 cases) + `kb/*.md` (381 words, 3 files) — floor-calibration reasoning
- `.github/workflows/ci.yml` (pytest + docker smoke, no Voyage key), `evals.yml` (`workflow_dispatch`, threshold 0.8), `Dockerfile` (`COPY kb ./kb`)

### Secondary (MEDIUM-HIGH confidence)
- `.planning/research/STACK.md` — numpy/httpx versions (PyPI-verified 2026-08-06), `voyageai`-SDK-vs-httpx, sqlite-vec rejection, single-JSON-vs-npz
- `.planning/research/PITFALLS.md` §Pitfall 1/10/11/13 — async contagion, eval regression, index staleness, `RELAY_` key rename
- `.planning/research/FEATURES.md` — citation shape as RAG↔reply↔eval↔dashboard join key
- `.planning/phases/01-security-perimeter/01-RESEARCH.md` — the SEC-04 recoverable-denial + `guardrail` event pattern this phase mirrors

### Tertiary (LOW confidence / could not verify this session)
- numpy/httpx exact current versions — slopcheck and `pip`/`pip index` unavailable in this environment (bare Python 3.14, no pip). Marked `[ASSUMED]`; planner should confirm on the CI 3.12 target before install.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — Voyage shape verified vs docs; numpy/httpx carried from PyPI-verified STACK.md + already-in-tree httpx
- Architecture (sync-executor-in-to_thread, retrieved_ids threading, citation guard): HIGH — read against the exact code that already implements the SEC-04 analogue and the off-loop assertion test
- D-01/D-06 heading reconciliation & floor value: MEDIUM — best-effort locator is reasoned, not measured; floor is a calibration output (D-04/D-11)
- Test-breakage surface: HIGH — enumerated by grepping every `send_reply`/`search_docs` call in the suite
- Package legitimacy: MEDIUM — slopcheck unavailable; both packages are top-tier/first-party-adjacent

**Research date:** 2026-08-10
**Valid until:** 2026-09-10 for stack/versions (Voyage catalogs move; re-confirm the model name if planning slips); codebase/pattern findings valid until `agent.py`/`tools.py` are next refactored

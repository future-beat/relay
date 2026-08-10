# Phase 4: Evaluation Coverage - Research

**Researched:** 2026-08-10
**Domain:** Eval-harness instrumentation (retrieval metrics, guardrail assertions, citation faithfulness) over an existing hand-written agent loop
**Confidence:** HIGH — every claim below is verified against the actual code in this repo, not against training data.

> **CONTEXT.md status:** No `.planning/phases/04-evaluation-coverage/04-CONTEXT.md` exists yet — this research ran ahead of `discuss-phase`. The `D-01..D-08` decisions referenced below were supplied inline by the orchestrator, not read from a locked CONTEXT.md. Treat them as the working constraint set; `discuss-phase` should confirm them and write the canonical CONTEXT.md. The `## User Constraints` section reproduces the orchestrator's framing verbatim; anything I inferred beyond it is tagged `[ASSUMED]` in the Assumptions Log.

<user_constraints>
## User Constraints (from orchestrator framing — CONTEXT.md not yet written)

### Locked Decisions (as provided)
- **D-01:** The work splits along the existing **free-CI vs paid-dispatch** line. Deterministic, zero-cost checks run in `ci.yml` (`pytest -q`); anything needing a live model or a Voyage query embedding runs only in `evals.yml` (`workflow_dispatch`).
- **D-03:** Retrieval metrics (recall@k, MRR) are **report-only, not gated**. The pytest that runs for free asserts a **soft floor** (`recall@3 > 0`), NOT a hard threshold. The only hard CI gate remains the existing 12-case pass-rate `--threshold 0.8` in `evals.yml`.
- **D-08:** The one genuinely new piece of machinery is a **denial-recovery seeding hook** — an eval-only mechanism that threads a dummy id into a run's `retrieved_ids` set and drops a real one, so exactly one real citation denial fires and a real model can be observed recovering from it. It must stay OUT of the default (free) suite's behaviour and out of the production signature's normal path.

### Claude's Discretion
- Exact metric definitions and label granularity (doc-level vs heading-level).
- Where the labeled retrieval set lives (`evals/retrieval.jsonl` vs extending `golden.jsonl`).
- Where each deterministic assertion physically lives (`tests/` vs the harness).
- The precise signature/placement of the D-08 seeding hook.

### Deferred Ideas (OUT OF SCOPE)
- README keyword-vs-semantic recall comparison (needs the numbers this phase produces first).
- Eval-results panel on the dashboard (Phase 5/6; needs an artifact-storage decision).
- Citation-faithfulness **LLM-judge** criterion (semantic half of EVAL-03) — this phase does the **structural/deterministic** half only.
- Rejected-action counter as a dashboard metric.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| EVAL-01 | Retrieval eval set (labeled query → relevant chunk ids) reports recall@k and MRR, wired into the existing harness; the existing 12-ticket suite does not regress below its CI threshold | `retrieve()` already supports `max_results=k` and returns doc/id/anchors; labels can reference real `kb/index.json` ids (verified below). Metric is pure Python over `retrieve()`. Report field added to `run_evals()` output. The 12-case `--threshold 0.8` gate in `evals.yml` is untouched. |
| EVAL-02 | Prompt-injection golden case (ticket body attempting to act on another ticket) asserts the SEC-04 guard fires | The guard exists and is exercised (`agent.py:129-145`, `denied_by="ticket_binding"` + `guardrail` event); `tests/test_guardrails.py` already has the deterministic pattern (`_seed_tickets`, `VICTIM_TICKET`). EVAL-02 adds an injection-flavoured case asserting event + no victim write. Free/deterministic. |
| EVAL-03 | Citation-faithfulness check: every chunk id cited in a reply was retrieved in that run (deterministic; no LLM judge) | `extract_outcome()` **already** records `citations` and `retrieval.retrieved_ids` (WR-10 fix, verified). The deterministic subset check `cited ⊆ retrieved` needs **no `run_case` change**. The recovery hook (D-08) is the only new code. |
</phase_requirements>

## Summary

Phase 4 is overwhelmingly a **composition-and-assertion** phase, not a build phase. The instrumentation the three requirements assert on already exists and was verified line-by-line:

- **SEC-04** binding guard (EVAL-02): live in `agent.py:129-145`, emits a `guardrail` SSE event and a structured log, is recoverable in-run, and is already covered by deterministic tests using seeded real ticket rows.
- **Citation guard + `retrieved_ids`** (EVAL-03): live in `agent.py:150-172`; `extract_outcome()` in `evals.py:90-140` already emits `citations` (with the load-bearing `None`-vs-`[]` distinction) and `retrieval.retrieved_ids` (the full accept-set: doc name + located id + every anchor). `tests/test_evals.py` already asserts `cited ⊆ retrieved` at the unit level.
- **`retrieve()`** (EVAL-01): live in `retrieval.py:246-327`, takes `max_results=k`, returns `{doc, heading, id, anchors, text, score}`, and degrades to keyword mode with **no** Voyage call when no key is set — which is exactly what makes a free CI soft-floor possible.

Two genuinely new artifacts are needed: a **labeled retrieval set** (`evals/retrieval.jsonl`) whose query→id labels reference real `kb/index.json` ids, and the **D-08 denial-recovery seeding hook** — a small, opt-in, eval-only injection point in `run_ticket` that forces one real citation denial so a paid run can observe a real model recovering (the one thing Phase 3 explicitly could not close; see `03-06-SUMMARY.md` "Still untested" and `03-REVIEW.md` WR-10).

**Primary recommendation:** Add a pure `recall_at_k`/`mrr` metric module driven by `retrieve()`; commit `evals/retrieval.jsonl` with doc-level relevance labels; add three deterministic **pytests** (they ride the existing free `pytest -q` step, so `ci.yml` needs no edit); add the D-08 seeding hook as a keyword-only, default-off parameter on `run_ticket`; surface semantic recall@k/MRR as a **report-only** field produced by the paid `evals.yml` dispatch (add `VOYAGE_API_KEY` to that job). Do **not** gate on retrieval metrics — the only hard gate stays the existing 12-case pass-rate.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| recall@k / MRR computation (EVAL-01) | Eval harness (`evals.py`) | Retrieval (`retrieval.py`, read-only) | Metric is a pure function over `retrieve()`; it must not change ranking behaviour, only measure it. |
| Labeled retrieval set (EVAL-01) | Eval data (`evals/retrieval.jsonl`) | KB index (`kb/index.json`, referenced) | Data artifact; labels are ids the index already defines. |
| Injection assertion (EVAL-02) | Test suite (`tests/`) | Agent loop (`agent.py`, guard under test) | Free/deterministic assertion; the guard itself is a Phase 1 concern already shipped. |
| Citation subset check (EVAL-03 structural) | Test suite (`tests/`) + harness report field | `evals.py` `extract_outcome` (already emits data) | The data is already recorded; this phase only asserts the property over it. |
| Denial-recovery seeding hook (EVAL-03, D-08) | Agent loop (`agent.py` `run_ticket`, opt-in param) | Paid eval path (`evals.py` + `evals.yml`) | Must live where `retrieved_ids` is created/grown; kept opt-in so production and free tests are unaffected. |
| Free-CI vs paid-dispatch wiring (D-01) | CI (`.github/workflows/*.yml`) | — | Cost boundary: keyless/deterministic → `ci.yml`; keyed/model → `evals.yml`. |

## Standard Stack

No new runtime or dev dependencies are required. Everything needed is already in `pyproject.toml`.

### Core (already present)
| Library | Version (installed) | Purpose | Why standard here |
|---------|--------------------|---------|-------------------|
| `numpy` | 2.5.2 (in `.venv`) | Cosine ranking inside `retrieve()`; trivial arithmetic for recall/MRR | Already a transitive dep via `retrieval.py`; no need to add anything for metric math. |
| `pytest` + `pytest-asyncio` | `>=8.0` / `>=0.23`, `asyncio_mode="auto"` | The free deterministic EVAL-02/EVAL-03/soft-floor checks | The repo's only test runner; new tests ride the existing `pytest -q` CI step. |
| `anthropic` (`AsyncAnthropic`) | `>=0.60` | Drives the paid recovery-hook case in `evals.yml` | Already the harness's client. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| A separate `evals/retrieval.jsonl` | Extending `golden.jsonl` with a `relevant_ids` field | Rejected: `golden.jsonl` rows are ticket-shaped (`customer_email/subject/body/expected_action`) and the model sends **topical phrases**, not ticket bodies, as `search_docs` queries (documented in `03-06-SUMMARY.md` "Calibration used two query shapes"). A dedicated file lets the query be the actual model-style phrase and keeps the two datasets from coupling. |
| Doc-level relevance labels | Heading-level (`doc#slug`) labels | Doc-level is the honest primary unit — `retrieve()` returns **whole files** (D-02), and the citation accept-set is doc + all anchors. Heading-level can be an optional secondary metric but risks penalizing a correct whole-doc retrieval. |
| Computing semantic recall in free CI | Committing a cached query→embedding fixture | Rejected as primary: it duplicates the index build and adds a second staleness trap. Free CI uses **keyword-mode** recall as a soft floor; real semantic numbers come from the paid dispatch. |

**Installation:** none.

## Package Legitimacy Audit

Not applicable — this phase installs **no external packages**. All code composes existing, already-vendored dependencies (`numpy`, `pytest`, `anthropic`). No registry lookups, no slopcheck run needed.

## Architecture Patterns

### Data flow (what this phase adds, in dashed boxes)

```
                         ┌───────────────────── FREE (ci.yml, pytest -q, no keys) ──────────────────────┐
                         │                                                                              │
 evals/retrieval.jsonl ──┼──► recall_at_k()/mrr()  ──► retrieve(index, q, max_results=k)  (keyword mode)│
 (query → relevant ids)  │        │                        ▲ no Voyage call when key unset              │
                         │        └─► soft-floor pytest: assert recall@3 > 0  (D-03, not gated)         │
                         │                                                                              │
 injection golden case ──┼──► run_ticket(FakeClient, TICKET)  ──► guardrail(guard="ticket_binding")     │
 (body says "act on #99")│        └─► assert event fires AND victim ticket has no reply row (EVAL-02)   │
                         │                                                                              │
 cited ⊆ retrieved  ─────┼──► extract_outcome(events).{citations, retrieval.retrieved_ids}  (EVAL-03)   │
                         └──────────────────────────────────────────────────────────────────────────────┘

                         ┌──────────────── PAID (evals.yml, workflow_dispatch, ANTHROPIC + VOYAGE) ─────┐
 evals/retrieval.jsonl ──┼──► recall_at_k()/mrr() over retrieve() in SEMANTIC mode ──► report field only │
                         │                                                    (recall@1, MRR = signal)   │
 D-08 seeding hook ──────┼──► run_ticket(real client, ..., seed_denial=…) ──► one real citation denial   │
                         │        └─► real model reads retry instruction ──► recovers ──► resolution     │
 existing 12-case suite ─┼──► pass-rate --threshold 0.8  (the ONLY hard gate; unchanged)                 │
                         └──────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 1: recall@k / MRR as a pure function over `retrieve()`
**What:** A metric module that, for each labeled query, calls `retrieve(index, query, max_results=k)` and compares the returned doc/id set against the labeled relevant set. No side effects, no persistence.
**When to use:** EVAL-01, both the free soft-floor pytest and the paid report field.
**Definitions (recommended, doc-level primary):**
- `recall@k` (per query, single relevant doc): `1.0` if the relevant doc appears among the top-`k` results' `doc` fields, else `0.0`; the reported metric is the mean over queries.
- `MRR`: mean of `1/rank` where `rank` is the 1-based position of the first result whose `doc` (or any `anchor`) is relevant; `0` if none in top-`k`.
- Queries labeled `relevant: []` (the `salesforce-integration` uncovered case) are **excluded from recall/MRR** and instead assert `retrieve()` returns `[]` (the D-03/D-04 escalation signal).

**Honesty note the planner must carry:** the corpus is **3 docs**. With `max_results=3`, `recall@3` **saturates to ~1.0** for any query that returns all docs, so it is a near-vacuous number — which is *precisely why D-03 makes it a soft floor, not a gate*. The metrics that actually carry signal on this corpus are **`recall@1` and `MRR`**. Report all of `recall@1`, `recall@3`, and `MRR`, but frame `recall@1`/`MRR` as the meaningful ones.

**Example (metric shape; verified `retrieve()` signature):**
```python
# retrieve() -> (results, mode, degraded, cause); results[i] has "doc" and "anchors"
def recall_at_k(index, labels, k, *, key=None):
    hits = 0
    scored = 0
    for row in labels:
        if not row["relevant"]:
            continue  # negative case; checked separately for []-means-escalate
        scored += 1
        results, *_ = retrieve(index, row["query"], key=key, max_results=k)
        got = {r["doc"] for r in results} | {a for r in results for a in r["anchors"]}
        if set(row["relevant"]) & got:
            hits += 1
    return hits / scored if scored else 0.0
```

### Pattern 2: deterministic guardrail assertion with a real would-be write (EVAL-02)
**What:** Seed **real** ticket rows for both the run's ticket and the victim ticket, drive a `send_reply(ticket_id=<victim>)` via a scripted `FakeClient`, and assert BOTH (a) a `guardrail` event with `guard="ticket_binding"` fires AND (b) the victim ticket has **zero** reply rows.
**Why both:** asserting only the event lets the test pass vacuously if the write path changes; asserting the DB row means removing the guard makes an *unguarded write actually land*, so the test fails honestly. This is the existing `tests/test_guardrails.py` pattern (`_seed_tickets`, `_reply_ticket_ids`, `VICTIM_TICKET`) — EVAL-02 reuses it with an injection-flavoured body.

### Anti-Patterns to Avoid
- **Gating CI on retrieval metrics.** Violates D-03 and would make a nondeterministic/tiny-corpus number a merge blocker. Report only.
- **Measuring recall in keyword mode and calling it the semantic claim.** Keyword-mode recall is a free wiring smoke test; the RAG quality claim requires the **semantic** run (paid dispatch). Label them distinctly in the artifact.
- **Baking the D-08 hook into the normal production path.** It must be opt-in and default-off, or it becomes a forgettable footgun that alters live runs (mirrors the codebase's stated aversion in `bind_to_ticket`'s docstring).
- **Reusing ticket bodies as retrieval queries.** The model sends topical phrases, not bodies (`03-06-SUMMARY.md`). Use model-style queries in `retrieval.jsonl`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Ranking for recall@k | A second retriever/scorer in the harness | The existing `retrieve(index, q, max_results=k)` | The metric must measure the *shipped* retriever, not a reimplementation, or the number is meaningless. |
| Recording what a reply cited | New event parsing | `extract_outcome()` (already emits `citations` + `retrieval.retrieved_ids`) | WR-10 already built this; re-parsing risks drifting from the accept-set `agent.py` actually uses. |
| The citation accept-set | Recomputing doc/heading anchors in the harness | `retrieval.anchors()` / the ids `agent.py:318-323` already adds | Narrowing to `id` alone would manufacture violations the running system never saw (documented in `extract_outcome`). |
| Forcing a citation denial | Editing `kb/` or the index to break retrieval | The D-08 opt-in `retrieved_ids` seeding hook | Mutating the corpus would break the 12-case suite and the committed index staleness gate. |

**Key insight:** the safe, honest version of every EVAL-0x check reuses the exact code path production uses; the only *new* code is (1) the pure metric math, (2) one data file, and (3) the opt-in seeding hook.

## Runtime State Inventory

Not a rename/refactor/migration phase — omitted. (New files added: `evals/retrieval.jsonl`, a metric module or functions in `evals.py`, new tests. No stored data, service config, OS state, secrets, or build artifacts carry a value that changes.)

## Common Pitfalls

### Pitfall 1: `recall@3` is vacuous on a 3-doc corpus
**What goes wrong:** Reporting `recall@3 = 1.0` and implying the retriever is perfect.
**Why it happens:** `max_results=3` on a 3-doc KB can return every doc, so the relevant one is always present.
**How to avoid:** Lead with `recall@1` and `MRR`; frame `recall@3 > 0` as the deliberately-weak soft floor D-03 asks for. Document the saturation in the report and the plan.
**Warning signs:** A `recall@3` of exactly `1.0` across all queries.

### Pitfall 2: Semantic recall needs a Voyage query embedding — it is NOT free
**What goes wrong:** Trying to compute the real semantic recall inside `ci.yml` and either getting keyword-mode numbers silently, or making a paid call in "free" CI.
**Why it happens:** `retrieve()` only embeds the query (a Voyage call) when a key is set; with no key it silently falls back to keyword mode (`retrieval.py:280-309`). The committed `kb/index.json` holds only the **document** embeddings.
**How to avoid:** Free CI runs the metric in keyword mode as a wiring soft-floor; the semantic numbers are produced by the paid `evals.yml` dispatch (add `VOYAGE_API_KEY` to that job). Label mode in the artifact so the two are never confused.
**Warning signs:** A "semantic" recall number appearing from a job with no `VOYAGE_API_KEY`.

### Pitfall 3: A citation denial is `is_error=True`, so a naive model ends `ended_without_action`
**What goes wrong:** The recovery-hook case fires a denial and the run dies without a terminal action, regressing the pass-rate gate.
**Why it happens:** `resolved_via` stays `None` on an errored terminal tool call; if the model treats the denial as final, `run_ticket` ends `ended_without_action` (`agent.py:430-431`).
**How to avoid:** The denial is **already phrased as a retry instruction** that names the valid `retrieved_ids` (`agent.py:160-172`), and `test_run_recovers_after_citation_denial` proves a fake recovers. The paid case measures whether a **real** model recovers — the exact open item from `03-06-SUMMARY.md`. If a real model fails to recover, that is a finding, not a harness bug.
**Warning signs:** `ended_without_action` on the seeded case in the paid report.

### Pitfall 4: The D-08 hook leaking into the free suite or production
**What goes wrong:** The seeding parameter defaults on, or a free test depends on it, altering normal runs.
**Why it happens:** Adding a non-default-off parameter, or seeding inside the shared registry.
**How to avoid:** Keyword-only, default-off (`seed_denial=None`/`False`) on `run_ticket`; seed only the **per-run** `retrieved_ids` set (never the registry); the free pytest tests the *mechanism* with a fake, the paid dispatch tests *real-model recovery*.
**Warning signs:** Any existing test changing behaviour after the hook lands; a diff to `app.state.registry`.

### Pitfall 5: `_no_outbound_http` conftest fixture fails any accidental Voyage call in tests
**What goes wrong:** A new retrieval-metric test that forgets to null the key makes a real paid call — and `tests/conftest.py`'s autouse `_no_outbound_http` will raise `AssertionError` instead.
**Why it happens:** `search_docs`/`retrieve` read `settings.voyage_api_key`; a dev with `VOYAGE_API_KEY` in `.env` would otherwise bill real calls.
**How to avoid:** Free metric tests set `monkeypatch.setattr(settings, "voyage_api_key", None)` (the `keyword_baseline` fixture pattern in `test_guardrails.py`) so they run in keyword mode. This is a feature: it guarantees the free suite is genuinely free.
**Warning signs:** `AssertionError: a test attempted a real outbound HTTP call`.

## Code Examples

### Verified: the SEC-04 guard EVAL-02 asserts on (do not modify)
```python
# src/relay/agent.py:129-145 — the branch the EVAL-02 mutation test must break
if (
    bound_ticket_id is not UNBOUND
    and supplied_ticket_id is not None
    and supplied_ticket_id != bound_ticket_id
):
    return json.dumps({
        "error": (... "Retry with ticket_id={bound_ticket_id}." ),
        "denied_by": "ticket_binding",
        "expected_ticket_id": bound_ticket_id,
        "supplied_ticket_id": supplied_ticket_id,
    }), True
```
**Named mutation for the EVAL-02 falsifiability check:** delete this `if` block (or change `supplied_ticket_id != bound_ticket_id` to `== `). Either makes the injected `send_reply(ticket_id=99)` land a row on the victim ticket and emits no `guardrail` event → EVAL-02 fails. Seed real rows (`_seed_tickets(conn, TICKET, VICTIM_TICKET)`) so the unguarded write actually persists.

### Verified: `extract_outcome` already carries EVAL-03's data (no run_case change)
```python
# src/relay/evals.py:99-140 (abridged) — citations + retrieved_ids already emitted
outcome["citations"] = tool_input.get("citations")          # None vs [] preserved
...
retrieved.update(x for x in (hit.get("doc"), hit.get("id")) if x)
retrieved.update(a for a in hit.get("anchors") or () if a)   # full accept-set
outcome["retrieval"]["retrieved_ids"] = sorted(retrieved)
```
EVAL-03's deterministic half is therefore a property assertion over the report:
`set(r["citations"] or []) <= set(r["retrieval"]["retrieved_ids"])` for every case — already exercised at unit level by `tests/test_evals.py::test_extract_outcome_records_what_the_reply_cited` and friends.

### D-08 seeding hook — recommended shape (opt-in, per-run only)
```python
# src/relay/agent.py — run_ticket gains a keyword-only, default-off probe.
# After the first successful search_docs grows `retrieved_ids` (agent.py:308-323),
# if the probe is armed: drop the top real id and inject a dummy, so the model's
# natural citation of the id it was shown is denied exactly once.
async def run_ticket(..., *, seed_citation_denial: bool = False):
    ...
    # inside the search_docs grow-step, guarded by `if seed_citation_denial and not _armed:`
    #   dropped = results[0]["id"]              # a real id the model will likely cite
    #   retrieved_ids.discard(dropped)
    #   retrieved_ids.add("__seeded_missing__") # dummy so the set is non-empty
    #   _armed = True
```
- **Stays out of the free suite:** parameter defaults `False`; no existing call passes it.
- **Stays out of production:** `main.py` never sets it; only the paid harness case does.
- **Recoverable by design:** the denial names the (reduced) `retrieved_ids`, and a real model retries with a still-valid id — the exact recovery Phase 3 could not observe.
- **Free mechanism test:** a `FakeClient` that cites the dropped id then recovers proves the hook fires and the guard denies+recovers, with no API cost. The **real-model** recovery claim is only provable in the paid dispatch.

### Labeled retrieval set — `evals/retrieval.jsonl` (ids verified against `kb/index.json`)
```jsonl
{"id": "rate-limits-pro", "query": "API rate limits Pro plan", "relevant": ["api.md", "api.md#rate-limits"]}
{"id": "refund-monthly", "query": "refund policy billing charge", "relevant": ["billing.md", "billing.md#refunds"]}
{"id": "password-reset", "query": "password reset", "relevant": ["account.md", "account.md#password-reset"]}
{"id": "2fa-lockout", "query": "two-factor authentication lost recovery codes lockout", "relevant": ["account.md", "account.md#two-factor-authentication"]}
{"id": "webhooks-on-pro", "query": "webhooks availability plan", "relevant": ["api.md", "api.md#webhooks"]}
{"id": "pro-pricing", "query": "Pro plan pricing", "relevant": ["billing.md", "billing.md#billing-and-plans"]}
{"id": "downgrade-data-loss", "query": "downgrade plan data retention projects", "relevant": ["billing.md", "billing.md#upgrades-and-downgrades"]}
{"id": "sso-config", "query": "SAML SSO configuration", "relevant": ["account.md", "account.md#sso-enterprise"]}
{"id": "data-export", "query": "export data", "relevant": ["account.md", "account.md#data-export-and-deletion"]}
{"id": "key-suspended", "query": "API key suspended", "relevant": ["api.md", "api.md#authentication"]}
{"id": "enterprise-sla", "query": "uptime SLA guarantee", "relevant": ["billing.md"]}
{"id": "salesforce-integration", "query": "Salesforce integration", "relevant": []}
```
Every non-empty `relevant` id above is a real doc name or a real `doc#slug` from `kb/index.json` (verified: `account.md` headings → `accounts-and-access`, `password-reset`, `two-factor-authentication`, `data-export-and-deletion`, `sso-enterprise`; `api.md` → `api-access`, `authentication`, `rate-limits`, `webhooks`; `billing.md` → `billing-and-plans`, `refunds`, `upgrades-and-downgrades`). The `salesforce-integration` row with `relevant: []` is the negative case asserting the empty-result escalation signal (`03-06-SUMMARY.md` "Residual risk"). The query phrasings mirror the model-style queries measured in the Phase 3 floor calibration.

## State of the Art

| Old (Phase 3 close) | Current (this phase) | Impact |
|---------------------|----------------------|--------|
| Report records action/category/grounded/quality/cost/error only | `extract_outcome` also records `citations` + `retrieval` (WR-10 fix, already landed) | EVAL-03 structural half is a report property, not new plumbing. |
| "Citation guard never fired" was unfalsifiable | 8/8 `send_reply` cases cited valid ids in the paid re-run (`03-06-SUMMARY.md`) | The guard is proven **quiet-because-correct**, not decorative. |
| Real-model recovery from a denial never observed | D-08 seeding hook forces one real denial in the paid dispatch | Closes the single open item Phase 3 left. |
| No retrieval metric at all | recall@1/recall@3/MRR reported (report-only) | EVAL-01; also feeds the deferred README keyword-vs-semantic comparison. |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest `>=8.0` + pytest-asyncio `>=0.23`, `asyncio_mode = "auto"` (`pyproject.toml`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `.venv/bin/python -m pytest tests/test_evals.py -q` |
| Full suite command | `.venv/bin/python -m pytest -q` (195 passing as of Phase 3 close) |
| Free-CI command | `pytest -q` (ci.yml, no API keys) — the new deterministic tests ride this unchanged |
| Paid command | `python -m relay.evals --concurrency 4 --threshold 0.8` (evals.yml, workflow_dispatch) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EVAL-01 | recall@1/@3 + MRR computed over `retrieve()` from labeled set | unit (keyword mode) | `pytest tests/test_evals.py -k recall -x` | ❌ Wave 0 (new tests) |
| EVAL-01 | soft floor `recall@3 > 0` (D-03, not gated) | unit (keyword mode) | `pytest tests/test_evals.py -k soft_floor -x` | ❌ Wave 0 |
| EVAL-01 | labeled set is well-formed; ids exist in `kb/index.json` | unit | `pytest tests/test_evals.py -k retrieval_labels -x` | ❌ Wave 0 |
| EVAL-01 | 12-case suite does not regress below `--threshold 0.8` | integration (paid) | `python -m relay.evals --threshold 0.8` (evals.yml) | ✅ (existing gate) |
| EVAL-02 | injection → `guard="ticket_binding"` event fires AND victim ticket unwritten | integration (free, FakeClient) | `pytest tests/test_evals.py -k injection -x` | ❌ Wave 0 (pattern exists in test_guardrails.py) |
| EVAL-03 | `cited ⊆ retrieved` holds for every case in a produced report | unit (free, keyword) | `pytest tests/test_evals.py -k citation_faithful -x` | ⚠️ partial (unit-level subset asserts exist) |
| EVAL-03 | seeding hook drops a real id + guard denies + fake recovers | unit (free) | `pytest tests/test_evals.py -k seed_denial -x` | ❌ Wave 0 |
| EVAL-03 | **real** model recovers from a seeded denial | integration (paid) | `python -m relay.evals` with hook armed (evals.yml) | ❌ Wave 0 (paid; report-only) |

### Sampling Rate
- **Per task commit:** `.venv/bin/python -m pytest tests/test_evals.py -q`
- **Per wave merge:** `.venv/bin/python -m pytest -q` (full free suite; must stay green)
- **Phase gate:** full free suite green; paid `evals.yml` dispatch run once to (a) confirm 12-case ≥ 0.8 and (b) capture semantic recall@k/MRR + one real-model recovery into the artifact.

### Wave 0 Gaps
- [ ] `evals/retrieval.jsonl` — the labeled query→id set (EVAL-01). New data file.
- [ ] Metric functions `recall_at_k` / `mrr` (in `src/relay/evals.py` or a small `src/relay/retrieval_eval.py`) — EVAL-01.
- [ ] `tests/test_evals.py` additions: recall/MRR unit tests, soft-floor test, label-well-formedness test, EVAL-02 injection test, EVAL-03 subset test, D-08 seeding-hook mechanism test.
- [ ] `src/relay/agent.py`: opt-in `seed_citation_denial` keyword-only parameter on `run_ticket` (D-08).
- [ ] `src/relay/evals.py`: surface semantic recall@k/MRR as a **report-only** field in `run_evals()` output; thread the arming flag into `run_case` for the paid recovery case.
- [ ] `.github/workflows/evals.yml`: add `VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}` to the paid job so semantic recall can be computed. **`ci.yml` needs no change** — the deterministic tests ride the existing `pytest -q` step.
- [ ] Framework install: none — `pytest`/`pytest-asyncio`/`numpy` already present.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python + `.venv` | all | ✓ | 3.14 local / 3.12 CI | — |
| `numpy` | recall/MRR + `retrieve()` | ✓ (`.venv`) | 2.5.2 | — |
| `pytest`/`pytest-asyncio` | free deterministic tests | ✓ | `>=8.0`/`>=0.23` | — |
| `kb/index.json` | semantic `retrieve()` + label validation | ✓ | voyage-4-lite, 512-dim, 3 docs, sha stamped | keyword mode (labels still validate against doc/heading ids) |
| `ANTHROPIC_API_KEY` | paid 12-case suite + real-model recovery | secret (CI dispatch only) | — | none (paid path only; free suite never needs it) |
| `VOYAGE_API_KEY` | **semantic** recall@k/MRR numbers | ✗ (not yet in evals.yml) | — | keyword-mode recall (free CI soft floor) |

**Missing dependencies with no fallback:** none for the free path.
**Missing dependencies with fallback:** `VOYAGE_API_KEY` — absent, semantic recall degrades to keyword-mode recall (still reportable, but not the RAG-quality claim). Add the secret to `evals.yml` to produce the real numbers.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The D-01/D-03/D-08 decisions supplied inline match what `discuss-phase` will lock in CONTEXT.md | User Constraints | Plan built against unratified decisions; low risk (orchestrator supplied them explicitly). |
| A2 | Doc-level relevance labels are the intended primary metric unit (vs heading-level) | Standard Stack / EVAL-01 | If heading-level is wanted, `relevant` lists and MRR ranking change; labels already include both, so cheap to switch. |
| A3 | `evals/retrieval.jsonl` (separate file) is preferred over extending `golden.jsonl` | Alternatives Considered | If reviewer prefers one dataset, merge; mechanically simple. |
| A4 | The D-08 hook belongs as a `run_ticket` keyword-only param (vs a harness-side wrapper) | Code Examples | If the codebase's "no forgettable per-call arg" rule is read strictly, may prefer a distinct eval-only entry point; both achieve opt-in isolation. |
| A5 | The paid recovery case is acceptable to run once at phase gate (small cost, ~pennies) | Validation Architecture | If zero paid runs are allowed this phase, the real-model recovery claim stays "proven by fake only" — reason from code and defer the live proof. |
| A6 | `VOYAGE_API_KEY` exists as a GitHub secret (only `ANTHROPIC_API_KEY` is referenced today) | Environment / evals.yml | If absent, semantic recall cannot be produced in CI; keyword-mode fallback still reports. |

## Open Questions

1. **Is a real `VOYAGE_API_KEY` available to the `evals.yml` dispatch?**
   - What we know: only `ANTHROPIC_API_KEY` is wired in `evals.yml`; `retrieve()` needs a Voyage key to embed queries for semantic recall.
   - What's unclear: whether the secret exists in the repo settings.
   - Recommendation: add `VOYAGE_API_KEY` to `evals.yml`; if unavailable, report keyword-mode recall in CI and generate semantic numbers locally from the `.venv` with the key in `.env`.

2. **How is the paid eval artifact stored/surfaced?**
   - What we know: `evals.py` writes to `eval_results/` (gitignored) and `evals.yml` uploads it as a build artifact; the dashboard eval panel is explicitly deferred (REQUIREMENTS v2).
   - What's unclear: whether recall@k/MRR should be committed anywhere durable this phase.
   - Recommendation: keep it in the uploaded artifact + report JSON only; durable storage is a Phase 5/6 decision (deferred).

3. **Does a real model actually recover from the seeded citation denial?**
   - What we know: the denial is recoverable by construction (retry instruction naming valid ids) and a fake recovers; no real denial has ever fired (`03-06-SUMMARY.md` "Still untested").
   - What's unclear: real-model behaviour — the whole reason D-08 exists.
   - Recommendation: run the seeded case once in the paid dispatch; report the outcome honestly whichever way it goes. A paid run would show either a `guardrail(guard="citation")` followed by a `resolution(via="send_reply")` (recovery), or an `ended_without_action` (a real finding).

## Sources

### Primary (HIGH confidence — read/verified in this repo)
- `src/relay/agent.py` — `_execute_guarded` (SEC-04 binding guard `:129-145`; citation guard `:150-172`; recoverable-by-retry-instruction), `run_ticket` (`retrieved_ids` creation `:207`, grow-step `:308-323`, `ended_without_action` `:430-431`), `bind_to_ticket`.
- `src/relay/evals.py` — `extract_outcome` (citations + retrieval `:90-140`), `run_case`, `run_evals` report shape, CLI flags + `--threshold 0.8` gate.
- `src/relay/retrieval.py` — `retrieve()` signature/`max_results` (`:246-327`), keyword fallback with no Voyage call when keyless (`:280-309`), `anchors()`/`_result()` id shape.
- `tests/test_evals.py` — existing WR-10 unit coverage of `citations`/`retrieved_ids`/subset; `run_case` artifact test.
- `tests/test_guardrails.py` — SEC-04 binding tests (`_seed_tickets`, `VICTIM_TICKET`, `_reply_ticket_ids`), citation-denial + recovery tests, `keyword_baseline` fixture.
- `tests/conftest.py` — `conn`/`registry`/`db` fixtures, autouse `_no_outbound_http` guard.
- `tests/helpers.py` — `FakeClient`, `TicketAwareFakeClient`.
- `evals/golden.jsonl` — 12 ticket cases (query topics reused for labels).
- `kb/index.json` — verified doc/heading ids (voyage-4-lite, 512-dim, 3 docs).
- `.github/workflows/ci.yml` (free `pytest -q`) and `evals.yml` (paid `workflow_dispatch`, `ANTHROPIC_API_KEY` only).
- `.planning/phases/03-semantic-retrieval/03-REVIEW.md` §WR-10 (seeding-hook sketch, unfalsifiability).
- `.planning/phases/03-semantic-retrieval/03-06-SUMMARY.md` (floor calibration table, 8/8 cited, "Still untested" recovery gap).
- `.planning/phases/01-security-perimeter/01-CONTEXT.md` (D-09/D-10/D-11 SEC-04 denial contract; EVAL-02 explicitly deferred to Phase 4).
- `.planning/REQUIREMENTS.md` (EVAL-01/02/03 wording + deferred/out-of-scope items).
- `pyproject.toml` (pytest/ruff/deps).

### Secondary / Tertiary
- None required — no external ecosystem lookups were needed for this composition phase.

## Metadata

**Confidence breakdown:**
- Standard stack (no new deps): HIGH — verified against `pyproject.toml` and installed `.venv`.
- Architecture / requirement→code mapping: HIGH — every guard, event, and field read directly from source.
- Metric definitions / label granularity: MEDIUM — definitions are standard, but doc-level vs heading-level and file placement are discretionary (A2/A3).
- D-08 hook shape: MEDIUM — mechanism is verified recoverable; exact signature is a design choice (A4).
- Paid-run behaviour (real-model recovery, semantic recall numbers): reasoned from code, **not executed** (no paid eval run per instructions).

**Research date:** 2026-08-10
**Valid until:** 2026-09-09 (stable; internal-code-driven, low churn — refresh if `agent.py`/`evals.py`/`retrieval.py` or the KB/index change).

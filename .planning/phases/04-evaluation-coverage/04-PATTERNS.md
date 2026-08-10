# Phase 4: Evaluation Coverage - Pattern Map

**Mapped:** 2026-08-10
**Files analyzed:** 6 (2 new, 3 modified, 1 new-or-extend-existing at Claude's discretion)
**Analogs found:** 6 / 6 (every file composes an existing, verified code path)

## Orientation

This phase is composition, not construction. The instrumentation all three requirements
assert on already exists and was read line-by-line:
- **EVAL-02**'s SEC-04 binding guard: `agent.py:129-145`, already fully covered by deterministic tests in `tests/test_guardrails.py`.
- **EVAL-03**'s data: `evals.py::extract_outcome` (`evals.py:90-140`) already emits `citations` + `retrieval.retrieved_ids`; `CaseResult` already carries them (`evals.py:86-87`); the subset check is already unit-exercised in `tests/test_evals.py`.
- **EVAL-01**'s input: `retrieve(index, q, *, key, max_results)` (`retrieval.py:246-327`) already returns the doc/id/anchors the metric compares against, and degrades to keyword mode with **no** Voyage call when keyless (`retrieval.py:280-309`) — which is what makes a free CI soft-floor possible.

The only genuinely new machinery is the **D-08 seeding hook** in `agent.py`. Everything else copies an existing analog verbatim.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `evals/retrieval.jsonl` (new) | eval-data | batch / labeled input | `evals/golden.jsonl` | exact (both JSONL label sets) |
| `src/relay/retrieval_eval.py` **or** new fns in `src/relay/evals.py` (new) | utility / metric | transform (pure fn over `retrieve()`) | `evals.py::extract_outcome` (pure fn over a stream) + `retrieval.py::retrieve` (data source) | role-match |
| `tests/test_evals.py` (modified — additions) | test | request-response / batch | `tests/test_guardrails.py` (injection + recovery) and `tests/test_evals.py` itself (labels, subset, artifact) | exact |
| `src/relay/evals.py` (modified — report field + arming flag) | harness / service | batch | `run_evals`/`run_case`/`print_summary` in the same file | exact (self-analog) |
| `src/relay/agent.py` (modified — `seed_citation_denial`) | agent loop | event-driven | `bind_to_ticket` + `retrieved_ids` grow-step + citation guard in the same file | exact (self-analog) |
| `.github/workflows/evals.yml` (modified — add `VOYAGE_API_KEY`) | config / CI | — | the existing `env:` block in `evals.yml` | exact (self-analog) |

**Frozen — do NOT modify:** `.github/workflows/ci.yml` (the new deterministic tests ride its existing `pytest -q` step, line 22, unchanged) and `src/relay/mcp_server.py` (the citation/binding guards deliberately no-op on its unbound path).

---

## Pattern Assignments

### `evals/retrieval.jsonl` (eval-data, batch)

**Analog:** `evals/golden.jsonl` — the 12-row ticket dataset, one JSON object per line.

**Shape pattern** (`evals/golden.jsonl:1`, ticket-shaped — the retrieval set parallels its
per-line structure but swaps the payload for a query→relevant-ids label):
```jsonl
{"id": "rate-limits-pro", "customer_email": "...", "subject": "...", "body": "...", "expected_action": "send_reply", "expected_categories": ["technical"], "notes": "..."}
```

**Loading pattern to reuse** (`evals.py:65-66` — no bespoke parser; the metric loader copies this exactly):
```python
def load_golden(path: Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

**Label content (verified against `kb/index.json`).** The index defines exactly three docs
and these headings (confirmed by reading `kb/index.json`):
- `account.md` → Accounts and Access, Password reset, Two-factor authentication, Data export and deletion, SSO (Enterprise)
- `api.md` → API Access, Authentication, Rate limits, Webhooks
- `billing.md` → Billing and Plans, Refunds, Upgrades and downgrades

`relevant` values are a bare doc name and/or a `doc#slug` anchor (slug = lowercased,
hyphenated heading). The RESEARCH.md example block (`04-RESEARCH.md:248-262`) is the ready
label set; every non-empty id there matches the ids above. The `salesforce-integration`
row has `relevant: []` — the negative/escalation case, excluded from recall/MRR and asserted
to return `[]`. Queries are **model-style topical phrases**, NOT ticket bodies (per
`03-06-SUMMARY.md`; do not reuse `golden.jsonl` bodies as queries).

---

### Metric module — `recall_at_k` / `mrr` (utility, transform)

**Placement (Claude's discretion, D per CONTEXT):** either a new `src/relay/retrieval_eval.py`
or functions added to `src/relay/evals.py`. Prefer wherever the mutation check reads most
honestly; the naming convention (snake_case, verb-first, one concern per module — see
CLAUDE.md) favors a small dedicated `retrieval_eval.py` if it grows beyond two functions.

**Analog 1 — pure-function-over-a-stream shape** (`evals.py:90-140`, `extract_outcome`): no
side effects, no persistence, builds a set and compares. The metric fns mirror this purity.

**Analog 2 — the data source, called unchanged** (`retrieval.py:246-327`). The metric must
call the *shipped* retriever, never reimplement ranking. Verified return contract:
```python
def retrieve(index, query, *, key=None, floor=..., max_results=3) -> tuple[list[dict], str, bool, str | None]:
    # returns (results, mode, degraded, cause); each result[i] has "doc", "id", "anchors", ...
```

**Accept-set construction to copy** (from `extract_outcome`, `evals.py:131-132`, and the
identical logic in `agent.py:318-323`) — doc name + located id + every anchor:
```python
retrieved.update(x for x in (hit.get("doc"), hit.get("id")) if x)
retrieved.update(a for a in hit.get("anchors") or () if a)
```
Use this same union for the recall comparison so a metric hit means what the guard's accept-set
means. `anchors()` (`retrieval.py:438-450`) is the authority on what a doc licenses.

**Metric definitions (from RESEARCH, D-09):** report `recall@1`, `recall@3`, and `MRR`; frame
`recall@1`/`MRR` as the signal-bearing ones (`recall@3` saturates to ~1.0 on a 3-doc corpus).
Rows with `relevant: []` are excluded from recall/MRR and instead assert `retrieve()` returns `[]`.

**Cost boundary (D-10, Pitfall 2/5):** free CI computes recall in **keyword mode** — the metric
tests MUST pin `settings.voyage_api_key = None` (see keyword_baseline pattern below) or the
autouse `_no_outbound_http` guard (`conftest.py:63-79`) will raise. Semantic recall is the paid
`evals.yml` path only.

---

### `tests/test_evals.py` additions (test, request-response / batch)

New deterministic tests ride the existing free `pytest -q` step (`ci.yml:22`) — **no `ci.yml`
edit**. All EVAL-02/EVAL-03 mechanism tests are free (fake client, keyword mode).

**Test double imports** (`tests/test_evals.py:3` — this file imports from `helpers`, not the
inline copies in `test_guardrails.py`):
```python
from helpers import FakeClient, response, text_block, tool_use_block
```

**#### EVAL-02 injection test — analog: `tests/test_guardrails.py:244-261`**

Copy `test_binding_denial_emits_guardrail_event` verbatim in structure; change the body to an
injection payload ("act on ticket #99"). Assert BOTH the event AND the un-write.

Seed helpers to reuse (`test_guardrails.py:214-226`):
```python
def _seed_tickets(conn, *tickets):
    """Insert real ticket rows so an unguarded cross-ticket write would actually land."""
    for ticket in tickets:
        conn.execute(
            "INSERT INTO tickets (id, customer_email, subject, body) VALUES (?, ?, ?, ?)",
            (ticket["id"], ticket["customer_email"], ticket["subject"], ticket["body"]),
        )
    conn.commit()

def _reply_ticket_ids(conn):
    rows = conn.execute("SELECT ticket_id FROM replies ORDER BY id").fetchall()
    return [row["ticket_id"] for row in rows]
```

Assertion template (`test_guardrails.py:229-241` + `:250-261`):
```python
async def test_..._injection(conn, registry):
    _seed_tickets(conn, TICKET, VICTIM_TICKET)          # VICTIM_TICKET at test_guardrails.py:196-201
    client = FakeClient([
        _response([_tool_use("send_reply", {"ticket_id": 99, "body": INJECTED_REPLY})]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    guardrails = [e for e in events if e.type == "guardrail"]
    assert guardrails[0].data["guard"] == "ticket_binding"       # (a) event fires
    assert conn.execute(                                          # (b) victim un-written
        "SELECT COUNT(*) FROM replies WHERE ticket_id = ?", (VICTIM_TICKET["id"],)
    ).fetchone()[0] == 0
```
**Named mutation (falsifiability, RESEARCH:214):** delete the `if` at `agent.py:129-145` or flip
`!=` to `==`; the injected write then lands on the victim row and no guardrail event fires → test
fails. Seed real rows so the unguarded write actually persists.

**#### EVAL-03 subset check — analog: `tests/test_evals.py:76-92`, `:181-211`**

The `cited ⊆ retrieved` property is already unit-asserted (`test_evals.py:92`,
`test_extract_outcome_records_what_the_reply_cited`) and end-to-end through `run_case`
(`test_evals.py:181-211`, `test_the_report_artifact_carries_citations_and_retrieval`). EVAL-03's
deterministic half generalizes this to "holds for every case in a produced report":
```python
for r in report["results"]:
    assert set(r["citations"] or []) <= set(r["retrieval"]["retrieved_ids"])
```
Reuse the `_hit`/`_search_result` builders (`test_evals.py:65-73`) for synthetic events.

**#### EVAL-01 label well-formedness — analog: `tests/test_evals.py:13-22`**

Copy `test_golden_dataset_is_well_formed`; validate each `relevant` id exists in `kb/index.json`
(doc name or `doc#slug`), ids are unique, queries non-empty. Same load-and-loop shape.

**#### EVAL-01 soft floor + recall/MRR unit — keyword mode.**

Pin keyword mode with the `keyword_baseline` fixture pattern (`test_guardrails.py:362-367`):
```python
@pytest.fixture()
def keyword_baseline(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
```
Assert only the soft floor `recall@3 > 0` (D-03, not a gate) plus that the metric fns return
sane values on the labeled set.

**#### EVAL-03 D-08 seeding-hook mechanism test — analog: `tests/test_guardrails.py:441-490`**

The one new mechanism. Copy `RecoveringFakeClient` (`test_guardrails.py:441-473`) and
`test_run_recovers_after_citation_denial` (`:476-490`). That client reads `retrieved_ids` out
of the denial payload rather than being scripted with the answer — the load-bearing property:
if the denial stops naming valid ids, recovery is impossible and the test fails honestly. The
new test arms `seed_citation_denial=True` on `run_ticket`, drives a fake that cites the dropped
id, and asserts: one `guardrail(guard="citation")` fired AND `events[-1]` is `resolution
via=send_reply`. Uses `_events_of` (`test_guardrails.py:381-385`) and `_reply_ticket_ids`.

---

### `src/relay/evals.py` (modified — report field + arming flag)

**Analog: itself.** Two changes, both self-referential.

**1. Report-only recall@k/MRR field** — attach to the dict `run_evals` returns
(`evals.py:255-264`). That return is the report; add keys beside `pass_rate`/`mean_quality`:
```python
return {
    "ran_at": ..., "model": ..., "cases": ..., "passed": ..., "pass_rate": ...,
    "mean_quality": ..., "total_cost_usd": ...,
    "retrieval_metrics": {...},   # NEW: recall@1/@3/MRR + mode (keyword|semantic), report-only
    "results": [asdict(r) for r in results],
}
```
`print_summary` (`evals.py:267-284`) is where a human-readable line for these numbers attaches
(mirror its existing `print(f"pass rate ...")` footer). Label the `mode` so keyword-vs-semantic
is never confused (Pitfall 2). Do NOT gate on these (D-03) — the only hard gate stays
`pass_rate < args.threshold` at `evals.py:302-304`, untouched.

**2. Thread the arming flag into the paid recovery case.** `run_case` (`evals.py:182-234`) calls
`run_ticket(client, registry, ticket)` at `evals.py:198`. For the single paid recovery case,
pass the new keyword-only `seed_citation_denial=True` through to that call. Keep it opt-in and
off for all 12 golden cases (default `False`). `CaseResult` (`evals.py:69-87`) already carries
`citations`/`retrieval`; no dataclass change needed for the subset check.

---

### `src/relay/agent.py` (modified — `seed_citation_denial` hook)

**Analog: itself** — the `retrieved_ids` machinery already present. This is the only new code.

**Signature** — add a keyword-only, default-off param to `run_ticket` (`agent.py:179-185`).
Per CLAUDE.md's function-design rule (keyword-only for optional collaborators) and the
codebase's explicit aversion to forgettable per-call controls (`bind_to_ticket` docstring,
`agent.py:57-73`), it MUST default off:
```python
async def run_ticket(client, registry, ticket, policy=None, budget=None, *, seed_citation_denial: bool = False):
```

**Seed point** — the per-run `retrieved_ids` set is created at `agent.py:207` and grown in the
`search_docs`-success branch at `agent.py:308-323`:
```python
# agent.py:207
retrieved_ids: set[str] = set()
# agent.py:308-323 (the grow-step the hook amends, once, when armed)
if block.name == "search_docs" and not is_error:
    for hit in payload.get("results", []):
        if hit.get("doc"): retrieved_ids.add(hit["doc"])
        if hit.get("id"):  retrieved_ids.add(hit["id"])
        retrieved_ids.update(a for a in hit.get("anchors") or () if a)
```
When armed (and only once — track a local `_armed` flag), after this grow-step: drop one real
id the model will likely cite and inject a dummy so the set stays non-empty:
```python
# guarded by `if seed_citation_denial and not _armed:` inside the grow-step
dropped = results[0]["id"]
retrieved_ids.discard(dropped)
retrieved_ids.add("__seeded_missing__")
_armed = True
```
Mutate ONLY this per-run set (never `app.state.registry`, Pitfall 4). The citation guard at
`agent.py:150-172` then denies the model's natural cite exactly once, with a retry instruction
naming the (reduced) valid ids — the denial is recoverable by construction (`agent.py:159-172`),
which is what lets a real model recover.

**Guard branch this exercises (do not modify), `agent.py:150-172`:**
```python
if name == "send_reply" and retrieved_ids is not None:
    allowed = {i.strip().lower() for i in retrieved_ids}
    missing = [c for c in (validated.get("citations") or []) if c.strip().lower() not in allowed]
    if missing:
        return json.dumps({"error": "...Retry send_reply citing only those ids...",
                           "denied_by": "citation", "missing_citations": missing,
                           "retrieved_ids": sorted(retrieved_ids)}), True
```

**Isolation checks:** `main.py` never sets the flag (production unaffected); no existing test
passes it (free suite unaffected); default `False` means the 12 golden cases never arm it.

---

### `.github/workflows/evals.yml` (modified — add `VOYAGE_API_KEY`)

**Analog: itself** — the existing `env:` block (`evals.yml:24-26`):
```yaml
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
run: python -m relay.evals --concurrency 4 --threshold "${{ inputs.threshold }}"
```
Add one line so semantic recall can be computed on the paid dispatch (D-11):
```yaml
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}    # NEW — semantic query embedding
```
Open question carried from RESEARCH: if `secrets.VOYAGE_API_KEY` does not exist in the repo,
the paid run falls back to keyword-mode recall (still reported); note that in the plan.
**`ci.yml` gets no change.**

---

## Shared Patterns

### Keyword-mode pinning (all free retrieval/metric tests)
**Source:** `tests/test_guardrails.py:362-367` (`keyword_baseline` fixture) + `tests/conftest.py:63-79` (`_no_outbound_http` autouse guard).
**Apply to:** every free EVAL-01 metric test and any test that touches `retrieve()`/`search_docs`.
```python
monkeypatch.setattr(settings, "voyage_api_key", None)   # no Voyage call → genuinely free
```
Omitting this makes `_no_outbound_http` raise `AssertionError` (Pitfall 5) — the feature that
guarantees the free suite bills nothing.

### Assert-the-side-effect, not just the event (EVAL-02, mutation honesty)
**Source:** `tests/test_guardrails.py:229-241`, `:264-274`.
**Apply to:** the EVAL-02 injection case.
Asserting only the `guardrail` event passes vacuously if the write path changes. Also assert the
victim ticket has zero reply rows (`_reply_ticket_ids` / `SELECT COUNT(*) FROM replies`), so
removing the guard makes an unguarded write actually land and the test fails.

### Recovery driven off the denial payload, not a script (EVAL-03/D-08)
**Source:** `tests/test_guardrails.py:441-473` (`RecoveringFakeClient`).
**Apply to:** the D-08 seeding-hook mechanism test.
The recovery client reads `last.get("retrieved_ids")` from the denial and retries with it. If
the denial stops naming valid ids, recovery is impossible → test fails. This is the exact
falsifiability property Phase 3 could not close for a real model.

### Deterministic fixtures (all free tests)
**Source:** `tests/conftest.py:24-45` — `conn` (`:memory:`), `registry` (`build_registry(conn, KB_DIR)`).
**Apply to:** every deterministic test here. Do not open real DB files or the live registry.

---

## No Analog Found

None. Every file this phase touches has a concrete existing analog — most are self-analogs
(extend a function beside its siblings). The single piece of new logic (the D-08 `agent.py`
seed point) is new *code* but reuses the fully-present `retrieved_ids` set and citation guard,
so even it has no "invent from RESEARCH" gap.

## Metadata

**Analog search scope:** `src/relay/` (agent, evals, retrieval), `tests/` (test_evals, test_guardrails, helpers, conftest), `evals/`, `kb/index.json`, `.github/workflows/`.
**Files scanned:** 11 read in full or targeted; `kb/index.json` inspected programmatically for id verification.
**Pattern extraction date:** 2026-08-10
</content>
</invoke>

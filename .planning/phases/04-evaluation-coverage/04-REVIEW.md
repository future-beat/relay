---
phase: 04-evaluation-coverage
reviewed: 2026-08-11T09:40:00Z
depth: standard
files_reviewed: 6
files_reviewed_list:
  - evals/retrieval.jsonl
  - src/relay/retrieval_eval.py
  - src/relay/evals.py
  - src/relay/agent.py
  - tests/test_evals.py
  - .github/workflows/evals.yml
findings:
  critical: 3
  warning: 9
  info: 6
  total: 18
status: issues_found
---

# Phase 4: Code Review Report

**Reviewed:** 2026-08-11T09:40:00Z
**Depth:** standard
**Files Reviewed:** 6
**Status:** issues_found

## Summary

Reviewed against the phase's own stated risk class: *a check that passes while the
thing it measures is broken*. Five of the six new mechanisms hold up under mutation —
EVAL-02's DB assertion is load-bearing against an over-blocking guard, EVAL-03's
anchors-union half is non-vacuous, the seed hook cannot leak between runs, the paid
probe cannot reach the `--threshold` gate, and no new test can bill Voyage or
Anthropic. Those are recorded below so a re-review does not re-litigate them.

The failures are concentrated in the two places nobody wrote a test for: the
**report** and the **paid dispatch**.

- `retrieval_metrics()` labels its numbers `"semantic"` whenever a key is *configured*,
  not when semantic ranking actually ran. Reproduced: with `VOYAGE_API_KEY` set and
  `kb/index.json` missing, the report reads `{"mode": "semantic", "recall@1": 0.9091}`
  — the keyword number, wearing the semantic label. The module docstring and
  `evals.yml`'s new comment both claim this is impossible.
- The D-08 probe silently disarms if the model calls `search_docs` twice. Reproduced:
  armed hook, `guardrail.citation_denial_seeded` logged, **zero** denials, run resolves
  `via=send_reply` — byte-identical in the artifact to a successful recovery.
- Nothing in `CaseResult` records that a denial happened, and `run_case`'s arming
  parameter — the only arming path, by design — has zero test coverage.

Net: EVAL-02 and EVAL-03 landed honestly. EVAL-01's number is coarser and more
fragile than reported, and D-08's paid probe is not yet falsifiable as an artifact.

---

## Critical Issues

### CR-01: `retrieval_metrics()["mode"]` reports intent, not the mode that ran — keyword numbers get labeled "semantic"

**File:** `src/relay/evals.py:82`, `src/relay/retrieval_eval.py:60`

**Issue:** The mode label is computed as `"semantic" if key else "keyword"` — it reads
the *credential*, not the retrieval. But `retrieve()` never raises; it degrades. When
a key is present and the index is missing/stale/mismatched, or when Voyage fails, it
returns `mode="keyword", degraded=True` and keyword results. `first_relevant_rank()`
throws that away:

```python
results, _mode, _degraded, _cause = retrieve(index, row["query"], key=key, max_results=k)
```

So every one of the three ways a keyed deployment can silently fall back produces
keyword recall stamped `"semantic"`.

Reproduced (key set, `kb/index.json` absent):

```
33× retrieval.index_unavailable_at_query
{"mode": "semantic", "labeled_queries": 12, "scored_queries": 11,
 "recall@1": 0.9091, "recall@3": 0.9091, "mrr": 0.9091}
```

0.9091 is the keyword baseline. The docstring at `evals.py:74-76` says the label
exists so "an unlabeled figure would be read as semantic quality (D-10)"; 04-01-SUMMARY
says "the `mode` field in the report says which one you got, so a keyword number can
never be misread as a semantic one"; `evals.yml:26-27` repeats the claim. All three are
false in exactly the failure mode that reaches CI — a stale committed index with the
secret configured, which is the deploy trap `retrieval.py` itself documents at
`INDEX_STALE`. This is a check that passes while the thing it measures is broken.

**Fix:** Report the observed mode, not the configured one. Return the mode/degraded
flags out of the scoring path and fold them into the payload:

```python
# retrieval_eval.py
def first_relevant_rank(index, row, *, k=3, key=None) -> tuple[int | None, str, bool]:
    results, mode, degraded, _cause = retrieve(index, row["query"], key=key, max_results=k)
    ...
    return rank, mode, degraded

# evals.py::retrieval_metrics
scored = score_labels(index, labels, key=key)          # one pass, see WR-03
return {
    "mode": scored.observed_mode,                       # "semantic" only if every row was
    "degraded_rows": scored.degraded_count,             # ranked semantically
    "key_configured": bool(key),
    ...
}
```

At minimum, assert the invariant instead of asserting the credential: if
`key and observed_mode != "semantic"`, label the block `"keyword_degraded"` and carry
the reason.

---

### CR-02: The seeded citation-denial probe silently disarms when the model searches twice

**File:** `src/relay/agent.py:337-354`

**Issue:** The discard is applied once (`seed_armed` latches), but nothing prevents a
*later* `search_docs` grow-step from putting the dropped id straight back into
`retrieved_ids`. `retrieval.anchors()` returns every heading of a returned doc, so a
second search that returns the same file re-adds the dropped anchor verbatim.
04-03-SUMMARY's deviation #2 identified this exact hazard *within* one call and moved
the discard after the `for hit in results` loop — but the same hazard one call later is
unhandled.

Reproduced (`seed_citation_denial=True`, a fake that searches twice then cites the
first search's top hit):

```
guardrail.citation_denial_seeded
dropped/cited: api.md#rate-limits   guardrails: []
final: resolution {'via': 'send_reply', ...}
```

Armed hook, seeding logged, **no denial**, clean terminal action. A real Claude model
refining its query — or searching once per topic in a two-topic ticket — hits this
routinely. Combined with CR-03 the paid artifact is then indistinguishable from a
genuine recovery, which is precisely the WR-10 unfalsifiability D-08 exists to close.
The test at `tests/test_evals.py:561` cannot catch it: its fake searches exactly once.

**Fix:** Make the drop sticky for the life of the run, not a one-shot mutation:

```python
seeded_drops: set[str] = set()
...
if seed_citation_denial and not seeded_drops and hits:
    dropped = hits[0].get("id")
    if dropped:
        seeded_drops.add(dropped)
        logger.warning("guardrail.citation_denial_seeded", ...)
# applied after EVERY grow, not just the arming one
retrieved_ids -= seeded_drops
```

Add a regression test whose fake issues two `search_docs` calls for the same doc before
citing, and assert the denial still fires.

---

### CR-03: A seeded run's artifact records nothing about the denial, and the only arming path is untested

**File:** `src/relay/evals.py:112-162` (`extract_outcome`), `src/relay/evals.py:91-109`
(`CaseResult`), `src/relay/evals.py:204-237` (`run_case`)

**Issue:** Two gaps that compose into an unfalsifiable paid probe.

1. `extract_outcome` handles `tool_use`, `tool_result`, `usage`, `resolution`, `error`
   — and **ignores `guardrail` events entirely**. `CaseResult` has no field for
   denials. So the entire output of a `seed_citation_denial=True` dispatch is
   `action` / `citations` / `retrieval`. These three cases produce identical artifacts:
   - hook armed → denial fired → model recovered (`action="send_reply"`)
   - hook armed → denial never fired because the model cited the bare doc name, which
     is still in the accept-set (`action="send_reply"`)
   - hook armed → denial never fired because of CR-02 (`action="send_reply"`)

   D-08's claim is "measures whether the model recovers". The artifact cannot express
   the difference between recovering and never being asked to.

2. `run_case(..., seed_citation_denial=True)` is deliberately the *only* arming path
   (no argparse flag — a good call, D-08). It has **zero test coverage**:
   `grep -rn "run_case" tests/` shows both call sites omit the flag, and nothing asserts
   `run_case`'s signature. Deleting `seed_citation_denial=seed_citation_denial` from
   `evals.py:235` leaves all 264 tests green and the paid dispatch permanently
   unarmed — reporting "recovered fine" forever.

**Fix:** Record the guard as an observed fact, and pin the forwarding.

```python
# extract_outcome
outcome["guardrails"] = []          # alongside citations/retrieval
...
elif event.type == "guardrail":
    outcome["guardrails"].append({
        "guard": event.data["guard"],
        "missing_citations": event.data.get("missing_citations"),
    })

# CaseResult
guardrails: list[dict[str, Any]] = field(default_factory=list)
seeded_denial: bool = False         # set from run_case's own flag
```

Then a seeded report reads `seeded_denial=true, guardrails=[{"guard":"citation",...}],
action="send_reply"` — recovery becomes a claim you can check. Add a test mirroring
`test_seed_denial_hook_is_keyword_only_and_default_off` for `run_case`, plus one that
drives `run_case(..., seed_citation_denial=True)` with the hook-dependent fake and
asserts a `citation` guardrail lands in the result.

---

## Warnings

### WR-01: recall@k/MRR are document-level; every `#anchor` label is inert

**File:** `src/relay/retrieval_eval.py:43-49`, `evals/retrieval.jsonl:1-11`

**Issue:** `_accept_set()` unions `result["doc"]`, `result["id"]`, and
`result["anchors"]` — and `retrieval.anchors()` returns `[doc, *every heading of doc]`.
So a result matches a label iff the *document* matches. No label content below doc
granularity can affect the metric. Measured:

| labels | recall@1 | MRR |
|---|---|---|
| as shipped | 0.9091 | 0.9091 |
| every anchor deliberately pointed at the **wrong section** of the right doc | 0.9091 | 0.9091 |
| every row relabeled `["api.md","billing.md","account.md"]` | 0.9091 | 0.9091 |

All three pass `test_retrieval_labels_well_formed` (it only checks membership in
`kb/index.json`). D-02 specifies "query→relevant-chunk-id pairs"; what ships measures
"did retrieve() return one of 3 documents". The `#anchor` half of ten labels is
decoration, and the locator (`_locate_heading`) — the thing that actually produces the
`id` the model is told to cite — is not measured at all.

**Fix:** Either (a) say so plainly in the module docstring and the SUMMARY — "document
recall over a 3-doc corpus" — and drop the anchors from `relevant`, or (b) add a
separate, honest locator metric that *does* discriminate:

```python
def locator_precision(index, labels, *, key=None) -> float:
    """Fraction of top hits whose query-located `id` is one of the labeled anchors."""
    rows = [r for r in scored_labels(labels) if any("#" in x for x in r["relevant"])]
    hits = 0
    for row in rows:
        results, *_ = retrieve(index, row["query"], key=key, max_results=1)
        if results and results[0]["id"] in set(row["relevant"]):
            hits += 1
    return round(hits / len(rows), 4) if rows else 0.0
```

### WR-02: The label queries are hand-authored rewrites, not the golden queries — the headline number swings 0.82–1.00 with the query form

**File:** `evals/retrieval.jsonl:1-12`

**Issue:** The row ids map 1:1 onto `evals/golden.jsonl`, but the `query` strings are
curated keyword-friendly rewrites of the tickets (`"Pro plan pricing"` for subject
`"How much is Pro?"`; `"password reset"` for `"Can't remember my password"`). The agent
never sends these — it composes its own query from the ticket. Measured, keyword mode,
same labels, same retriever:

| query text | recall@1 | MRR |
|---|---|---|
| curated (as shipped) | 0.9091 | 0.9091 |
| golden `subject` | 0.8182 | 0.8182 |
| golden `subject + body` | 1.0000 | 1.0000 |

A ±0.18 swing driven entirely by an undocumented authoring choice. D-03 argues a hard
threshold is unsafe *because the metric is jumpy*; this is a second, larger source of
jumpiness that is not disclosed anywhere and is invisible to every test.

**Fix:** Record the derivation in the file or the module docstring ("hand-authored
search-style queries; not the text the agent emits"), and report at least one
ticket-derived variant alongside so the gap between curated and realistic queries is
visible rather than assumed away.

### WR-03: `retrieval_metrics()` re-runs `retrieve()` three times per row — 33 Voyage embeddings per paid run, and three metrics that can disagree

**File:** `src/relay/evals.py:85-87`

**Issue:** `recall_at_k(…,1)`, `recall_at_k(…,3)` and `mrr(…)` each iterate the scored
rows and each call `retrieve()`. Confirmed by log count: **33** `retrieve()` calls for
11 rows. Consequences:

- With `VOYAGE_API_KEY` now wired into `evals.yml` (D-11), every paid dispatch buys 3×
  the embeddings it needs. 04-01-SUMMARY's cost note ("~11 real Voyage
  query-embedding calls") understates the shipped per-run spend by 3×.
- The three numbers are no longer guaranteed mutually consistent: `_embed_query`
  degrades to keyword on failure, per call. One transient Voyage 429 during the
  `recall@1` pass and not the `recall@3` pass yields a report where `recall@3 < recall@1`
  — an impossible reading that `test_recall_and_mrr_over_labeled_set:271` asserts against
  in keyword mode but nothing checks in the mode that can actually violate it.
- 33 blocking `httpx.post` calls run on the event loop inside `run_evals` (a coroutine);
  a Voyage stall blocks for up to 33 × 20s.

**Fix:** Score once, derive all three:

```python
ranks = [first_relevant_rank(index, row, k=3, key=key) for row in scored_labels(labels)]
recall_1 = mean(r == 1 for r in ranks)
recall_3 = mean(r is not None for r in ranks)
mrr_ = mean(1.0 / r if r else 0.0 for r in ranks)
```

(`retrieve(max_results=3)` is a superset of `max_results=1` for this retriever, so
recall@1 is `rank == 1`.) Wrap the whole block in `asyncio.to_thread` when called from
`run_evals`.

### WR-04: The report-only block runs unguarded inside the report literal — one exception discards a paid run

**File:** `src/relay/evals.py:303`

**Issue:** `"retrieval_metrics": retrieval_metrics()` is evaluated *after*
`asyncio.gather` has spent the money and *before* `out_path.write_text(...)` in
`main()`. Any exception — a malformed line in `evals/retrieval.jsonl`, a `KeyError`
from a label missing `query`, an `OSError` reading the labels — propagates out of
`run_evals`, out of `asyncio.run`, and the entire 12-case paid report is lost with a
traceback and a non-zero exit. D-03 says the retrieval metric never gates; via this
path it is the only thing that can fail the job outright.

**Fix:** Isolate it, the same way `bounded()` already isolates a bad case:

```python
try:
    metrics = retrieval_metrics()
except Exception as exc:  # noqa: BLE001 — report-only, must never sink a paid run
    metrics = {"error": f"{type(exc).__name__}: {exc}"}
```

and make `print_summary` tolerate the `error` shape.

### WR-05: `__seeded_missing__` is handed to the model as a citable id, and the guard accepts it

**File:** `src/relay/agent.py:350`, `src/relay/agent.py:162-171`

**Issue:** The sentinel is added to `retrieved_ids`, so it flows into the denial
payload's `available` string and `retrieved_ids` list — i.e. the guard tells the model
"you may cite `__seeded_missing__`" — and any subsequent citation of it passes the
subset check. Observed in the shipped test's own recovery:

```
recovered_with: ['__seeded_missing__', 'api.md', 'api.md#api-access',
                 'api.md#authentication', 'api.md#webhooks']
```

Two consequences: (1) RAG-04's property ("a fabricated source is denied") is knowingly
violated on the seeded path, and the shipped mechanism test bakes that in; (2)
`extract_outcome` builds `retrieved_ids` from the tool payloads, which never contain the
sentinel — so a seeded paid run in which the model cites it registers as a
**citation-faithfulness violation the running system never saw**, in the same report
EVAL-03 is supposed to make trustworthy.

The stated justification ("keeps the set non-empty") only bites when the dropped id is
the doc's own bare name, which happens only for a heading-less doc.

**Fix:** Drop the sentinel; guard the edge case instead.

```python
if seed_citation_denial and not seed_armed and hits:
    dropped = hits[0].get("id")
    # Never empty the set — an empty accept-set denies everything and the denial
    # stops being recoverable, which is a different experiment.
    if dropped and retrieved_ids - {dropped}:
        retrieved_ids.discard(dropped)
        seed_armed = True
```

### WR-06: `scored_queries` re-implements the exclusion rule instead of calling `scored_labels`

**File:** `src/relay/evals.py:84`

**Issue:** `len([r for r in labels if r.get("relevant")])` duplicates
`scored_labels()`'s predicate. The reported denominator and the denominator the metric
actually divides by are now two independent expressions. Change the rule in one place
(e.g. to also exclude rows whose relevant ids are all absent from the index) and the
report silently states a denominator the numbers were not computed with — the exact
failure the 04-01 mutation table was written to prevent.

**Fix:** `"scored_queries": len(scored_labels(labels))`, importing `scored_labels`
alongside `recall_at_k`/`mrr`.

### WR-07: EVAL-01's delivery path has no test — deleting `retrieval_metrics` from the report leaves 264 tests green

**File:** `src/relay/evals.py:71-88`, `src/relay/evals.py:303`, `src/relay/evals.py:326-334`

**Issue:** `grep -rn "retrieval_metrics\|run_evals\|print_summary" tests/` returns
nothing. The three shipped tests exercise `retrieval_eval.py` in isolation. Nothing
asserts that the report *carries* the block, that `mode` is populated, or that
`print_summary` renders it. Removing line 303 entirely — the whole deliverable of
plan 01 task 2 — is a green build. This is the same shape as WR-10's original finding:
the computation is proven, the artifact it is supposed to appear in is not.

**Fix:** One free test, keyword-pinned:

```python
def test_report_carries_labeled_retrieval_metrics(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    m = evals.retrieval_metrics()
    assert m["mode"] == "keyword"
    assert m["scored_queries"] == len(scored_labels(load_labels()))
    assert 0.0 <= m["recall@1"] <= m["recall@3"] <= 1.0
    print_summary({..., "retrieval_metrics": m, "results": []})   # renders without KeyError
```

### WR-08: Wiring `VOYAGE_API_KEY` into `evals.yml` changes the *gated* 12-case suite's retrieval mode, and that configuration was never run

**File:** `.github/workflows/evals.yml:28`

**Issue:** The secret does not only feed `retrieval_metrics` — it feeds every
`search_docs` call in all 12 golden runs, flipping them from keyword to semantic
ranking. The `pass_rate < 0.8` gate (D-04, explicitly "unchanged") now evaluates a
configuration it has never been measured under. 04-03-SUMMARY concedes the paid
dispatch was not run; 04-01-SUMMARY concedes the secret's existence is unverified. So
the gate's inputs changed and the gate's behaviour under those inputs is unknown in
both directions (secret present → untested retrieval mode; secret absent → GitHub
injects `""`, keyword, which at least matches the old baseline).

**Fix:** Run the paid dispatch once and record the pass rate and the observed
retrieval mode before treating the 0.8 gate as still-valid; or scope the key so only
the metric block sees it (e.g. compute metrics in a separate step/process) if the
intent was to leave the graded runs on the measured baseline.

### WR-09: `test_production_never_arms_the_seed_denial_hook` is a substring grep over one file

**File:** `tests/test_evals.py:491-495`

**Issue:** It reads `src/relay/main.py` and asserts the literal string is absent. It
does not cover any other production caller, and it cannot see indirect arming
(`run_ticket(client, registry, ticket, **opts)`). It is better than nothing but it
asserts a spelling, not a property.

**Fix:** Assert over the package, and assert the runtime property too:

```python
src = Path(__file__).parent.parent / "src" / "relay"
armed = [p.name for p in src.glob("*.py")
         if p.name not in {"agent.py", "evals.py"} and "seed_citation_denial" in p.read_text()]
assert armed == [], f"non-eval modules reference the eval-only hook: {armed}"
```

plus a test that a normal `run_ticket(...)` call emits no `guardrail.citation_denial_seeded`
log record.

---

## Info

### IN-01: `hit.get("doc")` in `extract_outcome`'s union is unfalsifiable

**File:** `src/relay/evals.py:153`

`retrieval.anchors()` always returns `[doc, *headings]`, and the test helper `_hit()`
always prepends the doc name to `anchors`. So removing `hit.get("doc")` from the union
changes no observable behaviour and no test fails — contrary to the 04-02-SUMMARY
mutation row, which only verified dropping `doc` **and** `anchors` together. Keep the
line as defence against a payload with no `anchors` key, but note it is not covered.

### IN-02: Three of the four headline assertions in the recall test are tautologies

**File:** `tests/test_evals.py:268-270`

`assert 0.0 <= r1 <= 1.0` (×3) cannot fail: `recall_at_k`/`mrr` are `hits/len(rows)`
with `hits <= len(rows)` by construction, and both early-return `0.0`. Only the
`password-reset` exact-match assert (`:277`) and the mixed-denominator asserts
(`:285-287`) carry weight. Harmless, but they pad the test with checks that survive any
mutation.

### IN-03: `mrr()` is MRR@3 but is reported and printed as `mrr`

**File:** `src/relay/retrieval_eval.py:83-99`, `src/relay/evals.py:88`, `:331`

Rank is truncated at `k=3` (`retrieve(max_results=k)`), so a relevant doc at rank 4
scores 0 rather than 0.25. That is a reasonable choice; the label should say so
(`"mrr@3"`) or a future corpus growth will change the number's meaning silently.

### IN-04: New tests use cwd-relative paths where the same file uses `__file__`-relative ones

**File:** `tests/test_evals.py:236` (`Path("kb/index.json")`), `:263`, `:294`
(`load_index(Path("kb"))`), `load_labels()` default at `src/relay/retrieval_eval.py:25`

`test_production_never_arms_the_seed_denial_hook:494` and `conftest.KB_DIR` both use
`Path(__file__).parent.parent`. The new tests fail if pytest is invoked from anywhere
but the repo root. CI runs from root so this is latent, not live.

### IN-05: `${{ inputs.threshold }}` is interpolated directly into the `run:` shell

**File:** `.github/workflows/evals.yml:29`

Pre-existing (not introduced by this phase) but the file is in scope: a
`workflow_dispatch` input is expanded by the runner into the shell command line before
bash sees it, so a dispatcher with write access can inject arbitrary shell. Pass it via
`env:` and reference `"$THRESHOLD"` instead.

### IN-06: Minor style/robustness nits in the new code

- `src/relay/agent.py:352-354` — the seeded-probe log is `logger.warning` for a
  deliberate, expected action; `logger.info` matches the codebase's use of WARNING for
  "something went wrong or was denied". The continuation-line indentation also departs
  from the `extra={"ctx": {...}}` block style used at `:361`, `:365`, `:386`, `:406`.
- `src/relay/retrieval_eval.py:49` — `result["doc"]` / `result["id"]` are indexed
  directly while `anchors` uses `.get(...)`; a payload shape change raises `KeyError`
  mid-report rather than degrading. `.get()` on all three would match the tolerant
  handling `extract_outcome` uses on the same structure.
- `tests/test_evals.py:465` — `assert len(cited) >= 5` is exactly the number of citing
  cases in the report; the pin has no headroom, so adding a non-citing case is fine but
  removing a citing one fails with a count message rather than a meaningful one.

---

## Verified as correct (do not re-litigate)

Each item below was checked by execution or by tracing the code, not by reading the
SUMMARY.

1. **The seed hook cannot leak beyond one run.** `seed_citation_denial`, `seed_armed`
   and `retrieved_ids` are all locals of the `run_ticket` frame; `bind_to_ticket`
   closes over the per-run set; `run_case` builds a fresh `build_registry()` per case;
   the shared `app.state.registry` never holds either. No module-level state is touched.
2. **The hook is default-off and unreachable from production.** `KEYWORD_ONLY`,
   default `False` (asserted at `tests/test_evals.py:482`); `main.py` and
   `mcp_server.py` contain no reference; `run_evals`' 12-case loop calls
   `run_case(client, case, kb_text)` with no flag; there is deliberately no argparse
   switch. (The *coverage* gap on the arming path is CR-03; the containment is sound.)
3. **Recall can never reach the `--threshold` gate.** `main()` compares only
   `report["pass_rate"]`, which is `passed/len(results)` over `CaseResult.passed`;
   `retrieval_metrics` contributes to no field feeding it. The only failure path is the
   unguarded exception in WR-04.
4. **The empty-`relevant` exclusion holds on both metrics.** `recall_at_k` and `mrr`
   each route through `scored_labels()` before iterating; there is no path that scores a
   row `scored_labels` rejected. Verified by reading both functions and by the shipped
   `len(scored_labels(mixed)) == len(mixed) - 1` assertion.
5. **EVAL-02's correct-ticket-write assertion is load-bearing against over-blocking.**
   Executed mutation: `_execute_guarded` patched to deny *every* `send_reply` with a
   `ticket_binding` denial → 2 guardrail events, 0 reply rows,
   `_reply_ticket_ids(conn) == [1]` fails. The test distinguishes rejection from
   breakage, as D-05 requires.
6. **EVAL-03's anchors-union half is non-vacuous.** The report cites
   `api.md#webhooks` while the located `id` is `api.md#rate-limits`, and the membership
   assert at `:466` pins that specific citation into the non-vacuity set, so dropping
   the `anchors` union from `extract_outcome` fails the subset loop. The
   `len(cited) >= 5` pin plus the fabricated-citation negative control at `:470-471`
   together rule out the vacuous-report failure mode.
7. **No new test can make a real Voyage or Anthropic call.** `_no_outbound_http` is
   autouse over the whole suite and patches `httpx.post`, which is the single egress in
   `retrieval._embed_query`. Belt and braces: the metric tests pass `key=None`
   explicitly (so `retrieve` never consults settings), and every test that drives
   `search_docs` through the registry pins `settings.voyage_api_key = None`. All model
   traffic goes through `FakeClient` / `HookDependentRecoveringClient`. Full run:
   264 passed, `ruff check src tests` clean.
8. **`VOYAGE_API_KEY` reaches settings under that exact name.** `config.py:85` uses
   `validation_alias="VOYAGE_API_KEY"` (no `RELAY_` prefix), so the new `evals.yml` env
   entry is correctly spelled; an absent secret yields `""` → falsy → keyword baseline,
   as documented.
9. **The change to `tests/test_evals.py` is purely additive** — 386 insertions, 0
   deletions against `f80d90e`. No existing assertion was weakened or removed.
10. **`HookDependentRecoveringClient` is genuinely hook-dependent.** Its first citation
    is read out of the live `search_docs` payload (`last["results"][0]["id"]`), which is
    the exact id the hook drops, so mutation B (making the discard a no-op) removes the
    denial and fails the test — unlike `RecoveringFakeClient`'s hardcoded absent id.
    The `client.cited_first not in client.recovered_with` assertion does prove the
    accept-set was narrowed rather than merely reported on.

---

_Reviewed: 2026-08-11T09:40:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_

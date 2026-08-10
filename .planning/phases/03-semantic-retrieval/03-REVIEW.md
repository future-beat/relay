---
phase: 03-semantic-retrieval
reviewed: 2026-08-10T00:00:00Z
depth: standard
files_reviewed: 16
files_reviewed_list:
  - src/relay/retrieval.py
  - scripts/build_index.py
  - kb/index.json
  - src/relay/tools.py
  - src/relay/guardrails.py
  - src/relay/agent.py
  - src/relay/prompts.py
  - src/relay/models.py
  - src/relay/config.py
  - tests/test_retrieval.py
  - tests/test_index.py
  - tests/test_tools.py
  - tests/test_guardrails.py
  - tests/conftest.py
  - pyproject.toml
  - .env.example
findings:
  critical: 4
  warning: 10
  info: 8
  total: 22
status: issues_found
---

# Phase 3: Code Review Report

**Reviewed:** 2026-08-10
**Depth:** standard (with targeted dynamic probes against the real KB and committed index)
**Files Reviewed:** 16
**Status:** issues_found

## Summary

The engineering discipline here is high — the degradation paths are genuinely
non-raising, the API key never leaves the process outside an `Authorization`
header, `input_type` is asserted off the intercepted request body on both sides,
and the whole-file (D-02) contract is byte-verified against disk. The self-reported
caveats in the summaries are honest and mostly accurate. 195 tests pass, ruff is clean.

That said, the phase's stated risk class — **silent quality degradation, or a guard
that never fires** — is where the defects cluster, and four of them are not
theoretical. Each was reproduced against the real `kb/` and the committed
`kb/index.json`:

1. **The citation guard denies correct citations.** The accept-set is one
   query-derived id per doc, but the model is handed the *whole file* with every
   heading visible. Citing `billing.md#upgrades-and-downgrades` — the section that
   actually answers "upgrade my plan" — is **denied**, while the noise ids
   `account.md#two-factor-authentication` and `api.md#api-access` are **accepted**.
   The guard is inverted for the case it matters most in.
2. **The RAG-02 staleness gate has hash collisions** on realistic KB edits.
   Splitting a doc in two, renaming a file, or adding an empty doc all leave
   `kb_sha256` unchanged — the runtime then serves stale vectors and CI stays green.
3. **A missing/stale index while `VOYAGE_API_KEY` *is* set degrades with
   `degraded=False` and no `notice`** — indistinguishable on the wire from the
   intended keyless baseline. Combined with (2) and the fact that `VOYAGE_API_KEY`
   is documented nowhere outside `.env.example`, the live demo can run keyword-only
   forever with no runtime signal at all.
4. **The `[]` escalation signal is query-phrasing-dependent, not robust.** The
   keyword half of the union has no floor and no stopword filter, so
   `integrate with Salesforce` returns all three docs while `Salesforce integration`
   returns `[]`. The 0.30 calibration gates only the semantic half.

Assessments of the four self-reported items are folded into the findings below
(see CR-01/WR-10 for #1, CR-04 for #2, "Verified as correct" for #3, WR-02/IN-02
for #4 — I disagree that the heading-fallback test is the weakest one).

---

## Critical Issues

### CR-01: Citation guard denies valid, better-grounded citations while accepting noise ones

**File:** `src/relay/agent.py:150-166`, `src/relay/retrieval.py:244-257`

**Issue:** `retrieved_ids` is grown from `hit["id"]` only
(`agent.py:306-308`) — **one id per retrieved doc**, and that id is
`{doc}#{slug(_locate_heading(doc, query))}`, i.e. derived from the *query*, not from
the document. But `_result` hands the model `doc.text` — the **entire file**, with all
its `##` headings in plain sight. The prompt (`prompts.py`) tells the model to cite
"the `id` of every search_docs result you relied on", and the model has no way to know
that the only legal anchor is the one the lexical locator happened to pick.

Reproduced against the real KB and committed index:

```
query: "upgrade my plan" -> retrieved_ids:
  {'account.md#two-factor-authentication', 'api.md#api-access', 'billing.md#billing-and-plans'}

cite='billing.md#upgrades-and-downgrades'   is_error=True   denied_by=citation   <- CORRECT section, DENIED
cite='billing.md'                           is_error=True   denied_by=citation   <- bare doc name, DENIED
cite='BILLING.MD#REFUNDS'                   is_error=True   denied_by=citation
cite='billing.md#billing-and-plans'         is_error=False  OK                   <- the intro paragraph, ACCEPTED
```

Three consequences, in ascending severity:

- The most natural model citation forms (the bare doc name; a real heading read out of
  the returned text) are structurally guaranteed to be denied.
- Each false denial costs a round trip and pushes the run toward the
  `ended_without_action` trap D-07 explicitly warns about. The `RecoveringFakeClient`
  test proves the *mechanism* recovers; nothing proves a real model does, because the
  guard never fired in the paid eval (see WR-10).
- The guard's accept-set for an "upgrade" query is
  `{2fa, api-access, billing-and-plans}` — two of which are pure keyword noise
  (see CR-04) — while the accurate anchor is rejected. A guard that rejects
  better grounding than it accepts is worse than no guard.

This also answers self-reported item #1 ("is there a cheap way to force the guard to
fire?"): **it already fires far too easily, on correct behaviour**. That is why zero
denials in the paid eval is not reassuring — see WR-10 for the alternative reading.

**Fix:** Build the accept-set from the document, not the query. In
`agent.py`, widen what a successful `search_docs` contributes:

```python
if block.name == "search_docs" and not is_error:
    for hit in payload.get("results", []):
        doc = hit.get("doc")
        if not doc:
            continue
        retrieved_ids.add(doc)                 # bare doc name is a legal cite
        if hit.get("id"):
            retrieved_ids.add(hit["id"])
        for anchor in hit.get("anchors", ()):  # every heading of the returned file
            retrieved_ids.add(anchor)
```

and have `retrieval._result` emit the full anchor list alongside the primary id:

```python
return {
    "doc": doc.doc,
    "heading": heading,
    "id": f"{doc.doc}#{slug(heading)}" if heading else doc.doc,
    "anchors": [f"{doc.doc}#{slug(h)}" for h in doc.headings],
    "text": doc.text,
    "score": round(score, 6),
}
```

Normalise on comparison too (`c.strip().lower()` on both sides) so casing/whitespace
drift is not a denial. The guard then still catches the case it exists for — a doc that
was never retrieved at all — without punishing correct grounding.

---

### CR-02: `kb_sha256` collides on realistic KB edits, so the RAG-02 staleness gate silently passes on a stale index

**File:** `src/relay/retrieval.py:51-60`

**Issue:** The digest concatenates raw file bytes with **no filename and no length
delimiter**:

```python
for path in sorted(Path(kb_dir).glob("*.md")):
    digest.update(path.read_bytes())
```

`hash("AB") == hash("A") + hash("B")` in this construction, so the hash is blind to
where one file ends and the next begins, and blind to names entirely. Reproduced:

```
SPLIT-DOC  (billing.md cut in half into billing.md + billing2.md)  same hash? True
RENAME     (api.md -> apiz.md, sort order preserved)               same hash? True
EMPTY-ADD  (add an empty zzz.md)                                   same hash? True
```

Splitting a KB document, or moving a trailing paragraph from `api.md` into the head of
`billing.md`, are ordinary editorial operations. After any of them:
`load_index`'s `_meta_mismatch` (`retrieval.py:309`) sees a matching hash and loads the
**old vectors describing text nobody is serving any more**, and the *new* file is
invisible to semantic ranking entirely (it has no row in the matrix, and `docs` comes
from `raw["docs"]`, not from disk — `retrieval.py:109-112`). CI's
`test_index_matches_kb` and `build_index.check()` use the same function and pass too.

This defeats the single mechanism RAG-02 exists for, and it fails in the *dangerous*
direction — stale retrieval that looks healthy.

**Fix:** Bind name and length into the digest:

```python
def kb_sha256(kb_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(kb_dir).glob("*.md")):
        body = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(body)).encode("ascii"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()
```

Rebuild `kb/index.json` after the change (its stamp will differ, which is correct).
Consider also asserting in `load_index` that `{d["doc"] for d in raw["docs"]}` equals
`{p.name for p in kb_dir.glob("*.md")}` — a cheap, independent belt to the hash's braces.

---

### CR-03: A key-configured deployment with an unusable index degrades silently — no `degraded` flag, no `notice`, no way to tell it from the intended baseline

**File:** `src/relay/retrieval.py:159-164`

**Issue:**

```python
if key and index.matrix is not None:
    ...            # only here can `degraded` ever become True
```

If the key is set but `index.matrix is None` — missing artifact, malformed JSON, model
or dimension drift, or a stale hash — the whole branch is skipped and `degraded` stays
`False`, `mode` stays `"keyword"`. Reproduced:

```
NO-INDEX + KEY -> mode: keyword  degraded: False   (no notice will be emitted)
```

The only signal is one `retrieval.index_unavailable` WARNING at process start
(`retrieval.py:126`). `agent.py:335` gates the `notice` event on
`payload.get("degraded")`, so no run ever reports it; `/metrics` and `/health` do not
surface it either. The SSE stream and the dashboard show `retrieval_mode: "keyword",
degraded: false` — byte-identical to the deliberate keyless CI baseline.

This is exactly the RAG-05 / D-14 case ("we fell back to keyword search" must be
visible), and it is the one that will actually happen in production: chain it with
CR-02 (a stale index that passes the gate) and WR-04 (`VOYAGE_API_KEY` documented
nowhere for Fly) and the live demo can run keyword-only for the rest of the milestone
with nobody able to tell.

**Fix:** Make "credential present but semantic path unusable" a first-class degradation:

```python
if key and index.matrix is None:
    # Configured for semantic retrieval and not getting it — the operator needs to
    # see this on every run, not once in a boot log nobody reads.
    degraded = True
elif key:
    vector = _embed_query(...)
    ...
```

Carry the reason forward so the notice is actionable: add
`unavailable_reason: str | None` to `Index` (set from `_meta_mismatch`/the exception in
`load_index`) and include it in the `notice` payload as
`{"kind": "retrieval_degraded", "cause": "index_unavailable" | "voyage_failed", ...}`.
Add a test that asserts `degraded is True` for `(key set, matrix None)`; none exists —
`test_load_index_missing_file_falls_back_to_keyword_mode` (`test_retrieval.py:239-245`)
currently **asserts the buggy behaviour** (`(mode, degraded) == ("keyword", False)`
with `key="test-key"`).

---

### CR-04: The `[]` escalation signal (D-03/D-04) is defeated by query phrasing — the keyword half of the union has no floor and no stopword filter

**File:** `src/relay/retrieval.py:173-189` (union), `src/relay/retrieval.py:232-241`
(`_keyword_hits`), `src/relay/retrieval.py:291-292` (`_terms`)

**Issue:** `_terms` keeps any token longer than 2 characters — so `the`, `and`, `for`,
`does`, `you`, `with` are all search terms — and `_keyword_hits` counts **unanchored
substrings** over the whole file. The result is that essentially any multi-word natural
query hits every document, and those hits enter `ranked` (line 178) with **no floor
applied to them at all**. Reproduced against the real KB:

```
'Salesforce integration'          -> []                                    (escalates)
'Salesforce CRM integration'      -> []                                    (escalates)
'integrate with Salesforce'       -> account.md, billing.md, api.md        (does not)
'does the KB cover Salesforce'    -> account.md, billing.md, api.md        (does not)
'Does the product work on Mars?'  -> account.md (22.0), billing.md (12.0), api.md (6.0)
```

`Does the product work on Mars?` — an unambiguously uncovered topic — returns the
entire knowledge base as "hits", each with a citation id the model may cite as
grounding. The 0.30 floor, calibrated with real measurements in 03-06, never sees these.

The summary's characterisation ("mostly harmless on a 3-doc corpus… on a larger KB the
keyword half is the weaker link") **understates it in the wrong direction**. Corpus size
is not the variable; *query phrasing* is. The `salesforce-integration` eval case
escalated because the model happened to emit a two-word topical phrase. One extra
stopword in the model's query — `integrate with Salesforce` — flips the same case to
"three full docs returned", and D-03's escalation signal is gone. The acceptance
evidence for D-04 therefore rests on model formatting luck, not on a guard.

**Fix:** Give the keyword half a gate of its own, so the union can actually be empty:

```python
_STOPWORDS = frozenset({"the","and","for","you","are","this","that","with","does",
                        "how","can","was","but","not","our","your","have","from",
                        "there","what","please","about","would","could","them"})

def _terms(query: str) -> list[str]:
    return [t for t in _WORD_RE.findall(query.lower())
            if len(t) > 2 and t not in _STOPWORDS]

def _keyword_hits(docs, query, *, min_score: float = 2.0):
    terms = _terms(query)
    if not terms:
        return []
    # Whole-word matches, not substrings: "and" must not score inside "standard".
    pattern = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(t) for t in terms))
    scored = [(i, float(len(pattern.findall(d.text.lower())))) for i, d in enumerate(docs)]
    hits = [(i, s) for i, s in scored if s >= min_score]
    hits.sort(key=lambda pair: (-pair[1], pair[0]))
    return hits
```

Then re-run the 12-case eval to confirm `salesforce-integration` still escalates *and*
that a paraphrased off-topic query now also returns `[]`. Add a regression test that
asserts `retrieve(index, "Does the product work on Mars?", key=None) == []` — the
current `test_search_docs_no_match` uses `"zzzzz qqqqq"`, which no real model would
ever emit and which passes for the wrong reason.

---

## Warnings

### WR-01: A NaN or all-zero query embedding is accepted — semantic ranking goes inert while reporting `mode="semantic", degraded=False`

**File:** `src/relay/retrieval.py:213-216`

**Issue:** `_embed_query` validates the *shape* but never the *values*.
`float(np.linalg.norm(v)) or 1.0` treats `nan` as truthy, so a NaN vector divides by NaN
and stays NaN; an all-zero vector divides by the `1.0` fallback and stays zero. Either
way every cosine falls below the floor, `semantic_hits` is empty, and the caller reports
the healthy `mode="semantic", degraded=False`. Note `json.loads` accepts bare `NaN` and
`Infinity` literals by default, so a malformed upstream body reaches this code. Reproduced:

```
NaN vector   -> mode=semantic degraded=False results=['billing.md#refunds']   (keyword only)
zero vector  -> mode=semantic degraded=False results=['billing.md#refunds']   (keyword only)
```

**Fix:**

```python
norm = float(np.linalg.norm(vector))
if not np.isfinite(vector).all() or norm == 0.0:
    raise ValueError("voyage returned a non-finite or zero embedding")
return vector / norm
```

The surrounding `except Exception` then correctly degrades with `degraded=True`.
Also pass `parse_constant=...` or reject non-finite values explicitly if you want to
harden the JSON boundary.

### WR-02: The shipped `retrieval_floor = 0.30` is exercised by no test

**File:** `src/relay/config.py:106`, `tests/test_retrieval.py:93,104,112,121,130,139,150,159,171,178,243,271`

**Issue:** Every single retrieval test hardcodes `floor=0.55` — the value 03-06 proved
was *wrong* — and no test reads `settings.retrieval_floor` at all. Setting the shipped
floor to `0.95` (semantic ranking inert, the exact failure 03-06 caught) or to `0.0`
(the escalation signal removed) keeps 195/195 green. The one measured, calibrated
constant in the phase is the one thing with zero regression coverage.

**Fix:** Add a calibration guard test pinned to the recorded measurements:

```python
def test_shipped_floor_sits_between_the_measured_bands():
    # 03-06 measured: off-topic tops out at 0.2543, the lowest covered query is 0.2659,
    # and 0.30 was chosen for margin. Anything outside this band is a recalibration,
    # not an edit.
    assert 0.26 < settings.retrieval_floor < 0.34

def test_off_topic_is_below_the_shipped_floor_and_covered_is_above(index, voyage):
    voyage(_scaled_basis(_doc_position(kb_docs, "billing.md"), 0.2543))
    assert retrieve(index, "salesforce crm sync", key="k")[0] == []
    voyage(_scaled_basis(_doc_position(kb_docs, "billing.md"), 0.3408))
    assert retrieve(index, "export data", key="k")[0]
```

### WR-03: `_locate_heading` biases the citation anchor toward the doc title/intro section

**File:** `src/relay/retrieval.py:260-288`

**Issue:** Three compounding causes: `_HEADING_RE` (`^##?\s+`) treats the `#` doc title
as a peer of the `##` sections, so `sections[0]` is always the intro; the score is a raw
substring count, unnormalised by section length, and intros restate the doc's vocabulary
densely; and ties resolve to `sections[0]` because the comparison is `score > best_score`
starting from `best = sections[0][0], best_score = 0.0`. Measured on `kb/billing.md`:

```
query "upgrade my plan"  ->  billing.md#billing-and-plans   (the intro)
                    correct:  billing.md#upgrades-and-downgrades
```

This is the join key Phase 4's evals and Phase 5's dashboard trace will display as
"the source". D-13 licenses "best-effort", but pointing at the intro for a question the
document answers two sections down is a defect, not best-effort — and it is what makes
CR-01 bite hardest.

**Fix:** Split sections on `##` only (keep `#` as the doc title, not a section), and
normalise by section length:

```python
_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)   # level-2 only
...
score = sum(blob.count(term) for term in terms) / max(len(blob.split()), 1)
```

### WR-04: `VOYAGE_API_KEY` is documented nowhere outside `.env.example`

**File:** `.env.example:4-10` (the only mention)

**Issue:** `grep -rn "VOYAGE" README.md docs/ fly.toml scripts/demo.sh .github/` returns
nothing. There is no `fly secrets set VOYAGE_API_KEY=...` step, no README note, no
deployment checklist. The phase's headline capability therefore ships **off by default
in production**, and per CR-03 there is no runtime signal that it is off.

**Fix:** Add the secret to the deploy documentation and, ideally, a startup assertion
that logs at INFO which retrieval mode the process came up in
(`retrieval.mode_selected` with `semantic|keyword` and the reason), so the boot log
answers the question without a code read.

### WR-05: `_embed_query` retries every failure class with no backoff and no retryability check

**File:** `src/relay/retrieval.py:198-217`

**Issue:** The loop retries unconditionally: a `401` (bad key), a `400` (bad request), a
`429` (rate limited) and a connect timeout are all retried immediately, with no sleep
and no jitter. Worst case is `2 × 10s = 20s` of blocked thread per `search_docs` call;
with `max_agent_steps = 10` that is up to ~200 s added to one SSE stream during a Voyage
outage, with no event emitted in between. Retrying a 429 with zero delay also makes the
rate limit worse.

**Fix:** Only retry transient classes, and back off:

```python
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
...
except httpx.HTTPStatusError as exc:
    last_error = exc
    if exc.response.status_code not in RETRYABLE_STATUS:
        break
    time.sleep(0.25)
except (httpx.TimeoutException, httpx.TransportError) as exc:
    last_error = exc
    time.sleep(0.25)
except Exception as exc:  # noqa: BLE001 — degrade, never end the run
    last_error = exc
    break
```

Consider lowering `REQUEST_TIMEOUT` to ~5 s: a query embedding that takes longer than
that has already cost more than the keyword fallback is worth.

### WR-06: `conftest._no_outbound_http`'s "fail loudly" guarantee does not hold through `retrieval._embed_query`

**File:** `tests/conftest.py:62-79`, `src/relay/retrieval.py:217`

**Issue:** The fixture raises `AssertionError`, and `_embed_query` catches bare
`Exception` — which `AssertionError` subclasses. Reproduced: with the conftest guard
installed and a key set, `retrieve()` returns `mode="keyword", degraded=True` rather
than failing. The fixture docstring claims it makes an unmocked call "fail loudly"; for
the one code path in the repo that makes outbound calls, it silently converts the
violation into a degradation. If the fixture is ever dropped, a developer with
`VOYAGE_API_KEY` in `.env` makes real, billed calls from unit tests and nothing signals
it.

**Fix:** Raise something outside the swallow, and assert the net actually works:

```python
class OutboundHTTPBlocked(BaseException):   # deliberately not an Exception
    """Bypasses retrieval's degrade-never-raise boundary so a violation is visible."""

def _forbidden(*args, **kwargs):
    raise OutboundHTTPBlocked("a test attempted a real outbound HTTP call")
```

plus a meta-test asserting that `retrieve(..., key="x")` under the fixture raises rather
than degrading.

### WR-07: Three independent copies of the index/settings drift comparison; the builder's `--check` path is dead

**File:** `scripts/build_index.py:139-155`, `src/relay/retrieval.py:301-311`, `tests/test_index.py:107-116`

**Issue:** `build_index.check()`, `retrieval._meta_mismatch()` and
`test_index_matches_kb` each independently re-implement "model matches, dimension
matches, `kb_sha256` matches". `scripts/build_index.py --check` is invoked by nothing —
not CI, not `scripts/demo.sh`, not a Makefile — so `check()` and the `--check` flag are
reachable only from one test. This directly contradicts the builder docstring's stated
rationale (`build_index.py:19`: "two copies would drift"). A fix to CR-02 must now be
mirrored in three places or the gate is inconsistent.

**Fix:** Export one predicate from `retrieval.py` (e.g.
`index_mismatch(kb_dir) -> str | None`) and have `check()`, `_meta_mismatch()` and the
CI test all call it. Either wire `python scripts/build_index.py --check` into
`.github/workflows/ci.yml` or delete the flag.

### WR-08: `scripts/` is outside the lint gate

**File:** `.github/workflows/ci.yml:19` (`ruff check src tests`)

**Issue:** `scripts/build_index.py` is 197 lines of new, importable source (it *is*
imported, by `tests/test_index.py:35-40`) and is never linted. `ruff check --select E501`
already finds violations there.

**Fix:** `ruff check src tests scripts` in CI.

### WR-09: `citations` items are unbounded in length and are echoed verbatim into the model's context on denial

**File:** `src/relay/guardrails.py:36`, `src/relay/agent.py:157-166`

**Issue:** `citations: list[str] = Field(default_factory=list, max_length=20)` caps the
*count* but not the *item size* — every sibling field in `guardrails.py` caps length
(`body` 10 000, `query` 500, `email` 254). The denial payload then echoes
`missing_citations` back verbatim into the tool result, which becomes model input. A
prompt-injected ticket body can steer the model into 20 multi-KB citation strings and
have them amplified back into the context. The `RunBudget` catches the cost eventually,
but only after the tokens are spent.

**Fix:**

```python
from typing import Annotated
from pydantic import StringConstraints

CitationId = Annotated[str, StringConstraints(min_length=1, max_length=200)]
citations: list[CitationId] = Field(default_factory=list, max_length=20)
```

and truncate what the denial echoes (`missing[:5]`, each item sliced to 200 chars).

### WR-10: Nothing records whether the model ever emitted `citations`, so "the guard never fired" is unfalsifiable

**File:** `src/relay/evals.py:85-105` (`extract_outcome`), `eval_results/eval-20260810T065817Z.json`

**Issue:** The after-run eval artifact records `action / category / grounded /
invented_claims / quality / cost / error` and nothing about retrieval or citations.
`extract_outcome` reads `tool_use` inputs for `set_category` and `send_reply.body` but
never `send_reply.citations`, and never `search_docs` results. So "zero
`guardrail.citation_unretrieved` lines across 12 cases" is **equally consistent with
"the model never passed `citations` at all"**, in which case the entire RAG-04 guard is
inert in production and every test still passes.

Assessment of self-reported item #1: the risk is correctly characterised as *open*, but
under-scoped — the open question is not only "does the model recover from a denial" but
"does the model ever cite". And per CR-01 the cheap way to force a firing already exists
and is a bug, not a probe.

**Fix (cheap, ~$0.03):** Extend `extract_outcome` with
`"citations": tool_input.get("citations")` and add a `retrieval` block
(`mode`, `degraded`, `retrieved_ids`) collected from `tool_result` events, then re-run a
single case. That turns "the guard did not fire" into either "the model cited 3 valid
ids and the guard correctly stayed quiet" or "the model cited nothing and the guard is
decorative". To force a real denial deterministically after CR-01 is fixed, add an
eval-only flag that seeds `run_ticket`'s `retrieved_ids` with one dummy id and drops the
first real `search_docs` id — one case, one denial, one recovery, measured.

---

## Info

### IN-01: `score` is not monotonic across the hybrid result list

**File:** `src/relay/retrieval.py:178-181`
The keyword-only tail is *ordered* by keyword count but *reported* with cosine scores,
so within the tail `results[i]["score"] < results[i+1]["score"]` is possible. Any
consumer that re-sorts by `score` (a plausible Phase 5 dashboard behaviour) will reorder
the list. Either sort the tail by cosine too, or emit `rank` alongside `score`.

### IN-02: Dead branch in `_locate_heading`

**File:** `src/relay/retrieval.py:266-268`
`return doc.headings[0] if doc.headings else None` — `sections` and `doc.headings` are
produced by the same `_HEADING_RE` over the same text, so `not sections` implies
`not doc.headings` for any index `build_index.py` produced. The `doc.headings[0]` half is
unreachable. Relevant to self-reported item #4: I *disagree* that
`test_citation_id_falls_back_to_the_bare_doc_when_there_are_no_headings` is the weakest
test — it covers a real, reachable branch (`sections` empty) and fails if the `id`
fallback is removed. The genuinely weakest artefacts in the phase are the hardcoded
`floor=0.55` in every retrieval test (WR-02) and `test_search_docs_stays_synchronous`,
which duplicates `tests/test_lifecycle.py`'s registry-wide sweep.

### IN-03: The `_FROM_SETTINGS` sentinel makes the `retrieve` signature lie

**File:** `src/relay/retrieval.py:48,137-138`
`key: str | None = _FROM_SETTINGS` and `floor: float = _FROM_SETTINGS` annotate types
that exclude the actual default. A reader (or a type checker) cannot tell that
`floor=None` is illegal. A small `@dataclass(frozen=True) class _FromSettings` with a
`Final` instance, or two overloads, would keep the ergonomics and the honesty.

### IN-04: `retrieval_floor` and `voyage_dim` have no bounds validation

**File:** `src/relay/config.py:104-106`
`RELAY_RETRIEVAL_FLOOR=5` makes semantic ranking silently inert — the exact failure the
0.55 placeholder caused, which 03-06 had to catch by measurement. Add
`Field(ge=-1.0, le=1.0)` and `Field(gt=0)` respectively; a cosine floor outside [-1, 1]
is a configuration error, not a tuning choice.

### IN-05: `send_reply`'s `citations` argument is accepted and discarded

**File:** `src/relay/tools.py:78-91`
Documented as intentional (03-04), but the consequence is that the model's grounding
claim exists only transiently inside `_execute_guarded` — Phase 5's dashboard trace and
Phase 6 will have no server-side record of what a reply cited. Worth an explicit
deferred-debt entry rather than only an inline comment.

### IN-06: Seven lines exceed the configured 100-char limit

**File:** `tests/test_retrieval.py:141` (103), `tests/test_guardrails.py:93` (104), plus
five in `scripts/build_index.py`
`[tool.ruff] line-length = 100` is set but ruff's default rule set does not include
`E501`, so the project's stated style rule is unenforced. Add `select = ["E4","E7","E9","F","E501"]`
(or `extend-select = ["E501"]`) to `pyproject.toml` and fix the offenders.

### IN-07: `build_index.main` dereferences `__doc__` unconditionally

**File:** `scripts/build_index.py:159`
`__doc__.splitlines()[0]` raises `AttributeError` under `python -OO`, which strips
docstrings. Use `(__doc__ or "").splitlines()[0] if __doc__ else "build kb/index.json"`.

### IN-08: The builder echoes the raw Voyage error body into an exception message

**File:** `scripts/build_index.py:84-87`
`exc.response.text` is the only place an upstream response body is surfaced. The key
travels in a header so an echo is unlikely, but this is maintainer-facing output printed
to stderr from a script that runs with a live credential in the environment — truncate
it (`exc.response.text[:500]`) on principle.

---

## Verified as Correct

These were probed adversarially and hold up:

- **No Voyage failure can raise into a run.** Timeouts, `ConnectError`,
  `HTTPStatusError`, malformed/non-JSON bodies, empty `data`, missing keys, ragged or
  wrong-width embeddings, and non-numeric values are all caught by `_embed_query`'s
  boundary (`retrieval.py:217`), and index-load failures by
  `(OSError, ValueError, KeyError, TypeError)` (`retrieval.py:124`). Verified by
  construction across every branch. `BaseException` classes (`KeyboardInterrupt`,
  `SystemExit`) are correctly not swallowed, and `CancelledError` cannot arrive here
  because the call runs in a `to_thread` worker. The only hole is WR-01, which also does
  not raise — it degrades silently.
- **The API key is never loggable.** It leaves the process only as an `Authorization`
  header (`retrieval.py:203`, `build_index.py:71`); `retrieval.voyage_failed` logs
  `f"{type(exc).__name__}: {exc}"` and httpx exception strings carry the URL, never the
  request headers. It is not a span attribute, not in a query string, not in the
  `notice` payload, and not in the citation `guardrail` payload.
  `test_voyage_failure_never_logs_the_api_key` is real coverage, not a tautology.
- **`retrieve()` never returns a chunk, and `text` is never truncated.**
  `_result` passes `doc.text` through unmodified; the builder stores whole files
  (`build_index.py:120-129`); `test_results_return_whole_files_never_chunks` compares
  bytes against `kb/billing.md` on disk; and
  `test_committed_index_has_one_full_width_embedding_per_doc` asserts the committed
  artifact's `text` equals the on-disk file. D-01/D-02 hold on every path I traced.
- **`input_type` is correct on both sides and asserted off the wire.**
  `"document"` at build time, `"query"` at search time, both asserted from the
  *intercepted request body* rather than from the `meta` literal — the one construction
  that can actually detect a swap. The best-designed test pair in the phase.
- **The committed `kb/index.json` is real.** Three 512-float rows, all unit-norm,
  pairwise cosines 0.507/0.514/0.549 — genuine, distinct embeddings, not zeros or
  duplicates. `meta` matches settings and the current KB hash.
- **The citation subset check cannot be *bypassed*.** Exact matching means it fails
  closed on case and whitespace drift (which is CR-01's problem, not a bypass); the empty
  string is denied; duplicates are all reported; a bare string is rejected by
  Pydantic's `list[str]`; an absent `citations` validates to `[]`, which is D-12's
  intended pass. I found no input that slips a fabricated id through.
- **No cross-run citation leakage.** `retrieved_ids` is created inside `run_ticket`
  (`agent.py:201`), captured per-run by `bind_to_ticket`, never touched on the shared
  registry, and mutated only on the event loop after `to_thread` returns
  (`agent.py:302-308`) — so no lock is needed and the shared-mutable-state pitfall does
  not apply. The reasoning in the summary is accurate.
- **Neither the `notice` event nor the citation guardrail event leaks doc text or PII.**
  `notice` carries `{kind, tool, retrieval_mode, results}` where `results` is a count;
  the guardrail carries ids only. The matching log lines carry the same fields.
- **`notice` rides the existing SSE contract without change.** `main.py:234` serialises
  `f"event: {event.type}..."` generically, and `evals.extract_outcome` has no `else`
  branch, so the new type is additive and backward compatible as claimed.
- **Voyage query spend is bounded, not a hole (self-reported item #3, confirmed).**
  `max_agent_steps = 10` × `owner_process_limit = 60/hour` ⇒ ≤ 600 query embeddings/hour,
  each ≤ 500 chars (`SearchDocsInput.query` cap) ≈ 125 tokens — under 100 k tokens/hour
  against a 200 M free tier, and the demo tier is 5/hour. The out-of-`RunBudget`
  disposition (T-03-17) is defensible. Worth noting only that a `dry_run=true` request
  still pays Voyage, since `search_docs` is read-tier and ungated by `ToolPolicy` — an
  acceptable but undocumented consequence.
- **Index is loaded once, not per call.** `build_registry` (`tools.py:104`) loads it and
  the closure captures it; `test_search_docs_reads_the_index_once_not_per_call` counts
  real invocations and would fail at 0 or 3.
- **`search_docs` stayed synchronous.** No coroutine in `retrieval.py`, so the
  `ToolSpec.execute: Callable[..., str]` contract and the Phase 2 `to_thread` seam are
  both intact, and the MCP sync path keeps working.
- **Back-compat held.** `src/relay/mcp_server.py` and `src/relay/evals.py` have zero diff
  across the phase; the unbound MCP path correctly skips the citation check
  (`retrieved_ids=None`) while an empty set correctly denies; 195 tests pass and
  `ruff check src tests` is clean.

---

_Reviewed: 2026-08-10_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_

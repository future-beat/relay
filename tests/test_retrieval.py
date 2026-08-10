"""Retrieval unit tests — zero real Voyage calls.

Every test either builds a synthetic Index in memory or writes a temp index.json,
and the autouse `_no_network` fixture makes an unmocked httpx.post fail loudly, so
the suite is green with VOYAGE_API_KEY unset (the keyword baseline, D-10).
"""

import json
import re
from pathlib import Path

import httpx
import numpy as np
import pytest

from relay import retrieval
from relay.config import settings
from relay.retrieval import Doc, Index, headings, kb_sha256, load_index, retrieve, slug

KB_DIR = Path(__file__).parent.parent / "kb"
DIM = 512


def _basis(i: int) -> np.ndarray:
    """Orthogonal unit vector: cosine 1.0 against doc i, 0.0 against every other."""
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("a test attempted a real HTTP call")

    monkeypatch.setattr(httpx, "post", _forbidden)


@pytest.fixture()
def voyage(monkeypatch):
    """Install a fake Voyage endpoint; returns the list of captured call kwargs."""
    calls: list[dict] = []

    def _install(vector=None, *, error=None, payload=None):
        def _fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            body = payload
            if body is None:
                body = {"data": [{"embedding": [float(x) for x in vector], "index": 0}]}
            return _FakeResponse(body)

        monkeypatch.setattr(httpx, "post", _fake_post)
        return calls

    return _install


@pytest.fixture()
def kb_docs() -> list[Doc]:
    docs = []
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        docs.append(Doc(doc=path.name, headings=headings(text), text=text))
    return docs


@pytest.fixture()
def index(kb_docs) -> Index:
    matrix = np.stack([_basis(i) for i in range(len(kb_docs))])
    return Index(docs=kb_docs, matrix=matrix, model=settings.voyage_model, dim=DIM)


def _doc_position(kb_docs: list[Doc], name: str) -> int:
    return [d.doc for d in kb_docs].index(name)


def test_semantic_ranking_puts_the_on_topic_doc_first_and_drops_below_floor(
    index, kb_docs, voyage
):
    voyage(_basis(_doc_position(kb_docs, "billing.md")))
    results, mode, degraded, cause = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "semantic"
    assert (degraded, cause) == (False, None)
    # account.md and api.md score 0.0 against this query vector and match no keyword.
    assert [r["doc"] for r in results] == ["billing.md"]
    assert results[0]["score"] == pytest.approx(1.0)


def test_off_topic_query_below_the_floor_returns_empty_results(index, voyage):
    # Uniform vector: cosine 1/sqrt(512) ~= 0.044 against every doc, far below the floor.
    voyage(np.ones(DIM, dtype=np.float32))
    results, mode, degraded, cause = retrieve(index, "zzzzz qqqqq", key="test-key", floor=0.55)
    assert results == []
    assert mode == "semantic"
    assert (degraded, cause) == (False, None)


def test_voyage_failure_degrades_to_keyword_and_never_raises(index, voyage):
    calls = voyage(error=httpx.ConnectError("voyage unreachable"))
    results, mode, degraded, cause = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "keyword"
    assert (degraded, cause) == (True, "voyage_failed")
    assert [r["doc"] for r in results] == ["billing.md"]
    assert len(calls) == 2, "one timeout-bounded attempt plus one manual retry"


def test_missing_key_is_the_keyword_baseline_not_a_degradation(index, monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    results, mode, degraded, cause = retrieve(index, "refund policy", floor=0.55)
    assert mode == "keyword"
    assert (degraded, cause) == (False, None), "no key is the baseline, not a failure"
    assert [r["doc"] for r in results] == ["billing.md"]


def test_malformed_voyage_response_degrades_instead_of_ranking_on_garbage(index, voyage):
    # Right envelope, wrong width — the dimension drift RESEARCH pitfall 6 describes.
    voyage(payload={"data": [{"embedding": [0.1] * 16, "index": 0}]})
    results, mode, degraded, cause = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "keyword"
    assert (degraded, cause) == (True, "voyage_failed")
    assert [r["doc"] for r in results] == ["billing.md"]


def test_voyage_failure_never_logs_the_api_key(index, voyage, caplog):
    voyage(error=httpx.ConnectError("voyage unreachable"))
    with caplog.at_level("WARNING", logger="relay.retrieval"):
        retrieve(index, "refund policy", key="sk-voyage-super-secret", floor=0.55)
    logged = "\n".join(
        [r.getMessage() for r in caplog.records] + [str(getattr(r, "ctx", "")) for r in caplog.records]
    )
    assert "sk-voyage-super-secret" not in logged
    assert "retrieval.voyage_failed" in logged


def test_hybrid_union_keeps_a_keyword_only_hit_the_embedding_missed(index, kb_docs, voyage):
    # Query vector points at account.md; "webhook" is a keyword hit in api.md only.
    voyage(_basis(_doc_position(kb_docs, "account.md")))
    results, mode, _, _ = retrieve(index, "webhook retry", key="test-key", floor=0.55)
    docs = [r["doc"] for r in results]
    assert mode == "semantic"
    assert docs[0] == "account.md", "the above-floor semantic hit still ranks first"
    assert "api.md" in docs, "the keyword-only hit must survive the union (D-05)"


def test_result_carries_the_citation_id_shape(index, kb_docs, voyage):
    voyage(_basis(_doc_position(kb_docs, "billing.md")))
    results, _, _, _ = retrieve(index, "refund policy", key="test-key", floor=0.55)
    result = results[0]
    assert set(result) == {"doc", "heading", "id", "anchors", "text", "score"}
    assert result["id"] == f"{result['doc']}#{slug(result['heading'])}"
    assert result["id"] == "billing.md#refunds"
    assert re.match(r"^[^#]+\.md#", result["id"])


def test_a_result_carries_every_anchor_of_the_whole_file_it_returns(index, kb_docs, voyage):
    # The model is handed the entire file, so the citation guard's accept-set has to be
    # every heading in it — not just the one the query-driven locator picked. A result
    # that only advertises `id` makes the accurate anchor uncitable (the guard then
    # denies `billing.md#upgrades-and-downgrades` for "upgrade my plan").
    voyage(_basis(_doc_position(kb_docs, "billing.md")))
    results, _, _, _ = retrieve(index, "upgrade my plan", key="test-key", floor=0.55)
    billing = next(r for r in results if r["doc"] == "billing.md")
    assert billing["anchors"] == [
        "billing.md",
        "billing.md#billing-and-plans",
        "billing.md#refunds",
        "billing.md#upgrades-and-downgrades",
    ]
    assert billing["id"] in billing["anchors"]
    # Only this doc's ids: a doc that was not returned contributes nothing to cite.
    assert all(a.split("#")[0] == "billing.md" for a in billing["anchors"])


def test_a_doc_without_headings_still_anchors_its_bare_name(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    # The word repeats so the doc clears KEYWORD_MIN_SCORE — this test is about the
    # anchor/id fallback for a heading-less doc, not about the keyword gate.
    plain = Doc(doc="plain.md", headings=[], text="no headings; headings are absent here")
    index = Index(docs=[plain], matrix=None, model=settings.voyage_model, dim=DIM)
    results, _, _, _ = retrieve(index, "headings", floor=0.55)
    assert results[0]["anchors"] == ["plain.md"]


def test_citation_id_falls_back_to_the_bare_doc_when_there_are_no_headings(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    # The word repeats so the doc clears KEYWORD_MIN_SCORE — this test is about the
    # anchor/id fallback for a heading-less doc, not about the keyword gate.
    plain = Doc(doc="plain.md", headings=[], text="no headings; headings are absent here")
    index = Index(docs=[plain], matrix=None, model=settings.voyage_model, dim=DIM)
    results, _, _, _ = retrieve(index, "headings", floor=0.55)
    assert results[0]["heading"] is None
    assert results[0]["id"] == "plain.md"


def test_query_path_sends_input_type_query(index, kb_docs, voyage):
    calls = voyage(_basis(_doc_position(kb_docs, "billing.md")))
    retrieve(index, "refund policy", key="test-key", floor=0.55)
    body = calls[0]["json"]
    # "document" here would silently cost recall rather than fail anything (D-09).
    assert body["input_type"] == "query"
    assert body["input"] == ["refund policy"]
    assert body["model"] == settings.voyage_model
    assert body["output_dimension"] == DIM
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["timeout"] == 10.0


# --- the keyword half's own gate (CR-04) ----------------------------------------------

# Every one of these is a way a real model might ask about something the KB does not
# cover. Under the ungated scorer, the two-word topical forms returned `[]` and the
# natural-sentence forms returned the ENTIRE knowledge base as citable grounding —
# so whether the agent escalated (D-03/D-04) came down to how it happened to phrase
# itself, not to whether the answer existed.
UNCOVERED_QUERIES = [
    "Salesforce integration",
    "Salesforce CRM sync",
    "integrate with Salesforce",
    "Do you integrate with Salesforce for our CRM pipeline?",
    "Can you tell me if there is a Salesforce connector",
    "is Salesforce supported",
    "any plan to support Salesforce?",
    "we would like to sync tickets to Salesforce",
    "third-party CRM integrations roadmap",
    "Does the product work on Mars?",
    "what about Mars colonization support",
    "kubernetes cluster autoscaling",
    "how do I bake sourdough bread",
]

# (query, the doc that must still come back). Model-style phrasings from 03-06's
# measured table. These ride on the keyword half alone here — no key, no vectors —
# which is what the calibration assumed when it put the floor above `uptime SLA
# guarantee` (0.2659) and left that case to this side of the union.
COVERED_QUERIES = [
    ("uptime SLA guarantee", "billing.md"),
    ("what is your uptime SLA guarantee for Enterprise", "billing.md"),
    ("export data", "account.md"),
    ("API rate limits Pro plan", "api.md"),
    ("password reset", "account.md"),
    ("webhooks availability plan", "api.md"),
    ("two-factor authentication lost recovery codes lockout", "account.md"),
    ("refund policy billing charge", "billing.md"),
    ("API key suspended", "api.md"),
    ("downgrade plan data retention projects", "billing.md"),
    ("SAML SSO configuration", "account.md"),
    ("refund policy", "billing.md"),
    ("webhook retry", "api.md"),
]


@pytest.fixture()
def keyword_index(kb_docs) -> Index:
    """The real KB with no vectors — the keyword half of the union, isolated."""
    return Index(docs=kb_docs, matrix=None, model=settings.voyage_model, dim=DIM)


@pytest.mark.parametrize("query", UNCOVERED_QUERIES)
def test_an_uncovered_ask_returns_nothing_however_it_is_phrased(keyword_index, query):
    results, _, _, _ = retrieve(keyword_index, query, key=None)
    assert results == [], f"{query!r} returned {[r['doc'] for r in results]} to cite"


@pytest.mark.parametrize(("query", "expected"), COVERED_QUERIES)
def test_a_covered_ask_still_reaches_its_doc_through_the_keyword_half(
    keyword_index, query, expected
):
    # The other direction of the same gate. A filter that only ever returns [] would
    # pass the test above and destroy retrieval; 03-06's floor was calibrated on the
    # assumption that these keep arriving.
    results, _, _, _ = retrieve(keyword_index, query, key=None)
    assert expected in [r["doc"] for r in results], f"{query!r} lost {expected}"


def test_the_whole_ticket_body_form_of_an_uncovered_ask_returns_nothing_either(
    keyword_index,
):
    """03-06 recorded this as the residual risk the semantic floor could not cover.

    If the model passes the ticket text instead of a topical phrase, the old scorer
    matched every doc on its phrasing words (account:6, billing:4, api:3) and the
    union returned them regardless of the floor. `salesforce-integration` is the
    golden case whose escalation depended on it.
    """
    body = (
        "Salesforce integration? Does Lanekeep integrate with Salesforce? "
        "We need two-way sync of tasks and contacts for our sales team."
    )
    results, _, _, _ = retrieve(keyword_index, body, key=None)
    assert results == []


def _two_doc_index(first: str, second: str = "unrelated words entirely") -> Index:
    """Two docs so IDF is meaningful — a term in one of them is discriminating."""
    return Index(
        docs=[
            Doc(doc="a.md", headings=[], text=first),
            Doc(doc="b.md", headings=[], text=second),
        ],
        matrix=None,
        model=settings.voyage_model,
        dim=DIM,
    )


def test_a_term_cannot_score_inside_a_longer_word():
    """The word-START anchor, isolated.

    Unanchored, `port` counts four times inside `support` and clears the gate on IDF
    alone. This is the same mechanism that let `and` score inside `standard` — and
    IDF does not save you here, because the term is concentrated in one doc rather
    than spread across the corpus.
    """
    index = _two_doc_index("support support support support")
    assert retrieve(index, "port", key=None)[0] == []
    # The anchor is a prefix, not a whole word: `refund` must keep matching `refunds`,
    # which the hybrid union depends on.
    assert [r["doc"] for r in retrieve(_two_doc_index("refunds " * 4), "refund", key=None)[0]] == [
        "a.md"
    ]


def test_stopwords_are_dropped_even_where_idf_would_not_catch_them():
    """The stopword list, isolated.

    On the committed KB `the` sits in every doc and IDF already zeroes it, which
    hides the fact that this gate does anything. Concentrate it in one doc and it
    becomes a perfectly discriminating term scoring 3.47 — a query made only of
    phrasing words would return a doc to cite.
    """
    index = _two_doc_index("the the the the the")
    assert retrieve(index, "the", key=None)[0] == []
    assert retrieve(index, "what does this have to do with that", key=None)[0] == []


def test_the_keyword_gate_sits_between_the_measured_bands(keyword_index):
    """CR-04's floor is a measurement, not a knob — moving it re-opens a real bug.

    Measured on the committed KB: the off-topic band tops out at 1.10 (the
    salesforce-integration ticket body) and the covered band bottoms at 2.197
    (`uptime SLA guarantee` on billing.md, the query 03-06 deliberately left to this
    half of the union). Below 1.11 the escalation signal leaks; above 2.19 the
    calibrated floor starves its one dependent case.
    """
    assert 1.11 <= retrieval.KEYWORD_MIN_SCORE <= 2.19
    scores = {
        r["doc"]: r["score"]
        for r in retrieve(keyword_index, "uptime SLA guarantee", key=None)[0]
    }
    assert scores["billing.md"] == pytest.approx(2.197, abs=0.01)


# --- kb_sha256 and the index artifact -------------------------------------------------


def _write_kb(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for path in sorted(KB_DIR.glob("*.md")):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    return tmp_path


def _write_index(tmp_path: Path, *, model=None, dim=DIM, kb_hash=None) -> Path:
    _write_kb(tmp_path)
    docs = []
    for i, path in enumerate(sorted(tmp_path.glob("*.md"))):
        text = path.read_text(encoding="utf-8")
        docs.append(
            {
                "doc": path.name,
                "headings": headings(text),
                "text": text,
                "embedding": [float(x) for x in _basis(i)],
            }
        )
    meta = {
        "model": model or settings.voyage_model,
        "output_dimension": dim,
        "input_type_document": "document",
        "kb_sha256": kb_hash or kb_sha256(tmp_path),
    }
    (tmp_path / "index.json").write_text(json.dumps({"meta": meta, "docs": docs}))
    return tmp_path


def test_kb_sha256_is_stable_hex_over_the_markdown_only(tmp_path):
    kb = _write_index(tmp_path)
    digest = kb_sha256(kb)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == kb_sha256(kb), "re-hashing the same KB must be stable"
    assert digest == kb_sha256(KB_DIR), "index.json is not *.md, so it must not be hashed"
    (kb / "billing.md").write_text("changed")
    assert kb_sha256(kb) != digest


def test_kb_sha256_detects_edits_that_leave_the_raw_bytes_identical(tmp_path):
    """RAG-02's gate is only as good as the digest. These three edits are ordinary.

    A digest over concatenated file bytes is blind to file boundaries and to names, so
    all three used to hash identically to the untouched KB: `load_index` kept the old
    vectors, the new file was invisible to semantic ranking, and CI stayed green.
    """
    original = kb_sha256(_write_kb(tmp_path / "original"))

    split = _write_kb(tmp_path / "split")
    body = (split / "billing.md").read_bytes()
    (split / "billing.md").write_bytes(body[: len(body) // 2])
    (split / "billing2.md").write_bytes(body[len(body) // 2 :])
    assert kb_sha256(split) != original, "splitting a doc in two must change the hash"

    renamed = _write_kb(tmp_path / "renamed")
    (renamed / "api.md").rename(renamed / "apiz.md")  # sort order preserved
    assert kb_sha256(renamed) != original, "renaming a doc must change the hash"

    added = _write_kb(tmp_path / "added")
    (added / "zzz.md").write_bytes(b"")
    assert kb_sha256(added) != original, "adding an empty doc must change the hash"

    moved = _write_kb(tmp_path / "moved")
    tail = (moved / "api.md").read_bytes()
    (moved / "api.md").write_bytes(tail[:-40])
    (moved / "billing.md").write_bytes(tail[-40:] + (moved / "billing.md").read_bytes())
    assert kb_sha256(moved) != original, (
        "moving a trailing paragraph between two docs must change the hash"
    )


def test_load_index_reads_the_committed_matrix(tmp_path):
    index = load_index(_write_index(tmp_path))
    assert index.matrix is not None
    assert index.matrix.shape == (3, DIM)
    assert np.allclose(np.linalg.norm(index.matrix, axis=1), 1.0)
    assert [d.doc for d in index.docs] == ["account.md", "api.md", "billing.md"]


def test_load_index_missing_file_falls_back_to_keyword_mode(tmp_path):
    index = load_index(_write_kb(tmp_path))
    assert index.matrix is None
    assert [d.doc for d in index.docs] == ["account.md", "api.md", "billing.md"]
    results, mode, degraded, cause = retrieve(index, "refund policy", key=None, floor=0.55)
    # key=None: this is the intended keyword baseline, so it is NOT a degradation.
    # With a key set it is — see the test immediately below, which is the case this
    # one used to assert backwards.
    assert (mode, degraded, cause) == ("keyword", False, None)
    assert [r["doc"] for r in results] == ["billing.md"]


def test_a_key_with_no_usable_index_is_a_degradation_not_the_baseline(tmp_path):
    """CR-03: the failure mode that actually reaches production.

    A deployment configured for semantic retrieval whose artifact is missing or stale
    serves keyword-quality results forever. Before this, `degraded` could only become
    True inside the `matrix is not None` branch, so the stream, the dashboard and
    /metrics were byte-identical to the deliberate keyless baseline and no operator
    could tell "working as designed" from "broken". The boot-time WARNING is not a
    substitute: it fires once, into a log nobody is reading at 3am.
    """
    index = load_index(_write_kb(tmp_path))
    assert index.matrix is None
    results, mode, degraded, cause = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert (mode, degraded, cause) == ("keyword", True, "index_unavailable")
    # Degraded, not dead: the keyword half still answers, the run still resolves.
    assert [r["doc"] for r in results] == ["billing.md"]


def test_the_reason_the_index_is_unusable_survives_to_the_query_path(tmp_path, caplog):
    # "Something is wrong with retrieval" is not actionable; "the kb changed without a
    # rebuild" is. load_index is the only thing that knows why, and it runs at startup.
    index = load_index(_write_index(tmp_path, kb_hash="0" * 64))
    assert "without rebuilding" in (index.unavailable_reason or "")
    with caplog.at_level("WARNING", logger="relay.retrieval"):
        retrieve(index, "refund policy", key="test-key", floor=0.55)
    ctx = [getattr(r, "ctx", {}) for r in caplog.records]
    assert any(c.get("cause") == "index_unavailable" for c in ctx)
    assert any("without rebuilding" in str(c.get("reason", "")) for c in ctx)


def test_load_index_hash_mismatch_falls_back_to_keyword_mode(tmp_path):
    index = load_index(_write_index(tmp_path, kb_hash="0" * 64))
    assert index.matrix is None


def test_load_index_model_mismatch_falls_back_to_keyword_mode(tmp_path):
    assert load_index(_write_index(tmp_path, model="voyage-2")).matrix is None


def test_load_index_dimension_mismatch_falls_back_to_keyword_mode(tmp_path):
    assert load_index(_write_index(tmp_path, dim=1024)).matrix is None


def test_load_index_malformed_json_falls_back_without_raising(tmp_path):
    _write_kb(tmp_path)
    (tmp_path / "index.json").write_text("{not json")
    assert load_index(tmp_path).matrix is None


def test_results_return_whole_files_never_chunks(tmp_path, voyage):
    """D-02: the tool's output stays byte-compatible with the keyword scorer's."""
    index = load_index(_write_index(tmp_path))
    voyage(_basis([d.doc for d in index.docs].index("billing.md")))
    results, _, _, _ = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert results[0]["text"].encode("utf-8") == (KB_DIR / "billing.md").read_bytes()

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
    results, mode, degraded = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "semantic"
    assert degraded is False
    # account.md and api.md score 0.0 against this query vector and match no keyword.
    assert [r["doc"] for r in results] == ["billing.md"]
    assert results[0]["score"] == pytest.approx(1.0)


def test_off_topic_query_below_the_floor_returns_empty_results(index, voyage):
    # Uniform vector: cosine 1/sqrt(512) ~= 0.044 against every doc, far below the floor.
    voyage(np.ones(DIM, dtype=np.float32))
    results, mode, degraded = retrieve(index, "zzzzz qqqqq", key="test-key", floor=0.55)
    assert results == []
    assert mode == "semantic"
    assert degraded is False


def test_voyage_failure_degrades_to_keyword_and_never_raises(index, voyage):
    calls = voyage(error=httpx.ConnectError("voyage unreachable"))
    results, mode, degraded = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "keyword"
    assert degraded is True
    assert [r["doc"] for r in results] == ["billing.md"]
    assert len(calls) == 2, "one timeout-bounded attempt plus one manual retry"


def test_missing_key_is_the_keyword_baseline_not_a_degradation(index, monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    results, mode, degraded = retrieve(index, "refund policy", floor=0.55)
    assert mode == "keyword"
    assert degraded is False
    assert [r["doc"] for r in results] == ["billing.md"]


def test_malformed_voyage_response_degrades_instead_of_ranking_on_garbage(index, voyage):
    # Right envelope, wrong width — the dimension drift RESEARCH pitfall 6 describes.
    voyage(payload={"data": [{"embedding": [0.1] * 16, "index": 0}]})
    results, mode, degraded = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert mode == "keyword"
    assert degraded is True
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
    results, mode, _ = retrieve(index, "webhook retry", key="test-key", floor=0.55)
    docs = [r["doc"] for r in results]
    assert mode == "semantic"
    assert docs[0] == "account.md", "the above-floor semantic hit still ranks first"
    assert "api.md" in docs, "the keyword-only hit must survive the union (D-05)"


def test_result_carries_the_citation_id_shape(index, kb_docs, voyage):
    voyage(_basis(_doc_position(kb_docs, "billing.md")))
    results, _, _ = retrieve(index, "refund policy", key="test-key", floor=0.55)
    result = results[0]
    assert set(result) == {"doc", "heading", "id", "text", "score"}
    assert result["id"] == f"{result['doc']}#{slug(result['heading'])}"
    assert result["id"] == "billing.md#refunds"
    assert re.match(r"^[^#]+\.md#", result["id"])


def test_citation_id_falls_back_to_the_bare_doc_when_there_are_no_headings(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    plain = Doc(doc="plain.md", headings=[], text="no headings anywhere in this file")
    index = Index(docs=[plain], matrix=None, model=settings.voyage_model, dim=DIM)
    results, _, _ = retrieve(index, "headings", floor=0.55)
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


# --- kb_sha256 and the index artifact -------------------------------------------------


def _write_kb(tmp_path: Path) -> Path:
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
    results, mode, degraded = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert (mode, degraded) == ("keyword", False)
    assert [r["doc"] for r in results] == ["billing.md"]


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
    results, _, _ = retrieve(index, "refund policy", key="test-key", floor=0.55)
    assert results[0]["text"].encode("utf-8") == (KB_DIR / "billing.md").read_bytes()

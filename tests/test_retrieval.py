"""Retrieval unit tests — zero real Voyage calls.

Every test either builds a synthetic Index in memory or writes a temp index.json,
and the autouse `_no_network` fixture makes an unmocked httpx.post fail loudly, so
the suite is green with VOYAGE_API_KEY unset (the keyword baseline, D-10).
"""

from pathlib import Path

import httpx
import numpy as np
import pytest
from relay.retrieval import Doc, Index, headings, retrieve

from relay.config import settings

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

"""The committed index artifact and the builder that writes it — zero real Voyage calls.

Two jobs:

* `test_index_matches_kb` is the CI staleness gate (RAG-02). It recomputes the hash with
  the SAME `kb_sha256` the builder stamps with, so editing `kb/*.md` without rebuilding
  fails the existing `pytest -q` step. It deliberately does NOT skip when `kb/index.json`
  is absent — a missing artifact is exactly the failure the gate exists to catch.
* `test_build_index_embeds_with_input_type_document` closes D-09's silent-recall trap on
  the index side. It asserts the value off the *intercepted request body*, because the
  `meta["input_type_document"]` the script writes comes from the same literal it sends:
  reading it back would match itself even if the request said "query".

The autouse `_no_network` fixture makes any unmocked httpx.post fail loudly, so the whole
module is green with VOYAGE_API_KEY unset.
"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from relay.config import settings
from relay.retrieval import kb_sha256

REPO_ROOT = Path(__file__).parent.parent
KB_DIR = REPO_ROOT / "kb"
INDEX_PATH = KB_DIR / "index.json"
BUILDER_PATH = REPO_ROOT / "scripts" / "build_index.py"
DIM = 512


def _load_builder():
    """Import scripts/build_index.py by path — `scripts/` is not an installed package."""
    spec = importlib.util.spec_from_file_location("build_index", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("a test attempted a real HTTP call")

    monkeypatch.setattr(httpx, "post", _forbidden)


@pytest.fixture()
def builder():
    return _load_builder()


@pytest.fixture()
def voyage(monkeypatch):
    """Fake the Voyage endpoint; returns the list of captured requests."""
    calls: list[dict] = []

    def _install(*, dim: int = DIM, response: _FakeResponse | None = None, error=None):
        def _fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            if response is not None:
                return response
            inputs = kwargs["json"]["input"]
            return _FakeResponse(
                {
                    "data": [
                        {"index": i, "embedding": [0.0] * (dim - 1) + [1.0]}
                        for i in range(len(inputs))
                    ]
                }
            )

        monkeypatch.setattr(httpx, "post", _fake_post)
        return calls

    return _install


@pytest.fixture()
def key(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", "test-key-never-sent-anywhere")
    return settings.voyage_api_key


# --- the staleness gate (RAG-02) -------------------------------------------------


def test_index_matches_kb():
    assert INDEX_PATH.exists(), (
        f"{INDEX_PATH} is missing — run `VOYAGE_API_KEY=... python scripts/build_index.py`"
    )
    meta = json.loads(INDEX_PATH.read_text(encoding="utf-8"))["meta"]
    assert meta["kb_sha256"] == kb_sha256(KB_DIR), (
        "kb/*.md changed without rebuilding kb/index.json — run scripts/build_index.py"
    )
    assert meta["model"] == settings.voyage_model
    assert int(meta["output_dimension"]) == settings.voyage_dim


def test_committed_index_has_one_full_width_embedding_per_doc():
    assert INDEX_PATH.exists(), (
        f"{INDEX_PATH} is missing — run `VOYAGE_API_KEY=... python scripts/build_index.py`"
    )
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    names = [d["doc"] for d in raw["docs"]]
    assert names == sorted(p.name for p in KB_DIR.glob("*.md"))
    for doc in raw["docs"]:
        assert len(doc["embedding"]) == settings.voyage_dim
        assert doc["text"] == (KB_DIR / doc["doc"]).read_text(encoding="utf-8")


# --- the builder (D-09 index side) -----------------------------------------------


def test_build_index_embeds_with_input_type_document(builder, voyage, key):
    """The blocker this plan closes: assert the REQUEST, not the meta literal.

    `meta["input_type_document"]` is written from the same constant the request uses,
    so reading it back proves nothing — a swapped literal matches itself. Only the
    intercepted body can tell a build-time "document" from a build-time "query".
    """
    calls = voyage()

    builder.build(KB_DIR, key=key, model=settings.voyage_model, dim=settings.voyage_dim)

    assert len(calls) == 1, "the whole KB should be embedded in one request"
    body = calls[0]["json"]
    assert body["input_type"] == "document", (
        "the index-time embed request must carry input_type='document' (D-09); "
        f"it carried {body['input_type']!r} — recall would degrade silently"
    )
    assert calls[0]["url"] == "https://api.voyageai.com/v1/embeddings"
    assert body["model"] == settings.voyage_model
    assert body["output_dimension"] == settings.voyage_dim
    assert body["input"] == [
        p.read_text(encoding="utf-8") for p in sorted(KB_DIR.glob("*.md"))
    ]


def test_build_index_stamps_meta_from_the_shared_hash_function(builder, voyage, key):
    voyage()
    index = builder.build(KB_DIR, key=key, model=settings.voyage_model, dim=settings.voyage_dim)

    assert index["meta"]["kb_sha256"] == kb_sha256(KB_DIR)
    assert index["meta"]["model"] == settings.voyage_model
    assert index["meta"]["output_dimension"] == settings.voyage_dim
    assert index["meta"]["input_type_document"] == "document"
    assert [d["doc"] for d in index["docs"]] == sorted(p.name for p in KB_DIR.glob("*.md"))
    assert all(len(d["embedding"]) == settings.voyage_dim for d in index["docs"])


def test_build_index_without_a_key_fails_loudly(builder, monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    with pytest.raises(builder.BuildError, match="VOYAGE_API_KEY is unset"):
        builder.resolve_key()


def test_main_without_a_key_writes_nothing(builder, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "voyage_api_key", None)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    (tmp_path / "billing.md").write_text("# Refunds\nrefunds within 30 days\n")

    assert builder.main(["--kb", str(tmp_path)]) == 1
    assert not (tmp_path / "index.json").exists()


def test_main_writes_the_artifact_next_to_the_kb(builder, voyage, key, tmp_path):
    voyage()
    (tmp_path / "billing.md").write_text("# Refunds\nrefunds within 30 days\n")

    assert builder.main(["--kb", str(tmp_path)]) == 0

    written = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert written["meta"]["kb_sha256"] == kb_sha256(tmp_path)
    assert written["docs"][0]["doc"] == "billing.md"
    assert written["docs"][0]["headings"] == ["Refunds"]


def test_check_mode_detects_a_stale_artifact(builder, voyage, key, tmp_path):
    voyage()
    (tmp_path / "billing.md").write_text("# Refunds\nrefunds within 30 days\n")
    builder.main(["--kb", str(tmp_path)])
    assert builder.check(tmp_path) is None

    (tmp_path / "billing.md").write_text("# Refunds\nrefunds within 14 days\n")
    assert "stale" in builder.check(tmp_path)


def test_build_index_rejects_a_wrong_width_embedding(builder, voyage, key, tmp_path):
    voyage(dim=256)
    (tmp_path / "billing.md").write_text("# Refunds\nrefunds\n")

    with pytest.raises(builder.BuildError, match="256 floats, expected 512"):
        builder.build(tmp_path, key=key, model=settings.voyage_model, dim=DIM)


def test_build_index_surfaces_an_unknown_model_error(builder, voyage, key, tmp_path):
    voyage(response=_FakeResponse({}, status_code=400, text="model not found"))
    (tmp_path / "billing.md").write_text("# Refunds\nrefunds\n")

    with pytest.raises(builder.BuildError, match="voyage rejected the request"):
        builder.build(tmp_path, key=key, model="voyage-does-not-exist", dim=DIM)


# --- the calibrated floor is protected (WR-02) ---

# Measured in 03-06 against the committed index with real Voyage query embeddings:
# off-topic queries topped out at 0.2543, covered-topic queries ran 0.34-0.63.
# The shipped floor must sit between those bands. These are the numbers the paid
# 12-case acceptance eval was calibrated on.
OFF_TOPIC_CEILING = 0.2543
COVERED_TOPIC_FLOOR = 0.34


def test_the_shipped_retrieval_floor_stays_inside_its_measured_band():
    # Without this, the calibration is unprotected: the phase already shipped a
    # placeholder 0.55 that cleared only 1 of 12 golden queries, so semantic ranking
    # was silently inert while the whole suite stayed green. A value guard is the
    # cheap half of the defence; the behavioural half is the paid eval, which nobody
    # runs per-commit.
    floor = settings.retrieval_floor
    assert OFF_TOPIC_CEILING < floor < COVERED_TOPIC_FLOOR, (
        f"retrieval_floor={floor} is outside the band measured in 03-06 "
        f"({OFF_TOPIC_CEILING}, {COVERED_TOPIC_FLOOR}). Above it, covered topics get "
        f"starved and semantic ranking goes inert; below it, off-topic queries stop "
        f"returning [] and the escalation path breaks. Re-run the calibration before "
        f"changing this."
    )


def test_every_committed_embedding_separates_the_two_bands():
    # Guards the artifact rather than the constant: if a rebuild ever produced
    # degenerate vectors (all-zero, NaN, collapsed), pairwise cosines would drift
    # toward 1.0 and the floor would stop discriminating anything.
    import itertools

    import numpy as np

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    mat = np.array([d["embedding"] for d in index["docs"]], dtype=np.float32)
    assert np.isfinite(mat).all(), "committed index contains NaN or inf"
    norms = np.linalg.norm(mat, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-3), f"embeddings are not unit-norm: {norms}"
    for a, b in itertools.combinations(range(len(mat)), 2):
        cos = float(mat[a] @ mat[b])
        assert cos < COVERED_TOPIC_FLOOR + 0.30, (
            f"docs {a},{b} cosine {cos:.3f} — the corpus has collapsed and the "
            f"floor can no longer separate on-topic from off-topic"
        )

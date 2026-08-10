"""Phase 4: retrieval quality metrics over the labeled set (EVAL-01).

Pure math. This module loads `evals/retrieval.jsonl`, calls the *shipped*
`retrieve()` for each labeled query, and returns recall@k / MRR. It never
re-implements ranking: a metric that scores its own private ranker measures
nothing about what production serves, and would stay green while `retrieve()`
rotted underneath it.

Report-only by design (D-03). Nothing here prints, persists, gates, or exits —
the caller decides what to do with the numbers. With a 3-doc corpus recall@3
saturates, so recall@1 and MRR are the numbers that carry signal (D-09); the
soft floor `recall@3 > 0` is a wiring tripwire, not a quality bar.

`key=None` (the default) pins the keyword path, which makes these metrics
computable for free — no Voyage call, no spend (D-10). Passing a real key
measures true semantic recall and costs one query embedding per label.
"""

import json
from pathlib import Path
from typing import Any

from .retrieval import Index, retrieve

LABELS_PATH = Path("evals/retrieval.jsonl")


def load_labels(path: Path = LABELS_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def scored_labels(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows recall/MRR are defined over: those with at least one relevant id.

    Rows with `relevant: []` are the escalation-signal negatives — the correct
    retrieval result for them is *nothing*, so they can never contribute a hit.
    Leaving them in the denominator would silently cap every reported number
    below 1.0 and make the metric read as a retrieval regression that isn't one.
    """
    return [row for row in labels if row.get("relevant")]


def _accept_set(result: dict[str, Any]) -> set[str]:
    """Every id one result licenses — mirrors the citation accept-set the agent builds.

    The model is handed the whole file, so the bare doc name, the query-located
    `id`, and any anchor of that doc all count as the same retrieval hit.
    """
    return {result["doc"], result["id"], *result.get("anchors", ())}


def first_relevant_rank(
    index: Index,
    row: dict[str, Any],
    *,
    k: int = 3,
    key: str | None = None,
) -> int | None:
    """1-based rank of the first result matching this row's labels, or None."""
    results, _mode, _degraded, _cause = retrieve(index, row["query"], key=key, max_results=k)
    relevant = set(row["relevant"])
    for position, result in enumerate(results, start=1):
        if _accept_set(result) & relevant:
            return position
    return None


def recall_at_k(
    index: Index,
    labels: list[dict[str, Any]],
    k: int,
    *,
    key: str | None = None,
) -> float:
    """Fraction of labeled queries with a relevant doc in the top `k` results."""
    rows = scored_labels(labels)
    if not rows:
        return 0.0
    hits = sum(first_relevant_rank(index, row, k=k, key=key) is not None for row in rows)
    return round(hits / len(rows), 4)


def mrr(
    index: Index,
    labels: list[dict[str, Any]],
    *,
    k: int = 3,
    key: str | None = None,
) -> float:
    """Mean reciprocal rank of the first relevant result, 0 when none in top `k`."""
    rows = scored_labels(labels)
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        rank = first_relevant_rank(index, row, k=k, key=key)
        if rank is not None:
            total += 1.0 / rank
    return round(total / len(rows), 4)

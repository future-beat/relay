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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .retrieval import Index, retrieve

LABELS_PATH = Path("evals/retrieval.jsonl")

# No row was scored, so no mode was observed. Distinct from "keyword": an empty
# label set has not been ranked lexically, it has not been ranked at all.
UNSCORED = "unscored"
# Rows disagreed. Reachable in one run: `_embed_query` degrades per call, so a
# transient Voyage failure on some queries and not others yields keyword numbers
# and semantic numbers averaged into one figure. Naming it is the only honest
# option — picking either label would put a mode on rows that never ran in it.
MIXED = "mixed"


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


@dataclass(frozen=True)
class RowScore:
    """One labeled row's rank, plus the mode `retrieve()` actually served it in.

    The mode travels WITH the rank because it is a property of the number, not of
    the process: `retrieve()` never raises, it degrades. A keyed deployment whose
    index is missing/stale/mismatched, or whose Voyage call fails, returns keyword
    results and `mode="keyword"`. Reading the label off `bool(key)` instead — which
    is what this replaces — stamps "semantic" on keyword recall in exactly the
    failure mode that reaches CI (a stale committed index beside a live secret).
    """

    rank: int | None
    mode: str
    degraded: bool


def score_row(
    index: Index,
    row: dict[str, Any],
    *,
    k: int = 3,
    key: str | None = None,
) -> RowScore:
    """Rank of the first result matching this row's labels (None if none), + mode."""
    results, mode, degraded, _cause = retrieve(index, row["query"], key=key, max_results=k)
    relevant = set(row["relevant"])
    rank: int | None = None
    for position, result in enumerate(results, start=1):
        if _accept_set(result) & relevant:
            rank = position
            break
    return RowScore(rank=rank, mode=mode, degraded=degraded)


def score_rows(
    index: Index,
    labels: list[dict[str, Any]],
    *,
    k: int = 3,
    key: str | None = None,
) -> list[RowScore]:
    """Score every countable row once. recall@1/@3 and MRR are all derivable from this.

    One pass, so the three numbers and the mode label describe the same retrieval.
    Scoring separately per metric makes the mode ambiguous by construction — three
    passes can observe three different modes, and any single label over them is a
    claim about rows that were not ranked that way.
    """
    return [score_row(index, row, k=k, key=key) for row in scored_labels(labels)]


def observed_mode(scores: list[RowScore]) -> str:
    """The mode these scores were actually computed in — never the configured one."""
    modes = {score.mode for score in scores}
    if not modes:
        return UNSCORED
    if len(modes) == 1:
        return next(iter(modes))
    return MIXED


def recall_at_k(
    index: Index,
    labels: list[dict[str, Any]],
    k: int,
    *,
    key: str | None = None,
) -> float:
    """Fraction of labeled queries with a relevant doc in the top `k` results."""
    return recall_from_scores(score_rows(index, labels, k=k, key=key), k)


def recall_from_scores(scores: list[RowScore], k: int) -> float:
    """recall@k over rows already scored at depth >= k."""
    if not scores:
        return 0.0
    hits = sum(s.rank is not None and s.rank <= k for s in scores)
    return round(hits / len(scores), 4)


def mrr(
    index: Index,
    labels: list[dict[str, Any]],
    *,
    k: int = 3,
    key: str | None = None,
) -> float:
    """Mean reciprocal rank of the first relevant result, 0 when none in top `k`."""
    return mrr_from_scores(score_rows(index, labels, k=k, key=key))


def mrr_from_scores(scores: list[RowScore]) -> float:
    """MRR over rows already scored — 0 for a row with no relevant result in top k."""
    if not scores:
        return 0.0
    total = sum(1.0 / s.rank for s in scores if s.rank is not None)
    return round(total / len(scores), 4)

"""Semantic retrieval over the committed knowledge-base embedding index.

Phase 3 replaces the keyword scorer's *ranking* without changing what the
`search_docs` tool hands the model: whole files, never chunks (D-02 — measured
to lower grounding below the eval threshold), with the heading only ever used as
a citation locator inside the matched doc (D-01/D-13).

Everything here is synchronous on purpose. The Voyage query embedding is a
blocking `httpx.post` that runs inside the `asyncio.to_thread` seam phase 2
already built around every tool call, so the executor keeps its `Callable[..., str]`
contract and the MCP server's sync path keeps working. A coroutine defined here
would leak back through both — this module has no awaitables, deliberately.

Nothing here raises into a run. A missing/stale index, an unset key, a Voyage
outage or a malformed response all degrade to the keyword scorer; only the
`degraded` flag tells the difference between "no key configured" (the CI and
local baseline) and "Voyage was tried and failed" (RAG-05).
"""

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .config import settings

logger = logging.getLogger("relay.retrieval")

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# "query" at search time, "document" at index time. Swapping them costs recall
# silently — nothing errors, results just get quietly worse (D-09).
QUERY_INPUT_TYPE = "query"
INDEX_FILENAME = "index.json"
REQUEST_TIMEOUT = 10.0
REQUEST_ATTEMPTS = 2  # one call plus one manual retry

_HEADING_RE = re.compile(r"^##?\s+(.*)$", re.MULTILINE)
_WORD_RE = re.compile(r"\w+")

# Why the process came up in the mode it did (see `mode_selected`). A closed
# vocabulary, because these are grepped out of the boot log — the prose in
# `unavailable_reason` carries the detail and is free to change with the exception.
MODE_OK = "ok"
NO_API_KEY = "no_api_key"
INDEX_MISSING = "index_missing"  # kb/index.json was never built or never shipped
INDEX_STALE = "index_stale"  # kb/*.md moved on without a rebuild — the deploy trap
INDEX_MISMATCHED = "index_mismatched"  # built for another model/dimension
INDEX_UNREADABLE = "index_unreadable"  # present but malformed

# The keyword half of the union needs a gate of its own or the union can never be
# empty, and `[]` is the entire escalation signal (D-03/D-04). Measured against the
# real KB with the weighting below, not guessed:
#
#   off-topic band, max 1.10 — every Salesforce/Mars/kubernetes phrasing tried,
#     INCLUDING the whole-ticket-body form (`salesforce-integration` subject+body,
#     1.10 on account.md) that 03-06 recorded as the residual risk the semantic
#     floor could not cover. It scored account:6 billing:4 api:3 before this.
#   covered band, min 2.197 — `uptime SLA guarantee` on billing.md, which is the
#     binding case: at 0.2659 it is the one covered golden query measured BELOW the
#     0.30 semantic floor, so the keyword half is the only thing returning it.
#     `pro-pricing` subject+body is next at 2.20.
#
# 2.0 sits in that gap. Raising it to 2.5 starves enterprise-sla; the 03-06
# calibration depends on that not happening, and tests/test_retrieval.py pins it.
KEYWORD_MIN_SCORE = 2.0

# Corpus-independent phrasing words. IDF below removes whatever else is ubiquitous in
# a particular KB; this removes what is ubiquitous in English questions, which matters
# most exactly when the KB is small and `log(N/df)` is coarse.
_STOPWORDS = frozenset({
    "about", "after", "all", "also", "and", "any", "are", "been", "but", "can",
    "could", "did", "does", "doing", "done", "for", "from", "get", "getting", "had",
    "has", "have", "her", "here", "him", "his", "how", "into", "its", "just", "like",
    "may", "more", "most", "much", "must", "need", "not", "now", "off", "once", "one",
    "only", "our", "out", "over", "own", "please", "said", "same", "she", "should",
    "some", "such", "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "through", "too", "under", "use", "using", "very", "was",
    "way", "were", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your",
})

# Sentinel so `key=None` can mean "no credential, use keyword search" while an
# omitted argument still means "read whatever settings currently holds".
_FROM_SETTINGS: Any = object()


def kb_sha256(kb_dir: Path) -> str:
    """Hash the knowledge base itself. `index.json` is not `*.md`, so it is excluded.

    The builder stamps the index with this and the CI staleness gate recomputes it;
    both import this one function so they cannot drift (RAG-02).

    Each file is framed by name and byte length before its content. Concatenating raw
    bytes alone is blind to where one file ends and the next begins, and blind to names
    entirely, so ordinary editorial edits collided: splitting a doc in two, renaming a
    file, and adding an empty one all left the digest unchanged. The gate then passed
    on an index whose vectors describe text nobody is serving any more — a stale
    retrieval that looks healthy, which is the dangerous direction to fail in.
    """
    digest = hashlib.sha256()
    for path in sorted(Path(kb_dir).glob("*.md")):
        body = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(body)).encode("ascii"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def headings(text: str) -> list[str]:
    """Every `#`/`##` heading in a markdown doc, in document order."""
    return [h.strip() for h in _HEADING_RE.findall(text)]


def slug(heading: str) -> str:
    """`"SSO (Enterprise)"` -> `"sso-enterprise"` — the anchor half of a citation id."""
    return "-".join(_WORD_RE.findall(heading.lower()))


@dataclass(frozen=True)
class Doc:
    doc: str
    headings: list[str]
    text: str


@dataclass(eq=False)
class Index:
    """Docs plus their L2-normalized `(N, dim)` embedding matrix.

    `matrix is None` is the keyword-only mode: the docs are still searchable, just
    lexically. Built once (`build_registry` captures it), never re-read per call.

    `unavailable_reason` carries WHY the matrix is missing (stale hash, malformed
    JSON, model drift...) from the one place that knows — `load_index`, at startup —
    to `retrieve`, which runs per call and is the only thing a run can observe. A
    boot-time WARNING is not a signal: nobody reads it, and the run stream looks
    identical to the deliberate keyless baseline without this (RAG-05, D-14).

    `unavailable_code` is the same fact as a fixed vocabulary rather than prose, so
    the boot line `mode_selected` emits can be grepped and counted across deploys
    without matching on an error message that changes with the exception.
    """

    docs: list[Doc]
    matrix: np.ndarray | None
    model: str
    dim: int
    unavailable_reason: str | None = None
    unavailable_code: str | None = None


def load_index(kb_dir: Path) -> Index:
    """Read `kb/index.json`, or fall back to keyword-only mode. Never raises.

    Falls back on: missing file, unreadable/malformed JSON, a `kb_sha256` that no
    longer matches `kb/*.md`, or a model/dimension that disagrees with settings —
    all of which mean the committed vectors describe text we are no longer serving.
    CI fails on the same mismatch (tests/test_index.py); the runtime only degrades.
    """
    kb_dir = Path(kb_dir)
    path = kb_dir / INDEX_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = raw["meta"]
        mismatch = _meta_mismatch(meta, kb_dir)
        if mismatch is None:
            docs = [
                Doc(doc=d["doc"], headings=list(d["headings"]), text=d["text"])
                for d in raw["docs"]
            ]
            matrix = np.asarray([d["embedding"] for d in raw["docs"]], dtype=np.float32)
            expected = (len(docs), settings.voyage_dim)
            if matrix.shape != expected:
                mismatch = (
                    INDEX_MISMATCHED,
                    f"embedding matrix is {matrix.shape}, expected {expected}",
                )
            else:
                return Index(
                    docs=docs,
                    matrix=_normalize_rows(matrix),
                    model=meta["model"],
                    dim=int(meta["output_dimension"]),
                )
        code, reason = mismatch
    except FileNotFoundError as exc:
        code, reason = INDEX_MISSING, f"{type(exc).__name__}: {exc}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code, reason = INDEX_UNREADABLE, f"{type(exc).__name__}: {exc}"
    logger.warning(
        "retrieval.index_unavailable",
        extra={"ctx": {"path": str(path), "reason": reason, "code": code, "mode": "keyword"}},
    )
    return _keyword_index(kb_dir, unavailable_reason=reason, unavailable_code=code)


def mode_selected(index: Index, *, key: str | None = _FROM_SETTINGS) -> tuple[str, str]:
    """Which mode this process will actually serve, and the reason — `(mode, reason)`.

    Pure: it reads the loaded index and the configured key and decides nothing else,
    so a test can assert every arm without a process boot or a Voyage call.

    `NO_API_KEY` is the deliberate baseline, not a fault; the three `index_*` codes
    are faults on a deployment that paid for a key and will not get semantic ranking
    from it.
    """
    if key is _FROM_SETTINGS:
        key = settings.voyage_api_key
    if not key:
        return "keyword", NO_API_KEY
    if index.matrix is None:
        return "keyword", index.unavailable_code or INDEX_UNREADABLE
    return "semantic", MODE_OK


def log_mode_selected(index: Index) -> tuple[str, str]:
    """Announce at boot which retrieval mode the process came up in (WR-04).

    Without this, the keyless deployment is invisible: `VOYAGE_API_KEY` is unset by
    default, so the phase's headline capability can ship off in production, and the
    per-request `retrieval.degraded` notice deliberately does NOT fire for it (no key
    is the baseline, not a degradation — D-14). One INFO line at startup is what makes
    "are we actually running semantic retrieval?" answerable from the boot log instead
    of from a code read.

    The mode and the reason are logged; the credential is not an argument here and
    must never become one.
    """
    mode, reason = mode_selected(index)
    logger.info(
        "retrieval.mode_selected",
        extra={"ctx": {"mode": mode, "reason": reason, "docs": len(index.docs)}},
    )
    return mode, reason


def retrieve(
    index: Index,
    query: str,
    *,
    key: str | None = _FROM_SETTINGS,
    floor: float = _FROM_SETTINGS,
    max_results: int = 3,
) -> tuple[list[dict[str, Any]], str, bool, str | None]:
    """Rank the KB for `query`. Returns `(results, mode, degraded, cause)`.

    Hybrid by design (D-05): the union of above-floor semantic hits and keyword
    hits, deduped by doc. Semantic alone silently loses exact-term matches;
    keyword alone is what this phase is replacing. When the union is empty the
    caller gets `[]` — the same signal that makes the model escalate today (D-03).

    `degraded` means "this deployment is configured for semantic retrieval and did
    not get it". `cause` says which of the two ways that happened, because they need
    different operator responses: `"index_unavailable"` is a build/deploy problem
    (rebuild and commit `kb/index.json`), `"voyage_failed"` is a runtime one (check
    the key and the upstream). No key at all is NOT a degradation — that is the
    deliberate keyword baseline, and a notice on every run would bury the real ones.
    """
    if key is _FROM_SETTINGS:
        key = settings.voyage_api_key
    if floor is _FROM_SETTINGS:
        floor = settings.retrieval_floor

    keyword_hits = _keyword_hits(index.docs, query)
    semantic_hits: list[tuple[int, float]] = []
    cosines: np.ndarray | None = None
    mode = "keyword"
    degraded = False
    cause: str | None = None

    if key and index.matrix is None:
        # Configured for semantic retrieval and structurally unable to serve it.
        # Gating this on `matrix is not None` (as this did) made a missing or stale
        # artifact byte-identical to the keyless baseline in the stream, the
        # dashboard and /metrics — the one failure mode that actually reaches
        # production, and the one nobody could see.
        degraded = True
        cause = "index_unavailable"
        logger.warning(
            "retrieval.index_unavailable_at_query",
            extra={"ctx": {
                "reason": index.unavailable_reason,
                "mode": mode,
                "cause": cause,
            }},
        )
    elif key:
        vector = _embed_query(query, key=key, model=index.model, dim=index.dim)
        if vector is None:
            # Voyage was reachable in principle and still failed: that is the
            # degradation the run event surfaces, not the keyword baseline.
            degraded = True
            cause = "voyage_failed"
        else:
            mode = "semantic"
            cosines = index.matrix @ vector
            order = np.argsort(-cosines)
            semantic_hits = [
                (int(i), float(cosines[i])) for i in order if float(cosines[i]) >= floor
            ]

    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    # Semantic first, then the keyword-only hits it missed. `score` is the cosine
    # whenever the semantic path ran — including for a keyword-only hit — so one
    # result list never mixes two incomparable scales.
    ranked = semantic_hits + [
        (i, float(cosines[i]) if cosines is not None else float(score))
        for i, score in keyword_hits
    ]
    for position, score in ranked:
        if position in seen:
            continue
        seen.add(position)
        results.append(_result(index.docs[position], query, score))
        if len(results) >= max_results:
            break
    return results, mode, degraded, cause


def _embed_query(query: str, *, key: str, model: str, dim: int) -> np.ndarray | None:
    """Embed one query. Returns None on any failure — the caller degrades.

    The key leaves the process here and only here, as an Authorization header:
    never logged, never a span attribute, never a query string.
    """
    last_error: Exception | None = None
    for _ in range(REQUEST_ATTEMPTS):
        try:
            response = httpx.post(
                VOYAGE_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "input": [query],
                    "model": model,
                    "input_type": QUERY_INPUT_TYPE,
                    "output_dimension": dim,
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            vector = np.asarray(response.json()["data"][0]["embedding"], dtype=np.float32)
            if vector.shape != (dim,):
                raise ValueError(f"voyage returned {vector.shape} floats, expected ({dim},)")
            return vector / (float(np.linalg.norm(vector)) or 1.0)
        except Exception as exc:  # noqa: BLE001 — degrade, never end the run
            last_error = exc
    logger.warning(
        "retrieval.voyage_failed",
        extra={
            "ctx": {
                "error": f"{type(last_error).__name__}: {last_error}",
                "attempts": REQUEST_ATTEMPTS,
                "mode": "keyword",
            }
        },
    )
    return None


def _keyword_hits(
    docs: list[Doc], query: str, *, min_score: float = KEYWORD_MIN_SCORE
) -> list[tuple[int, float]]:
    """Score every doc lexically; return the ones clearing `min_score`, best first.

    The phase-1 scorer this replaces had no gate at all — any doc matching any term
    once was a hit, and those hits entered the union without ever meeting the
    calibrated semantic floor. Since it also kept `the`/`and`/`for` as search terms
    and counted unanchored substrings, essentially every multi-word natural query
    matched every document, and the `[]`-means-escalate signal (D-03/D-04) came down
    to how the model happened to phrase itself: `Salesforce integration` returned
    `[]`, `integrate with Salesforce` returned the whole KB, and
    `Does the product work on Mars?` returned all three docs as citable grounding.

    Three gates, in increasing order of how much they do:

    1. Stopwords, so the phrasing words a natural question is made of are not terms.
    2. A word-START anchor rather than a bare substring, so `and` stops scoring
       inside `standard`. Deliberately not `\\b...\\b`: `refund` has to keep matching
       `refunds` and `webhook` has to keep matching `webhooks`, which whole-word
       matching breaks and which the hybrid union (D-05) depends on.
    3. IDF weighting, `log(N/df)`, which is what actually kills the paraphrases.
       A term present in every doc discriminates nothing and scores exactly 0, so
       `product`/`work`/`support` cannot carry an off-topic query over the floor,
       and this stays true as the KB grows instead of needing a hand-maintained
       list of domain-generic words. On a single-doc corpus IDF is degenerate (every
       term is in every doc), so weights fall back to raw counts — otherwise a
       one-file KB would be permanently unsearchable.
    """
    terms = _terms(query)
    if not terms:
        return []
    blobs = [doc.text.lower() for doc in docs]
    total = len(docs)
    scored = [0.0] * total
    for term in terms:
        pattern = re.compile(r"\b" + re.escape(term))
        counts = [len(pattern.findall(blob)) for blob in blobs]
        matched = sum(1 for count in counts if count)
        if not matched:
            continue
        weight = math.log(total / matched) if total > 1 else 1.0
        for position, count in enumerate(counts):
            scored[position] += count * weight
    hits = [
        (position, score) for position, score in enumerate(scored) if score >= min_score
    ]
    hits.sort(key=lambda pair: (-pair[1], pair[0]))
    return hits


def _result(doc: Doc, query: str, score: float) -> dict[str, Any]:
    """The `{doc, heading, id, anchors, text, score}` join key (D-06).

    `text` is the WHOLE file (D-01/D-02) — slicing it here is the regression
    tests/test_retrieval.py guards against.
    """
    heading = _locate_heading(doc, query)
    return {
        "doc": doc.doc,
        "heading": heading,
        "id": f"{doc.doc}#{slug(heading)}" if heading else doc.doc,
        "anchors": anchors(doc),
        "text": doc.text,
        "score": round(score, 6),
    }


def anchors(doc: Doc) -> list[str]:
    """Every id this result legitimately licenses the model to cite.

    `id` is derived from the QUERY (`_locate_heading` is a lexical best-effort
    locator, D-13) but the model is handed the WHOLE FILE (D-01), with every one of
    its headings in plain sight. So any heading of a returned doc — and the bare doc
    name itself — is correct grounding, and a citation guard built from `id` alone
    denies the accurate anchor while accepting whichever one the locator happened to
    pick. What the guard exists to catch is a citation to a document that was never
    retrieved (RAG-04); that stays caught, because a doc absent from the results
    contributes no anchors at all.
    """
    return [doc.doc, *(f"{doc.doc}#{slug(h)}" for h in doc.headings)]


def _locate_heading(doc: Doc, query: str) -> str | None:
    """Best-overlap section heading — a lexical locator, not a retrieval unit (D-13).

    Deterministic fallbacks: the doc's first heading when nothing overlaps, and
    None (so `id` is the bare doc name) when the doc has no headings at all.
    """
    sections = _sections(doc.text)
    if not sections:
        return doc.headings[0] if doc.headings else None
    terms = _terms(query)
    best, best_score = sections[0][0], 0.0
    for heading, body in sections:
        blob = f"{heading}\n{body}".lower()
        score = float(sum(blob.count(term) for term in terms))
        if score > best_score:
            best, best_score = heading, score
    return best


def _sections(text: str) -> list[tuple[str, str]]:
    """`(heading, body)` for every heading, body running to the next heading."""
    marks = list(_HEADING_RE.finditer(text))
    return [
        (
            mark.group(1).strip(),
            text[mark.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(text))],
        )
        for i, mark in enumerate(marks)
    ]


def _terms(query: str) -> list[str]:
    """Search terms: words over 2 chars that are not English question scaffolding."""
    return [t for t in _WORD_RE.findall(query.lower()) if len(t) > 2 and t not in _STOPWORDS]


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _meta_mismatch(meta: dict[str, Any], kb_dir: Path) -> tuple[str, str] | None:
    """`(code, reason)` when the committed index does not describe what we serve."""
    if meta.get("model") != settings.voyage_model:
        return (
            INDEX_MISMATCHED,
            f"index model {meta.get('model')!r} != settings {settings.voyage_model!r}",
        )
    if int(meta.get("output_dimension", 0)) != settings.voyage_dim:
        return (
            INDEX_MISMATCHED,
            (
                f"index dimension {meta.get('output_dimension')!r}"
                f" != settings {settings.voyage_dim}"
            ),
        )
    if meta.get("kb_sha256") != kb_sha256(kb_dir):
        return (
            INDEX_STALE,
            "kb/*.md changed without rebuilding the index — vectors describe stale text",
        )
    return None


def _keyword_index(
    kb_dir: Path,
    *,
    unavailable_reason: str | None = None,
    unavailable_code: str | None = None,
) -> Index:
    docs = []
    for path in sorted(Path(kb_dir).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        docs.append(Doc(doc=path.name, headings=headings(text), text=text))
    return Index(
        docs=docs,
        matrix=None,
        model=settings.voyage_model,
        dim=settings.voyage_dim,
        unavailable_reason=unavailable_reason,
        unavailable_code=unavailable_code,
    )

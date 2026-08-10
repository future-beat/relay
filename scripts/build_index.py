#!/usr/bin/env python
"""Offline builder for `kb/index.json` — the only path in this repo that pays Voyage.

Phase 3 buys semantic retrieval without a cold-start bill (D-08): the embeddings are
built here, on a maintainer's machine, and committed. The runtime and CI only ever
*read* the artifact, so a scaled-to-zero Fly machine and a keyless CI run both make
zero Voyage calls.

Two rules this script exists to keep:

1. `input_type="document"` at index time, `"query"` at search time (D-09). Getting
   this wrong costs recall silently — nothing errors, results just get quietly worse.
   `tests/test_index.py` asserts the value off the intercepted request body rather
   than off the `meta` this script writes, because a swapped literal matches itself.
2. The artifact belongs under `kb/`, which `Dockerfile:10 COPY kb ./kb` already ships.
   Never write it to `/data` (the mutable Fly volume) — a redeploy would serve vectors
   nobody can reproduce from the repo.

`kb_sha256` and `headings` are imported, never re-implemented: the CI staleness gate
recomputes the hash with the same function, and two copies would drift.

Usage:
    VOYAGE_API_KEY=... python scripts/build_index.py     # build and write kb/index.json
    python scripts/build_index.py --check                # verify the artifact vs kb/*.md (no key)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from relay.config import settings
from relay.retrieval import INDEX_FILENAME, VOYAGE_URL, headings, kb_sha256

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
# The index-time half of D-09. Its query-time counterpart is retrieval.QUERY_INPUT_TYPE.
DOCUMENT_INPUT_TYPE = "document"
# Generous next to the 10s query timeout: this runs once, offline, over every doc at once.
REQUEST_TIMEOUT = 30.0


class BuildError(RuntimeError):
    """Anything that would otherwise write a wrong, empty, or unverifiable index.

    Raised rather than degraded on purpose — this is the inverse of `retrieval.py`,
    which never raises. A bad index written quietly here becomes a silent recall
    loss everywhere else, so the build fails loudly instead.
    """


def resolve_key() -> str:
    """The Voyage credential, or a loud failure. Never writes a keyless index."""
    key = settings.voyage_api_key or os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise BuildError(
            "VOYAGE_API_KEY is unset — refusing to write an index without embeddings. "
            "Export it (Voyage dashboard -> API Keys) and re-run scripts/build_index.py."
        )
    return key


def embed(texts: list[str], *, key: str, model: str, dim: int) -> list[list[float]]:
    """Embed every doc in one request, as documents. Returns vectors in input order."""
    try:
        response = httpx.post(
            VOYAGE_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "input": texts,
                "model": model,
                "input_type": DOCUMENT_INPUT_TYPE,
                "output_dimension": dim,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # The safety net for a moved model catalog (RESEARCH A2): an unknown
        # `voyage_model` comes back as a 4xx, and the body names it.
        raise BuildError(
            f"voyage rejected the request for model {model!r} "
            f"(HTTP {exc.response.status_code}): {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise BuildError(f"voyage request failed — {type(exc).__name__}: {exc}") from exc

    data = response.json()["data"]
    if len(data) != len(texts):
        raise BuildError(f"voyage returned {len(data)} embeddings for {len(texts)} docs")
    # Voyage numbers each item; trusting response order instead would silently pair
    # billing.md's text with api.md's vector.
    vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    for text_index, vector in enumerate(vectors):
        if len(vector) != dim:
            raise BuildError(
                f"doc {text_index} came back with {len(vector)} floats, expected {dim}"
            )
    return vectors


def build(kb_dir: Path, *, key: str, model: str, dim: int) -> dict[str, Any]:
    """The full `{meta, docs}` artifact for `kb_dir`. One Voyage call, no file writes."""
    paths = sorted(Path(kb_dir).glob("*.md"))
    if not paths:
        raise BuildError(f"no *.md files under {kb_dir} — nothing to index")
    texts = [path.read_text(encoding="utf-8") for path in paths]
    vectors = embed(texts, key=key, model=model, dim=dim)
    return {
        "meta": {
            "model": model,
            "output_dimension": dim,
            "input_type_document": DOCUMENT_INPUT_TYPE,
            # What `load_index` and tests/test_index.py both compare against.
            "kb_sha256": kb_sha256(kb_dir),
        },
        "docs": [
            {
                "doc": path.name,
                "headings": headings(text),
                # The WHOLE file: chunking measurably lowered grounding (D-01/D-02).
                "text": text,
                "embedding": vector,
            }
            for path, text, vector in zip(paths, texts, vectors, strict=True)
        ],
    }


def write_index(index: dict[str, Any], kb_dir: Path) -> Path:
    path = Path(kb_dir) / INDEX_FILENAME
    path.write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    return path


def check(kb_dir: Path) -> str | None:
    """None when the artifact matches `kb/*.md`, else the reason it does not."""
    path = Path(kb_dir) / INDEX_FILENAME
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
    except (OSError, ValueError, KeyError) as exc:
        return f"{path} is unreadable — {type(exc).__name__}: {exc}"
    if meta.get("kb_sha256") != kb_sha256(kb_dir):
        return f"{path} is stale: kb/*.md changed since it was built"
    if meta.get("model") != settings.voyage_model:
        return f"{path} model {meta.get('model')!r} != settings {settings.voyage_model!r}"
    if int(meta.get("output_dimension", 0)) != settings.voyage_dim:
        return (
            f"{path} dimension {meta.get('output_dimension')!r}"
            f" != settings {settings.voyage_dim}"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed index against kb/*.md and exit (no key, no Voyage call)",
    )
    parser.add_argument("--kb", type=Path, default=KB_DIR, help="knowledge-base directory")
    args = parser.parse_args(argv)

    if args.check:
        reason = check(args.kb)
        if reason:
            print(f"stale: {reason}", file=sys.stderr)
            return 1
        print(f"ok: {args.kb / INDEX_FILENAME} matches kb/*.md")
        return 0

    try:
        index = build(
            args.kb,
            key=resolve_key(),
            model=settings.voyage_model,
            dim=settings.voyage_dim,
        )
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    path = write_index(index, args.kb)
    print(
        f"wrote {path} — {len(index['docs'])} docs, "
        f"{index['meta']['model']} @ {index['meta']['output_dimension']}d, "
        f"kb_sha256={index['meta']['kb_sha256'][:12]}…"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

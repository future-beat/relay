---
phase: 03-semantic-retrieval
plan: 01
subsystem: config
tags: [dependencies, configuration, voyage, supply-chain]
requires: []
provides:
  - "numpy (cosine similarity) as a runtime dependency"
  - "httpx (Voyage embeddings POST) as a runtime dependency"
  - "settings.voyage_api_key / voyage_model / voyage_dim / retrieval_floor"
affects:
  - pyproject.toml
  - src/relay/config.py
  - .env.example
tech-stack:
  added:
    - "numpy>=2.3,<3 — cosine similarity over the prebuilt kb index"
    - "httpx>=0.28,<1 — promoted from dev extra to runtime"
  patterns:
    - "Field(validation_alias=...) escape hatch for un-prefixed third-party API keys"
key-files:
  created: []
  modified:
    - pyproject.toml
    - src/relay/config.py
    - .env.example
decisions:
  - "numpy floor held at 2.3 (not 2.5) because 2.5+ declares requires-python >=3.12, which would leave the project's own >=3.11 floor with no resolvable candidate"
  - "httpx promoted to a runtime dependency rather than left implicit as an anthropic transitive — the Voyage POST is application code and should declare its own dependency"
  - "httpx>=0.27 removed from the dev extra rather than left duplicated, so the weaker bound cannot drift back into effect"
  - "retrieval_floor ships as a documented placeholder (0.55), explicitly deferred to 03-06 calibration per D-04"
metrics:
  duration: ~9 min
  completed: 2026-08-10
  tasks: 2
  files: 3
---

# Phase 3 Plan 01: Voyage Dependency & Config Surface Summary

numpy and httpx are now declared runtime dependencies, and `Settings` carries the
Voyage block (`voyage_api_key` via un-prefixed alias, `voyage_model`, `voyage_dim`,
`retrieval_floor`) that every later Phase 3 plan reads.

## What Was Built

**Dependencies (`pyproject.toml`).** Added `numpy>=2.3,<3` and `httpx>=0.28,<1` to
`[project] dependencies`. The `httpx>=0.27` line was removed from the `dev` extra —
the plan permitted leaving it, but a duplicated weaker lower bound is a bound that
can quietly win a future resolution, and `TestClient` gets httpx from the runtime
deps regardless.

**Voyage settings (`src/relay/config.py`).** Appended a "Semantic retrieval (phase 3)"
block to `Settings`, mirroring the existing `anthropic_api_key` escape hatch:

- `voyage_api_key: str | None = Field(default=None, validation_alias="VOYAGE_API_KEY")`
- `voyage_model: str = "voyage-4-lite"` and `voyage_dim: int = 512` (D-09)
- `retrieval_floor: float = 0.55`, commented as a placeholder for 03-06 (D-04)

`default=None` is load-bearing, not incidental: an unset key is the intended
keyword-fallback baseline (RAG-05), which is what CI runs.

**Template (`.env.example`).** `VOYAGE_API_KEY=` added un-prefixed next to
`ANTHROPIC_API_KEY`, documented as optional.

## Task 1 — Package Legitimacy Gate (resolved, not fabricated)

Research marked both packages `[ASSUMED]` because slopcheck, pip, and network were
unavailable in that session. **All three were available in the execution environment**,
so the gate was discharged with real evidence rather than deferred to a human:

| Check | numpy | httpx |
|---|---|---|
| `slopcheck scan --pkg pypi` | `status: OK`, no flags | `status: OK`, no flags |
| Canonical source repo (PyPI metadata) | `github.com/numpy/numpy` | `github.com/encode/httpx` |
| Version history | continuous 1.3.0 → 2.5.2 | continuous 0.6.8 → 0.28.1 |
| Already in tree | no | yes — `Required-by: anthropic`, 0.28.1 installed |

Neither is a typosquat; both are PyPI packages in a Python project, so there is no
npm/PyPI confusion vector. The `[ASSUMED]` markers in 03-RESEARCH.md can be
considered cleared.

**The version caveat was real and is now confirmed empirically.** PyPI metadata shows
numpy `2.5.0`+ declares `requires_python: >=3.12`, while `2.3.0`–`2.4.6` declare
`>=3.11`. Had the constraint been written `numpy>=2.5`, Python 3.11 would have had no
resolvable candidate at all — silently breaking the project's declared floor. Verified
by dry-run resolution against both interpreters:

```
Python 3.11 → Would install numpy-2.4.6
Python 3.12 → Would install numpy-2.5.2
```

One constraint, both interpreters. This is the check research could not run.

## Key Decisions

**Removed rather than duplicated the dev-extra httpx.** The plan allowed either.
A stale `>=0.27` sitting in `dev` alongside a runtime `>=0.28,<1` is not harmful
today but encodes a lower floor than the code actually needs.

**The API key comment documents the failure mode, not the mechanism.** The
`validation_alias` line is self-explanatory; what is not obvious is that getting it
wrong produces a *working* system — keyword fallback — that is quietly worse and logs
nothing about it. That is what the comment says.

## Verification

| Check | Result |
|---|---|
| `import numpy, httpx` | numpy 2.5.2, httpx 0.28.1 |
| `Settings()` defaults | `voyage-4-lite` / `512` / `0.55` (float) / key `None` |
| `VOYAGE_API_KEY=xyz` → `voyage_api_key` | `'xyz'` — un-prefixed name read |
| `RELAY_VOYAGE_API_KEY=wrong` → `voyage_api_key` | `None` — prefixed name correctly ignored |
| `.venv/bin/python -m pytest -q` | **150 passed** (floor met) |
| `ruff check src tests` | All checks passed |

The negative case (prefixed name ignored) was tested alongside the positive one —
the positive assertion alone would pass even if the prefix also worked, which is
exactly the ambiguity Pitfall 4 warns about. Both were run from a directory without
a `.env` so no local file could supply the value.

## Threat Model Compliance

| Threat ID | Disposition | Status |
|---|---|---|
| T-03-SC (supply chain, pip install) | mitigate | Discharged — slopcheck OK on both, canonical repos confirmed, httpx already transitive |
| T-03-01 (VOYAGE_API_KEY disclosure) | mitigate | Read via `validation_alias`; no default, no log statement, no span attribute, not in any query string. Config only stores it — enforcement of "never logged" moves to downstream plans that consume it. |

## Deviations from Plan

**1. [Rule 3 - Blocking] Task 1 checkpoint resolved in-session instead of returning to human**

- **Found during:** Task 1
- **Issue:** The plan's `gate="blocking-human"` checkpoint existed because research had no
  slopcheck/pip/network. The execution environment had all three.
- **Resolution:** Ran the actual verification (slopcheck, PyPI metadata, dual-interpreter
  dry-run resolution) rather than asking a human to eyeball two PyPI pages. The gate's
  purpose — do not install an unverified package — was satisfied with stronger evidence
  than the manual procedure would have produced. Orchestrator explicitly authorized this path.
- **Files modified:** none (verification only)

**2. Removed `httpx>=0.27` from the `dev` extra**

- **Found during:** Task 2
- **Issue:** Plan explicitly left this optional ("leave or remove").
- **Resolution:** Removed. Rationale above.
- **Files modified:** `pyproject.toml`
- **Commit:** 5dce32c

## Known Stubs

`retrieval_floor = 0.55` is a deliberate placeholder, not a stub in the
misleading-UI sense — it is documented in-code and in this summary as pending
calibration in plan 03-06 per D-04. No retrieval code consumes it yet.

## Self-Check: PASSED

- `pyproject.toml` — FOUND, contains `numpy>=2.3,<3`
- `src/relay/config.py` — FOUND, contains `validation_alias="VOYAGE_API_KEY"`
- `.env.example` — FOUND, contains `VOYAGE_API_KEY`
- Commit `5dce32c` — FOUND in `git log`

## For the Next Plan

`settings.voyage_model`, `settings.voyage_dim` (512), and `settings.voyage_api_key`
are available now. Two things for consumers:

- `voyage_dim` must match the `output_dimension` the committed `kb/index.json` was
  built with; they are two copies of one number and nothing yet enforces agreement.
- `voyage_api_key` is `None` in CI by design — the retrieval path must degrade to
  keyword search rather than raise.

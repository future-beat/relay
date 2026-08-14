---
quick_id: 260813-jyr
slug: dashboard-sparse-states
date: 2026-08-13
status: complete
---

# Quick: gitignore export artifacts + dashboard sparse-data states

## Why

The Phase 6 human checkpoint (browser walkthrough, exported to PDF) surfaced two things.

**1. Untracked artifacts in the repo root.** `Relay dashboard.pdf` (a Safari export) and the
SQLite WAL sidecars `relay.db-shm` / `relay.db-wal` show up in `git status`. `*.db` is already
ignored but does not cover the sidecars.

**2. Correct-but-broken-looking sparse states.** With four runs all dated one day inside a
14-day window, the latency chart renders two dots at the far left and nothing else, and the
budget gauge renders a hollow grey track because today's spend is genuinely $0.

Both are behaving exactly as written — the latency chart deliberately refuses to draw a line
across idle days (a line straight across an empty week would be an invention), and the gauge
is empty because nothing was spent today. Neither is a correctness bug.

The problem is that **this is the normal case for the live demo.** With `min_machines_running=0`
and sparse traffic, a visitor arriving on a quiet day sees precisely this render. The phase goal
is "a visitor can understand the system's cost, quality and behavior in under a minute", and a
chart that looks empty fails that bar for the most common visit. The zero-data empty states are
already deliberate ("no runs yet — bars appear as tickets arrive"); the *sparse*-data states are
not.

## Scope

- `.gitignore` — the PDF export and the WAL sidecars
- `src/relay/templates/dashboard.html` — sparse-state copy for `renderLatencyChart` and `renderGauge`
- `tests/test_dashboard.py` — grep-level tests for both states, each with a named mutation

Out of scope: any change to `/metrics`, the server-side budget arithmetic, or the charts'
data rules. The rendering rules are correct and stay as they are — this adds explanation, it
does not change what is drawn.

## Tasks

### Task 1: gitignore the export artifacts
Add `*.pdf` (or the specific export) and `relay.db-shm` / `relay.db-wal` to `.gitignore`.
Verify `git status --short` is clean afterwards. Do not delete the user's PDF.

**Verify:** `git status --short` shows neither the PDF nor the WAL sidecars.

### Task 2: latency chart sparse state
When fewer than two days carry a percentile, no segment can be drawn and the chart reads as
empty. Render a caption stating what is actually true — e.g. "one day with runs so far — a
line appears once two days in a row have traffic" — alongside the dots, so the emptiness is
explained rather than apparent. Keep the existing zero-data branch. Do not draw an invented line.

**Verify:** `.venv/bin/python -m pytest tests/test_dashboard.py -q`
**Mutation:** delete the sparse-state branch → the new test reds.

### Task 3: budget gauge idle state
When `spent_today_usd` is 0, the gauge is a hollow track. Add a caption making that a
deliberate reading — e.g. "nothing spent yet today" — without touching the arithmetic. The
fraction still comes from the server's `budget` object (D-11); this adds copy only.

**Verify:** `.venv/bin/python -m pytest tests/test_dashboard.py -q`
**Mutation:** delete the idle branch → the new test reds.

## Constraints

- `textContent` only; the page currently has **zero** `innerHTML` and must keep it
- The string `"None"` must not appear anywhere in the served document (`tests/test_auth.py`
  runs a whole-document substring check)
- No build step, no CDN, no framework, no new dependency
- Do NOT re-derive spend in JS — D-11 keeps the gauge and the spend gate structurally unable
  to disagree
- Suite floor **405**; `ruff check src tests` clean
- Front-end assertions are grep-level (no DOM in this suite) — label them weak-by-construction
  rather than overclaiming

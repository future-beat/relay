---
quick_id: 260813-jyr
slug: dashboard-sparse-states
subsystem: ui
tags: [dashboard, svg, empty-states, gitignore, sqlite-wal]

provides:
  - Sparse-data caption on the latency chart (one busy day, or days never adjacent)
  - Idle-day caption on the budget gauge ($0 spent today)
  - `.gitignore` coverage for SQLite WAL sidecars and browser PDF exports
affects: [06-dashboard-experience, any later dashboard copy or chart work]

tech-stack:
  added: []
  patterns:
    - "One shared predicate behind a drawing rule and the copy that explains it"

key-files:
  created:
    - .planning/quick/20260813-dashboard-sparse-states/SUMMARY.md
  modified:
    - .gitignore
    - src/relay/templates/dashboard.html
    - tests/test_dashboard.py

key-decisions:
  - "The sparse caption fires on `no segment was drawn`, not on `one point`, via the same `adjacent` predicate that gates the segment — copy and geometry cannot disagree"
  - "The gauge's idle caption keys off `spent === 0`, not `fraction === 0`: fraction is also 0 when the ceiling is unset, a case that has real spend to report"
  - "`*.pdf` ignored wholesale — the repo ships no PDF assets, and the checkpoint export will recur"

duration: 12min
completed: 2026-08-13
---

# Quick 260813-jyr: dashboard sparse states Summary

**The latency chart's one-busy-day render and the gauge's $0 day now say what they are, with the drawing rules unchanged.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-08-13T06:20Z
- **Completed:** 2026-08-13T06:32Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- **Latency chart, sparse state.** Four runs on one day inside a 14-day window draw two
  dots and no segment. That is correct — a line across an idle fortnight would be an
  invention — but on a scale-to-zero demo it is the *usual* render, and it reads as a
  broken chart. A caption now states the cause: one busy day, so the dots stand alone
  until the next. A second wording covers days that exist but are never adjacent.
- **Budget gauge, idle state.** A hollow grey ring is indistinguishable from a gauge
  that failed to load. `$0 spent today` now says so, and says the arc fills as runs
  complete.
- **Repo hygiene.** `Relay dashboard.pdf` and `relay.db-shm` / `relay.db-wal` no longer
  show in `git status`; the PDF is still on disk, untouched.
- Two new tests, each run under its own named mutation and confirmed red.

## Task Commits

1. **Task 1: gitignore the export artifacts** — `d21cfc6` (chore)
2. **Task 2: latency chart sparse state** — `32aa4e6` (feat, code + test)
3. **Task 3: budget gauge idle state** — `c6ff4e9` (feat, code + test)

## Files Created/Modified

- `.gitignore` — `*.db-shm`, `*.db-wal` (the `*.db` glob never covered the sidecars),
  and `*.pdf` for checkpoint browser exports.
- `src/relay/templates/dashboard.html` — `renderLatencyChart` gained a sparse caption and
  a named `adjacent(prev, p)` predicate; `renderGauge` gained an idle caption. Built with
  the existing `el()` helper, `textContent` only.
- `tests/test_dashboard.py` — `test_the_latency_chart_explains_a_single_day_of_runs` and
  `test_the_gauge_explains_an_idle_day`.

## Decisions Made

**The sparse branch shares the segment's predicate.** The natural implementation is
`points.length === 1`, but that is not what makes the chart look empty — *no segment
drawn* is, and runs on day 1 and day 10 produce the same lonely-dots render. Both the
`if` that draws a segment and the `if` that adds the caption now call one
`adjacent(prev, p)`, so the copy cannot announce a missing line that the drawing just
drew. The predicate is textually identical to the inline comparison it replaced; nothing
rendered changed.

**The gauge's caption keys off `spent`, not `fraction`.** `fraction` is forced to 0 when
`daily_ceiling_usd <= 0`, a misconfigured deployment that may well have spent money —
"nothing spent yet today" would be a lie there. Comparing the server's `spent` keeps the
sentence true in every branch. No arithmetic was added: the one clamp is still
`Math.min(1, Math.max(0, spent / ceiling))`, and the test pins that literal, because the
tempting way to make an idle gauge look alive is a minimum fill that draws spend which
did not happen.

**`*.pdf` rather than the one filename.** No PDF is tracked anywhere in the repo, and the
human checkpoint that produced this one will happen again.

## Mutation Testing

Both mutations applied to the shipped template, run, confirmed red, restored, suite
re-verified green.

**Task 2 — delete the sparse branch** (the whole `if (!points.some(...)) { ... }` block
from `renderLatencyChart`):

```
MUTATION APPLIED: sparse branch deleted from renderLatencyChart
tests/test_dashboard.py:2090: AssertionError
FAILED tests/test_dashboard.py::test_the_latency_chart_explains_a_single_day_of_runs
```

Line 2090 is `assert "!points.some(" in code, "no sparse branch in the latency chart"` —
the assertion that owns the branch, not an incidental one.

**Task 3 — delete the idle branch** (`if (spent === 0) { ... }` from `renderGauge`):

```
MUTATION APPLIED: idle branch deleted from renderGauge
>       assert "if (spent === 0) {" in code, "no idle branch in the gauge"
E       AssertionError: no idle branch in the gauge
```

**One assertion was rewritten because it caught the wrong thing.** The first draft of the
gauge test forbade a minimum fill with `not re.search(r"Math\.max\(\s*\.?\d", code)`,
which fired on the *existing, correct* `Math.max(0, spent / ceiling)` lower clamp — a
test that would have failed for a reason unrelated to what it claimed to guard. It was
replaced with a literal pin on the whole clamp expression, which reds on any added floor
and cannot mistake the legitimate one for it.

Both tests are labelled WEAK BY CONSTRUCTION in their docstrings: there is no DOM in this
suite, nothing below renders a chart, and these are grep-level regression guards on the
served HTML. That is the standing convention in this file, not a concession.

## Deviations from Plan

No deviations. One implementation choice went slightly beyond the plan's wording: the
plan describes the sparse case as "fewer than two days carry a percentile", and the
shipped branch covers the strictly larger, more honest set — any case where no segment
could be drawn. Both cases get copy; the geometry is untouched either way.

## Verification

- `.venv/bin/python -m pytest -q` -> **407 passed** (floor 405, +2 new)
- `.venv/bin/ruff check src tests` -> clean
- `innerHTML` occurrences in the template: **0** (unchanged)
- the forbidden stringified-missing-value literal in the template: **0**;
  `tests/test_auth.py`'s whole-document check green
- `git diff` on the frozen set (`mcp_server.py`, `evals.py`, `evals.yml`): empty
- `git status --short` shows neither the PDF nor the WAL sidecars; the PDF is still on
  disk at `Relay dashboard.pdf`
- The zero-data branches (`!series.length || series.every(...)`, `!points.length`) are
  untouched and still asserted by `test_charts_have_an_empty_state`

## Issues Encountered

The over-broad `Math.max` regex described above — caught by running the test before
trusting it, fixed rather than reported as a pass.

## Notes for the next reader

The dashboard now has three chart states, not two: no runs at all, runs that cannot form
a line, and runs that can. Anything added to the latency chart should keep all three, and
anything that changes when a segment is drawn must go through `adjacent` so the caption
follows it.

STATE.md and ROADMAP.md were deliberately not touched — the orchestrator owns those.

---
*Quick task: 20260813-dashboard-sparse-states*
*Completed: 2026-08-13*

## Self-Check: PASSED

All five touched files exist on disk; all three task commits (`d21cfc6`, `32aa4e6`,
`c6ff4e9`) are in the branch history.

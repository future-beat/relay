---
phase: 06-dashboard-experience
plan: 02
subsystem: telemetry
tags: [sqlite, metrics, aggregation, percentiles, dashboard, disclosure]

requires:
  - phase: 06-dashboard-experience
    plan: 01
    provides: settings.metrics_window_days
  - phase: 05-run-event-persistence-live-feed
    provides: runs.run_uid, WR-10's explicit-column discipline
provides:
  - "telemetry.run_metrics: totals, global p50/p95, outcome_distribution, dense daily series and a bounded last_runs, all from SQL"
  - "telemetry._percentile: half-up nearest rank, the codebase's single definition of median"
  - "run_uid published on /metrics last_runs — the drill-down's correlation token (D-01/D-03)"
  - "TOTALS_SQL / OUTCOMES_SQL / GLOBAL_PERCENTILE_SQL / OUTCOME_DISTRIBUTION_SQL / DAILY_BUCKETS_SQL / WINDOW_DAYS_SQL / LAST_RUNS_SQL as named, greppable constants"
affects: [06-04 dashboard template charts, 06-05 try-it drill-down link]

tech-stack:
  added: []
  patterns:
    - "Aggregation stays in SQL; Python only shapes the response and densifies"
    - "Named module-level SQL constants, following ratelimit.DAILY_SPEND_SQL"
    - "Time arithmetic done by SQLite's clock on both the write and the read side"

key-files:
  created:
    - tests/test_metrics.py
  modified:
    - src/relay/telemetry.py
    - tests/test_run_events.py

key-decisions:
  - "_percentile stays in the module even though production percentiles now come from SQL: it is the Python statement of the rounding the SQL must match, and test_percentile_is_half_up asserts the two agree for every sampled (n, pct) pair"
  - "Empty days carry p50_ms/p95_ms None rather than 0 — a zero plots a spike to the floor and reads as 'every run was instant on Tuesday'"
  - "The daily window's bound is asserted against DAILY_BUCKETS_SQL directly (rows + EXPLAIN QUERY PLAN), not through run_metrics, because densification masks an unbounded read"
  - "outcome_distribution is added alongside the raw `outcomes` map rather than replacing it: nothing reads `outcomes` on the page, but it is published shape"

patterns-established:
  - "Backdating fixture: record_run cannot set created_at, so tests seed through an explicit datetime('now', ?) offset built from SQLite's clock"

requirements-completed: [DASH-02, DASH-04]

duration: 42min
completed: 2026-08-12
---

# Phase 6 Plan 02: /metrics as a Data API Summary

**`/metrics` now answers entirely from SQL — totals, one half-up definition of median shared by the card and the chart, a seven-bucket outcome distribution, a dense window-bounded daily series, and a `LIMIT 20` last-runs page carrying `run_uid` again as a deliberate, now-safe reversal of WR-10.**

## Performance

- **Duration:** ~42 min
- **Tasks:** 3 of 3
- **Files modified:** 3 (1 created)
- **Commits:** 3 implementation + 1 docs

## What Was Built

### Task 1 — SQL totals, half-up percentiles, bounded `last_runs`, `run_uid` restored (`cee5c24`)

`run_metrics` no longer materialises every row of `runs`. It runs four statements:
`TOTALS_SQL` (COUNT/COALESCE SUM/MAX), `OUTCOMES_SQL` (the raw map), `GLOBAL_PERCENTILE_SQL`
(the ranked CTE, once per percentile with `pct` bound as a parameter), and `LAST_RUNS_SQL`
(`ORDER BY id DESC LIMIT 20` over the explicit column tuple). The Python slice
`rows[-20:][::-1]` — the last unbounded read on a route that is ungated and polled every
5 s per tab — is gone.

`_percentile`'s index changed from `round(...)` (banker's) to
`min(n - 1, math.floor(pct * (n - 1) + 0.5))` (half-up), matching SQLite's `ROUND` in the
percentile SQL. Research measured the two disagreeing on 16 of 177 sampled `(n, pct)`
pairs; shipping both would have put a different p50 on the card and on the chart line.

`run_uid` is back in `_PUBLIC_RUN_COLUMNS`, with the comment rewritten to state why the
reversal is sound rather than to hide it: the drill-down the uid opens is public and
server-redacted (D-01/D-03), and its full-fidelity branch keys off `tickets.origin`, read
server-side. The explicit tuple — WR-10's actual mechanism — is untouched.

### Task 2 — `outcome_distribution` (`0349208`)

`OUTCOME_DISTRIBUTION_SQL` is a `CASE ... GROUP BY bucket` over the closed set of outcome
strings the single `record_run` call site can write, with the two specific error branches
ahead of `LIKE 'error:%'` (SQLite evaluates `CASE WHEN` in source order). The result is
overlaid onto a zero-filled dict of the seven bucket names, so the bar chart has a defined
shape before the first run. An outcome string added at the call site without a branch here
falls through to `incomplete` — wrong but visible, not dropped.

### Task 3 — the dense daily series (`6d812d7`)

`DAILY_BUCKETS_SQL` buckets by `date(created_at)` with `PARTITION BY` window functions for
per-day p50/p95, using a rank expression character-for-character identical to the global
one. The `WHERE created_at >= datetime('now', ?, 'start of day')` is parameterised on
`settings.metrics_window_days` and is served by `idx_runs_created_at` (asserted via
`EXPLAIN QUERY PLAN`). `WINDOW_DAYS_SQL` generates the window's day labels with a recursive
CTE off SQLite's clock — not Python's `datetime` — so the last bucket cannot belong to the
wrong day under a non-UTC process. `_daily_series` maps the query rows onto that list,
filling absent days with `runs=0 / cost_usd=0.0 / p50_ms=None / p95_ms=None`.

## Mutation Testing

Every named mutation was applied, run, confirmed, and restored.

| # | Mutation | Test | Result |
|---|----------|------|--------|
| M1 | `LAST_RUNS_SQL` → plain `SELECT * FROM runs` | `test_metrics_publishes_exactly_these_columns` | **GREEN — see honesty note below** |
| M1b | `SELECT *, outcome AS internal_note FROM runs` | same | RED — `AssertionError` on the exact key set |
| M2 | `_percentile` index back to `round(pct * (n - 1))` | `test_percentile_is_half_up` | RED — `_percentile is not half-up at n=2, pct=0.5` |
| M3a | drop `LIMIT 20` | `test_last_runs_is_bounded_and_newest_first` | RED — `assert 25 == 20` |
| M3b | drop `DESC` | same | RED — `assert [1, 2, 3, ...] == [20, 19, 18, ...]` |
| M4 | move `LIKE 'error:%'` above the two specific error branches | `test_outcome_distribution_buckets_every_outcome` | RED — `{'error': 7} != {'error': 5}`, `budget_exceeded` 0 vs 1 |
| M5 | drop the zero-fill, return query rows only | `test_outcome_distribution_is_zero_filled_when_empty` | RED — `assert {} == {...7 buckets...}` |
| M6 | daily rank → `CAST(? * n AS INTEGER)` | `test_daily_percentiles_match_the_oracle` | RED — `p50 mismatch at -2: 380 != 3994` |
| M7 | return the daily query's rows without densifying | `test_daily_series_is_dense_and_empty_safe`, `..._on_an_empty_database` | RED — `assert 2 == 14`, `assert 0 == 14` |
| M8 | daily `WHERE` → tautology (`? IS NOT NULL`) | `test_the_window_bounds_the_chart_not_the_ledger` | RED **after the test was strengthened** — see below |

### Honesty note 1 — M1 does not prove what the plan expected

The plan named "restore `SELECT * FROM runs`" as the mutation for the restated WR-10 test.
**It does not turn the test red, and cannot today**: now that `run_uid` is back on the
tuple, `_PUBLIC_RUN_COLUMNS` is *exactly* the eleven columns of the `runs` table, so
`SELECT *` and the explicit list return an identical key set. The mutation is vacuous by
arithmetic, not by weak assertion.

What the test actually guards is T-06-06 — a *new* column arriving silently — so M1b was
run instead: `SELECT *, outcome AS internal_note`, which is precisely what `SELECT *`
becomes the moment somebody adds a column to `runs`. That is red. The exact-key-set
assertion is doing real work; the plan's chosen mutation just happened to sit in the one
window where the two queries coincide. This is worth carrying forward: if a future phase
removes a column from the tuple without removing it from the table, M1 becomes meaningful
again.

### Honesty note 2 — M8 was green until the test was fixed

`test_the_window_bounds_the_chart_not_the_ledger` was first written to read
`run_metrics()["daily"]`. Under M8 it stayed green: densification maps the SQL rows onto
the window's day list, so an out-of-window day is discarded in Python and the payload looks
identical. The read would have gone unbounded (T-06-07's exact failure) with a passing
test — the ninth-vacuous-test pattern, caught by running the mutation rather than assuming
it.

The test now asserts `DAILY_BUCKETS_SQL` **directly**: that its rows contain only the
in-window day, and that its `EXPLAIN QUERY PLAN` contains `idx_runs_created_at`. Both are
red under M8. The `run_metrics`-level assertions were kept as the ledger-vs-chart half.

### Honesty note 3 — what is a regression guard, not proof

- `test_totals_are_sql_aggregates`, `test_percentile_of_nothing_is_zero`,
  `test_metrics_window_days_is_a_setting` and `test_daily_series_on_an_empty_database` are
  **regression guards**, not proofs of a mechanism. They would each also pass against a
  competent Python implementation. They exist to pin the response shape and the empty
  state, which is what the dashboard template will code against.
- `test_unknown_outcome_lands_visibly_in_incomplete` proves the `ELSE` branch's behaviour
  but not that anyone will notice; the visibility claim rests on the chart, not on this test.

## Deviations from Plan

**1. [Rule 1 — Bug] `test_the_window_bounds_the_chart_not_the_ledger` was vacuous as specified**

- **Found during:** Task 3 mutation testing
- **Issue:** The plan's behaviour spec ("assert it is absent from the series but still
  counted in the totals") is satisfied by densification alone, so the `WHERE` — the entire
  T-06-07 mitigation — was untested.
- **Fix:** The test now asserts the query's own rows and its query plan, in addition to the
  `run_metrics`-level claim.
- **Files modified:** `tests/test_metrics.py`
- **Commit:** `6d812d7`

**2. [Rule 2 — Missing critical coverage] `test_percentile_is_half_up` extended to the real SQL string**

- **Found during:** Task 1
- **Issue:** As specified the test only checked Python against a Python formula, which
  cannot catch the SQL drifting away from it — and the whole point of the change is that
  the two agree.
- **Fix:** The test executes `GLOBAL_PERCENTILE_SQL` itself over an in-memory table for
  every sampled `(n, pct)` pair and counts disagreements (asserted zero).
- **Files modified:** `tests/test_metrics.py`
- **Commit:** `cee5c24`

**3. [Housekeeping] Fixture seed order changed**

The oracle fixture draws all per-day counts before any durations, so the asserted `n=0` and
`n=1` coverage does not silently disappear when the duration draw count changes.

## Threat Model Follow-through

| Threat ID | Disposition | How it landed |
|-----------|-------------|---------------|
| T-06-05 (run_uid disclosure) | mitigate | Reversal made deliberate and documented in `_PUBLIC_RUN_COLUMNS`' comment; the restated test asserts the value arrives. The load-bearing redaction proof remains 06-04's leak test — **this plan does not prove the drill-down is safe, only that the uid is published on purpose** |
| T-06-06 (silent new column) | mitigate | Exact-key-set assertion kept and proven red by M1b |
| T-06-07 (unbounded reads) | mitigate | `LIMIT 20`, index-pruned daily `WHERE` (query plan asserted), all aggregates in SQL |
| T-06-SC | accept | No packages installed |

## Verification

- `.venv/bin/python -m pytest -q` → **365 passed** (floor was 345; the surplus includes the
  concurrent 06-03 executor's work landing in the same tree)
- `.venv/bin/ruff check src/relay/telemetry.py tests/test_metrics.py tests/test_run_events.py tests/test_observability.py` → clean
- Empty-state payload carries every key: `runs` 0, `outcomes` `{}`, `outcome_distribution`
  seven zeros, `tokens` 0/0, `cost_usd` 0.0/0.0, `latency_ms` 0/0/0, `daily` 14 empty days,
  `last_runs` `[]`
- `grep -c "SELECT \*" src/relay/telemetry.py` → 0; `run_uid` → 6; `LIMIT` on last_runs → present;
  `outcome_distribution` → 2; `metrics_window_days` → 2; `PARTITION BY` → 2
- Frozen files unchanged: `src/relay/mcp_server.py`, `src/relay/evals.py`, `.github/workflows/evals.yml`
- Files owned by the concurrent 06-03 executor (`src/relay/events.py`, `retrieval.py`,
  `agent.py`, `tests/test_dashboard.py`) appear in none of this plan's three commits

## Known Stubs

None.

## Deferred Issues

- `src/relay/main.py:412`'s comment ("run_metrics does SELECT * FROM runs, the one read here
  that grows unbounded") is now false. `main.py` belongs to 06-04/06-05; logged in
  `deferred-items.md` rather than edited from inside a parallel wave.
- `ruff check tests` currently reports `F821 project_run_detail` in `tests/test_dashboard.py`
  — the concurrent 06-03 executor's in-flight work, not a regression from this plan.

## Self-Check: PASSED

- `src/relay/telemetry.py` — FOUND (modified)
- `tests/test_metrics.py` — FOUND (created)
- `tests/test_run_events.py` — FOUND (modified)
- Commits `cee5c24`, `0349208`, `6d812d7` — all FOUND in `git log`

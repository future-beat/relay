# Phase 6 deferred items

Out-of-scope discoveries logged during execution, not fixed by the discovering plan.

## From 06-02 (metrics aggregation)

- **~~`src/relay/main.py:412` comment is now stale.~~ Done — that comment is gone.**
  What was wrong was this note's own reasoning for closing it: "every read is
  aggregated or bounded (`LIMIT 20`, the daily `WHERE`)". Only the star select was
  fixed; the reads were not. Corrected against `EXPLAIN QUERY PLAN` (WR-03):

  | query | plan | bounded? |
  |---|---|---|
  | `TOTALS_SQL` | `SCAN runs` | no |
  | `OUTCOMES_SQL` | `SCAN runs` + temp b-tree GROUP BY | no |
  | `OUTCOME_DISTRIBUTION_SQL` | `SCAN runs` + two temp b-trees | no |
  | `WINDOW_PERCENTILE_SQL` (×2/request) | `SEARCH runs USING INDEX idx_runs_created_at` + temp b-tree ORDER BY | to the window only |
  | `LAST_RUNS_SQL` | `SCAN runs`, reverse rowid, no ORDER BY b-tree, `LIMIT 20` | yes |
  | `DAILY_BUCKETS_SQL` | `SEARCH runs USING INDEX idx_runs_created_at` | yes |

  Three of the six read every row and grow with the volume. `WINDOW_PERCENTILE_SQL`
  gained its `WHERE` from WR-09 (so the p50 card and the p50 chart are one statistic),
  not as a cost control — the bound is a side effect and would disappear with the
  display decision that produced it.

- **`/metrics` has no perimeter and these reads hold the DB lock (WR-03, deferred).**
  The route carries no `dependencies=` and is polled every 5s per open tab. All six
  queries run in one `asyncio.to_thread` holding `Database`'s process-wide lock, which
  is the same lock `RunRecorder.record` needs once per agent step. Accepted risk while
  the volume is small; the fix is a `_gate("metrics", public=True)` bucket plus a bound
  on the three scans. The plan table above is pinned by
  `tests/test_metrics.py::test_the_metrics_query_plans_are_what_this_comment_says`, so
  this note goes red rather than stale if the queries change.

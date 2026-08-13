# Phase 6 deferred items

Out-of-scope discoveries logged during execution, not fixed by the discovering plan.

## From 06-02 (metrics aggregation)

- **`src/relay/main.py:412` comment is now stale.** It reads "run_metrics does
  SELECT * FROM runs, the one read here that grows unbounded". As of 06-02 that is
  false on both counts: the columns are explicit and every read is aggregated or
  bounded (`LIMIT 20`, the daily `WHERE`). `main.py` belongs to 06-04/06-05, which
  are already editing that file; whichever of them lands next should correct the
  comment to describe the current read. Not fixed here to avoid a cross-plan
  conflict inside a parallel wave.

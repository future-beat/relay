"""Phase 4 observability: structured logs, OpenTelemetry tracing, run metrics.

- configure_logging() switches the app to single-line JSON logs.
- setup_tracing() installs a TracerProvider; spans export over OTLP when
  OTEL_EXPORTER_OTLP_ENDPOINT is set, otherwise tracing is a cheap no-op.
- record_run()/run_metrics() persist one row per agent run and aggregate
  token/cost/latency stats for the /metrics endpoint.
"""

import json
import logging
import math
import os
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import settings
from .db import Database


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "ctx", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn's access log duplicates our request context; keep errors only
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def setup_tracing(service_name: str = "relay") -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def record_run(
    conn: Database,
    *,
    ticket_id: int,
    model: str,
    duration_ms: int,
    steps: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    outcome: str,
    # Optional so every pre-phase-5 caller (evals, the MCP path, direct test callers)
    # keeps working; legacy rows and un-stamped runs simply store NULL.
    run_uid: str | None = None,
) -> None:
    # One statement, but the commit is what matters: a bare commit() is connection-scoped
    # and would land another request's half-finished write alongside this row.
    # Stays synchronous — this runs in event_stream's finally, where an extra suspension
    # point is a risk with no measurable payoff (one INSERT, tens of microseconds).
    with conn.transaction():
        conn.execute(
            "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
            " output_tokens, cost_usd, outcome, run_uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                model,
                duration_ms,
                steps,
                input_tokens,
                output_tokens,
                cost_usd,
                outcome,
                run_uid,
            ),
        )


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile, half-up — this codebase's one definition of "median".

    Half-up, not Python's `round()`, which is banker's rounding. The percentiles on
    /metrics are now computed by SQL (below), and SQLite's `ROUND` is half-up: ship
    both roundings and the p50 card and the p50 line on the latency chart show
    different numbers for the same runs, on a page whose entire purpose is looking
    credible. Research measured the two disagreeing on 16 of 177 sampled (n, pct)
    pairs, so this is not a theoretical tie-break.

    Its production role is now to be that statement of the definition in Python:
    tests/test_metrics.py::test_percentile_is_half_up asserts this function and
    WINDOW_PERCENTILE_SQL agree for every sampled pair, which is what stops the two
    from drifting apart the next time either is touched.
    """
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, math.floor(pct * (len(sorted_values) - 1) + 0.5))
    return sorted_values[index]


# The public shape of /metrics, named rather than taken from a star select (WR-10). The
# star meant every column of `runs` was published, so phase 5's `run_uid` joined the
# payload the moment the column was added — an undecided disclosure, made silently.
# The explicit tuple is what makes the NEXT column somebody adds to `runs` a decision
# taken here rather than a silent change to a public API.
#
# `run_uid` is on the list deliberately, reversing WR-10's removal. WR-10 was right
# while the uid was a handle into `run_events`' raw payloads with no decided access
# model; phase 6 decided that model (D-01/D-03): the drill-down the uid opens is
# public and server-redacted, and its full-fidelity branch keys off `tickets.origin`,
# read server-side and unreachable from the request. Holding the uid therefore grants
# nothing that is not already redacted — and it is the one thing that makes a row in
# the last-runs table clickable.
_PUBLIC_RUN_COLUMNS = (
    "id", "ticket_id", "model", "duration_ms", "steps",
    "input_tokens", "output_tokens", "cost_usd", "outcome", "created_at",
    "run_uid",
)


# --- /metrics aggregation ---------------------------------------------------------
# Named module-level constants, following ratelimit.DAILY_SPEND_SQL's precedent: these
# run on an ungated route polled every 5s per open tab, so they are the queries you
# want to be able to grep for.
#
# WHAT THEY ACTUALLY COST. Measured with EXPLAIN QUERY PLAN against a populated `runs`,
# and pinned by test_the_metrics_query_plans_are_what_this_comment_says so the two
# cannot drift. This block used to claim every read here was an aggregation that had
# replaced a full materialisation; three of them still read every row, and a comment
# asserting a bound the code does not have is worse than no comment, because the next
# reader will not re-measure (WR-03):
#
#   TOTALS_SQL                SCAN runs
#   OUTCOMES_SQL              SCAN runs + temp b-tree for the GROUP BY
#   OUTCOME_DISTRIBUTION_SQL  SCAN runs + temp b-trees for the GROUP BY and ORDER BY
#   WINDOW_PERCENTILE_SQL     SEARCH runs USING INDEX idx_runs_created_at, then a temp
#                             b-tree ORDER BY over the window — TWICE per request (p50
#                             and p95 are two calls)
#   LAST_RUNS_SQL             SCAN runs, but in reverse rowid order with no ORDER BY
#                             b-tree, so LIMIT 20 stops it after 20 rows — bounded
#   DAILY_BUCKETS_SQL         SEARCH runs USING INDEX idx_runs_created_at — bounded to
#                             settings.metrics_window_days
#
# So: the first three grow with the table for the life of the Fly volume. The percentile
# pair is bounded to the metrics window — but that WHERE is there because the card and
# the chart have to be one statistic (WR-09), not as a cost control, and removing it for
# a display reason would silently un-bound this read too.
#
# All six run inside one asyncio.to_thread (main.py) holding Database's process-wide
# lock for the whole read, and /metrics carries no rate-limit dependency at all — so an
# unauthenticated `while :; do curl $H/metrics; done` is a lock-holding loop competing
# with every in-flight run's per-event write. Bounding the three scans and metering the
# route are DEFERRED, not done: see
# .planning/phases/06-dashboard-experience/deferred-items.md.

TOTALS_SQL = (
    "SELECT COUNT(*)                        AS runs,"
    "       COALESCE(SUM(input_tokens), 0)  AS input_tokens,"
    "       COALESCE(SUM(output_tokens), 0) AS output_tokens,"
    "       COALESCE(SUM(cost_usd), 0.0)    AS cost_usd,"
    "       COALESCE(MAX(duration_ms), 0)   AS max_ms"
    " FROM runs"
)

# The raw outcome strings, kept alongside outcome_distribution: it is one cheap
# GROUP BY, it is part of /metrics' published shape, and nothing on the page reads it.
OUTCOMES_SQL = "SELECT outcome, COUNT(*) AS n FROM runs GROUP BY outcome"

# Nearest-rank percentile in SQL, over THE SAME ROWS the latency chart plots (WR-09).
#
# The `WHERE` is that agreement, and it is not an optimisation. Without it this query
# was every run for the life of the volume while DAILY_BUCKETS_SQL was bounded to
# metrics_window_days: 100 runs at 5000ms three weeks ago plus 3 runs at 200ms today put
# a "p50 ms" card reading 5000 directly above a latency chart whose only plotted point
# sat at 200. Two numbers labelled p50, contradicting each other, on a page whose whole
# premise is that what it shows is real. Sharing the rank EXPRESSION was never enough —
# a statistic is its population as much as its formula, so the population is shared too.
#
# `MIN(n - 1, ...)` is the two-argument scalar min; `MAX(CASE ...)` is the aggregate
# picking the single ranked row out. An empty window -> one row of NULL, which the
# caller floors to 0. The `? * (n - 1)` rank must stay identical to _percentile's index
# and to DAILY_BUCKETS_SQL's, and test_percentile_is_half_up pins that.
#
# Parameters are bound in the order the placeholders appear in the TEXT: the window
# offset (inside the CTE) first, then the percentile.
WINDOW_PERCENTILE_SQL = (
    "WITH ranked AS ("
    " SELECT duration_ms,"
    "        ROW_NUMBER() OVER (ORDER BY duration_ms) AS rn,"
    "        COUNT(*)     OVER ()                     AS n"
    " FROM runs"
    " WHERE created_at >= datetime('now', ?, 'start of day'))"
    " SELECT MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(? * (n - 1)) AS INTEGER))"
    "                 THEN duration_ms END) AS value"
    " FROM ranked"
)

# DASH-02's distribution, as a GROUP BY over a closed bucket mapping.
#
# The two specific error branches MUST precede the `LIKE 'error:%'` branch: SQLite
# evaluates CASE WHEN in source order, so reordering them silently collapses
# budget_exceeded and step_limit into `error` — the chart still renders, with two bars
# quietly at zero.
#
# The branch list is derived from the single `record_run` call site (main.py), which is
# the only place `runs.outcome` is written. A new outcome string added there without a
# branch here falls through to `incomplete`: wrong, but visible on the chart, rather
# than dropped.
OUTCOME_DISTRIBUTION_SQL = (
    "SELECT CASE"
    "   WHEN outcome = 'send_reply'               THEN 'resolved'"
    "   WHEN outcome = 'create_escalation'        THEN 'escalated'"
    "   WHEN outcome = 'dry_run_complete'         THEN 'dry_run'"
    "   WHEN outcome = 'error:budget_exceeded'    THEN 'budget_exceeded'"
    "   WHEN outcome = 'error:step_limit_reached' THEN 'step_limit'"
    "   WHEN outcome LIKE 'error:%'               THEN 'error'"
    "   ELSE 'incomplete'"
    " END AS bucket, COUNT(*) AS n"
    " FROM runs GROUP BY bucket ORDER BY n DESC, bucket"
)

# Zero-filled first, then overlaid with the query's rows: only buckets with runs come
# back from SQL, and a bar chart that grows its bars one at a time as outcomes first
# occur reads as a broken chart rather than as an honest empty state.
_OUTCOME_BUCKETS = (
    "resolved", "escalated", "dry_run", "incomplete",
    "budget_exceeded", "step_limit", "error",
)

LAST_RUNS_LIMIT = 20

# ORDER BY id DESC LIMIT 20 in SQL, not `rows[-20:][::-1]` in Python: the slice
# materialised every row of `runs` to keep twenty of them.
#
# It is NOT, as this comment used to say, "the last unbounded read left on this route"
# (WR-03) — TOTALS_SQL, OUTCOMES_SQL and OUTCOME_DISTRIBUTION_SQL all still scan the
# whole table, and the header block above measures them. What makes THIS one bounded is
# that `id` is the rowid, so `ORDER BY id DESC` is a backwards b-tree walk with no sort
# step and the LIMIT stops it after twenty rows.
LAST_RUNS_SQL = (
    f"SELECT {', '.join(_PUBLIC_RUN_COLUMNS)} FROM runs"
    f" ORDER BY id DESC LIMIT {LAST_RUNS_LIMIT}"
)


# DASH-04 / D-10: cost and latency bucketed by day, not per run — legible at 3 runs
# and at 300. The rank expression is character-for-character the one in
# WINDOW_PERCENTILE_SQL AND the `WHERE` is the same window, so the chart's p50 and the
# card's p50 are the same statistic over the same rows — the card partitioned by nothing
# where the chart partitions by day (WR-09: the shared expression alone left two numbers
# labelled p50 free to disagree by tens of seconds).
#
# The `WHERE` is what keeps this bounded, and it is served by idx_runs_created_at
# (db.py:83); `date(created_at)` in the GROUP BY is then a function over already-pruned
# rows. The offset is a parameter rather than an interpolated literal so the window is
# configuration (settings.metrics_window_days), not a number buried in a query.
DAILY_BUCKETS_SQL = (
    "WITH windowed AS ("
    " SELECT date(created_at) AS day, duration_ms, cost_usd,"
    "        ROW_NUMBER() OVER (PARTITION BY date(created_at) ORDER BY duration_ms) AS rn,"
    "        COUNT(*)     OVER (PARTITION BY date(created_at))                      AS n"
    " FROM runs"
    " WHERE created_at >= datetime('now', ?, 'start of day'))"
    " SELECT day,"
    "        n                              AS runs,"
    "        ROUND(SUM(cost_usd), 4)        AS cost_usd,"
    "        MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(0.50 * (n - 1)) AS INTEGER))"
    "                 THEN duration_ms END) AS p50_ms,"
    "        MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(0.95 * (n - 1)) AS INTEGER))"
    "                 THEN duration_ms END) AS p95_ms"
    " FROM windowed GROUP BY day, n ORDER BY day"
)

# The window's day labels, generated by SQLite rather than Python's datetime: the rows
# are stamped by datetime('now') (UTC) and matched by datetime('now', ...), so a
# Python-side date would drift with the process timezone and the last bucket of the
# chart would silently belong to the wrong day — db.py:258-264's reasoning, from the
# read side.
WINDOW_DAYS_SQL = (
    "WITH RECURSIVE window_days(day) AS ("
    " SELECT date('now', ?)"
    " UNION ALL"
    " SELECT date(day, '+1 day') FROM window_days WHERE day < date('now'))"
    " SELECT day FROM window_days"
)


def _daily_series(conn: Database) -> list[dict[str, Any]]:
    """Cost and latency per day across the window, with no gaps.

    Densifying is done here rather than on the client because SQL only returns days
    that have runs: a quiet Tuesday comes back as a *missing* point, which a line
    chart draws straight through — the graph then says "nothing happened" and "it was
    busy" with the same picture. Filling server-side makes the chart a plain map on
    the client and makes an idle day visibly idle.

    Empty days carry runs=0 and cost_usd=0.0 (both true) but p50/p95 None, not 0: a
    zero would plot a spike down to the floor and read as "every run was instant".
    """
    offset = _window_offset()
    rows = {
        r["day"]: {
            "day": r["day"],
            "runs": int(r["runs"]),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "p50_ms": None if r["p50_ms"] is None else int(r["p50_ms"]),
            "p95_ms": None if r["p95_ms"] is None else int(r["p95_ms"]),
        }
        for r in conn.execute(DAILY_BUCKETS_SQL, (offset,)).fetchall()
    }
    days = [r["day"] for r in conn.execute(WINDOW_DAYS_SQL, (offset,)).fetchall()]
    return [
        rows.get(day, {"day": day, "runs": 0, "cost_usd": 0.0, "p50_ms": None, "p95_ms": None})
        for day in days
    ]


def _window_days() -> int:
    return max(1, settings.metrics_window_days)


def _window_offset() -> str:
    """The window, as SQLite's own modifier — computed ONCE for every query that uses it.

    The chart and the percentile cards have to be bounded by the same expression, not by
    two expressions that happen to agree today (WR-09). A second `f"-{n} days"` written
    beside the other query is exactly how the two populations drifted apart the first
    time, and an off-by-one between them would be invisible on the page.
    """
    return f"-{_window_days() - 1} days"


def _sql_percentile(conn: Database, pct: float) -> int:
    row = conn.execute(WINDOW_PERCENTILE_SQL, (_window_offset(), pct)).fetchone()
    value = row["value"] if row is not None else None
    return int(value) if value is not None else 0


def run_metrics(conn: Database) -> dict[str, Any]:
    totals = conn.execute(TOTALS_SQL).fetchone()
    n_runs = int(totals["runs"])
    total_cost = float(totals["cost_usd"])
    outcomes = {r["outcome"]: r["n"] for r in conn.execute(OUTCOMES_SQL).fetchall()}
    distribution = dict.fromkeys(_OUTCOME_BUCKETS, 0)
    for row in conn.execute(OUTCOME_DISTRIBUTION_SQL).fetchall():
        distribution[row["bucket"]] = row["n"]
    return {
        "runs": n_runs,
        "outcomes": outcomes,
        "outcome_distribution": distribution,
        "tokens": {
            "input": int(totals["input_tokens"]),
            "output": int(totals["output_tokens"]),
        },
        "cost_usd": {
            "total": round(total_cost, 4),
            "mean_per_run": round(total_cost / n_runs, 4) if n_runs else 0.0,
        },
        # p50 and p95 are over the CHART'S window and say so, because the page prints
        # them side by side and a card that disagrees with the graph under it costs more
        # credibility than a longer label does (WR-09). `max` is deliberately the
        # LIFETIME extreme and is not rendered on any card — it is a ledger fact like
        # cost_usd.total, and windowing it would quietly rewrite what the slowest run
        # ever was. window_days is published so the page can label the two percentiles
        # from the server's own number rather than hardcoding 14 beside a setting.
        "latency_ms": {
            "p50": _sql_percentile(conn, 0.50),
            "p95": _sql_percentile(conn, 0.95),
            "max": int(totals["max_ms"]),
            "window_days": _window_days(),
        },
        "daily": _daily_series(conn),
        "last_runs": [dict(r) for r in conn.execute(LAST_RUNS_SQL).fetchall()],
    }

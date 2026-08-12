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
    GLOBAL_PERCENTILE_SQL agree for every sampled pair, which is what stops the two
    from drifting apart the next time either is touched.
    """
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, math.floor(pct * (len(sorted_values) - 1) + 0.5))
    return sorted_values[index]


# The public shape of /metrics, named rather than taken from `SELECT *` (WR-10). The
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
# want to be able to grep for. Every one of them replaces a Python aggregation over a
# full materialisation of `runs` — a read whose cost grew for the life of the volume.

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

# Nearest-rank percentile in SQL. `MIN(n - 1, ...)` is the two-argument scalar min;
# `MAX(CASE ...)` is the aggregate picking the single ranked row out. Empty table ->
# one row of NULL, which the caller floors to 0. The `? * (n - 1)` rank must stay
# identical to _percentile's index, and test_percentile_is_half_up pins that.
GLOBAL_PERCENTILE_SQL = (
    "WITH ranked AS ("
    " SELECT duration_ms,"
    "        ROW_NUMBER() OVER (ORDER BY duration_ms) AS rn,"
    "        COUNT(*)     OVER ()                     AS n"
    " FROM runs)"
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

# ORDER BY id DESC LIMIT 20 in SQL, not `rows[-20:][::-1]` in Python: the slice was
# the last unbounded read left on this route.
LAST_RUNS_SQL = (
    f"SELECT {', '.join(_PUBLIC_RUN_COLUMNS)} FROM runs"
    f" ORDER BY id DESC LIMIT {LAST_RUNS_LIMIT}"
)


def _sql_percentile(conn: Database, pct: float) -> int:
    row = conn.execute(GLOBAL_PERCENTILE_SQL, (pct,)).fetchone()
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
        "latency_ms": {
            "p50": _sql_percentile(conn, 0.50),
            "p95": _sql_percentile(conn, 0.95),
            "max": int(totals["max_ms"]),
        },
        "last_runs": [dict(r) for r in conn.execute(LAST_RUNS_SQL).fetchall()],
    }

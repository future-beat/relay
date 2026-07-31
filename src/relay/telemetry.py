"""Phase 4 observability: structured logs, OpenTelemetry tracing, run metrics.

- configure_logging() switches the app to single-line JSON logs.
- setup_tracing() installs a TracerProvider; spans export over OTLP when
  OTEL_EXPORTER_OTLP_ENDPOINT is set, otherwise tracing is a cheap no-op.
- record_run()/run_metrics() persist one row per agent run and aggregate
  token/cost/latency stats for the /metrics endpoint.
"""

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


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
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    model: str,
    duration_ms: int,
    steps: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    outcome: str,
) -> None:
    conn.execute(
        "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
        " output_tokens, cost_usd, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, model, duration_ms, steps, input_tokens, output_tokens, cost_usd, outcome),
    )
    conn.commit()


def _percentile(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, round(pct * (len(sorted_values) - 1)))
    return sorted_values[index]


def run_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id").fetchall()]
    durations = sorted(r["duration_ms"] for r in rows)
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    total_cost = sum(r["cost_usd"] for r in rows)
    return {
        "runs": len(rows),
        "outcomes": outcomes,
        "tokens": {
            "input": sum(r["input_tokens"] for r in rows),
            "output": sum(r["output_tokens"] for r in rows),
        },
        "cost_usd": {
            "total": round(total_cost, 4),
            "mean_per_run": round(total_cost / len(rows), 4) if rows else 0.0,
        },
        "latency_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": durations[-1] if durations else 0,
        },
        "last_runs": rows[-20:][::-1],
    }

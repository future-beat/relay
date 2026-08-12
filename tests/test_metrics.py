"""Phase 6: the /metrics SQL-aggregation surface (DASH-02, DASH-04).

Separate from tests/test_observability.py on purpose. test_observability.py covers
phase 4's question — "does a run get recorded and do the cards have numbers" — and
tests/test_run_events.py is phase 5's 2100-line file about persistence. What is
tested here is a different claim: that /metrics' numbers are produced by SQL
aggregation over a bounded read, that the daily series the charts plot is dense and
window-bounded, and that the p50 on the card and the p50 on the chart line are the
same definition of "median". Those are properties of the *queries*, so they get a
file whose fixtures can backdate rows and whose oracles are written in Python.

`record_run` cannot backdate `created_at` (it takes SQLite's `datetime('now')`
default), so every seed here inserts through `_insert_run` with an explicit
`datetime('now', ?)` offset. That is the only reason this file writes raw SQL.
"""

import math
import sqlite3

from relay.config import settings
from relay.telemetry import (
    GLOBAL_PERCENTILE_SQL,
    _percentile,
    run_metrics,
)

# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------

_INSERT_RUN_SQL = (
    "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
    " output_tokens, cost_usd, outcome, run_uid, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))"
)


def _insert_run(
    conn,
    *,
    day_offset: int = 0,
    duration_ms: int = 100,
    cost_usd: float = 0.01,
    outcome: str = "send_reply",
    ticket_id: int = 1,
    run_uid: str | None = None,
) -> None:
    """One `runs` row at a controlled day offset. `+0 days` is today, `-3 days` is Tuesday.

    The offset is applied by SQLite's own clock rather than Python's, matching
    db.py's reasoning: `created_at` is written by `datetime('now')` (UTC), and the
    window predicate compares against `datetime('now', ...)`, so a Python-side
    timestamp would drift with the process timezone and make the window tests lie.
    """
    with conn.transaction():
        conn.execute(
            _INSERT_RUN_SQL,
            (
                ticket_id, "claude-sonnet-5", duration_ms, 1, 10, 5,
                cost_usd, outcome, run_uid, f"{day_offset:+d} days",
            ),
        )


def seed_runs(
    conn,
    *,
    days: int,
    per_day: int,
    duration_ms: int = 100,
    cost_usd: float = 0.01,
    outcome: str = "send_reply",
) -> None:
    """`per_day` runs on each of the most recent `days` days, today included."""
    for offset in range(0, -days, -1):
        for _ in range(per_day):
            _insert_run(
                conn,
                day_offset=offset,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                outcome=outcome,
            )


def _oracle(values: list[int], pct: float) -> int:
    """Nearest-rank, half-up — the one definition of median this codebase has."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.floor(pct * (len(ordered) - 1) + 0.5))]


# ---------------------------------------------------------------------------
# Task 1: one definition of median, bounded last_runs
# ---------------------------------------------------------------------------


def test_percentile_is_half_up():
    """The card's percentile and the chart's percentile must be the same number.

    `_percentile` used Python's `round()` — banker's rounding — while the SQL
    nearest-rank formula uses SQLite's `ROUND`, which is half-up. Research measured
    them disagreeing on 16 of 177 sampled (n, pct) pairs, which on the page reads as
    the p50 card and the p50 chart line quietly contradicting each other. This pins
    both halves: Python matches the half-up formula, AND the real SQL string agrees
    with Python for every sampled pair.

    MUTATION that must turn this red: revert `_percentile`'s index to
    `round(pct * (n - 1))`. Banker's rounding sends every .5 tie to the even index,
    so n=5/pct=0.50 (index 2.0, no tie) still agrees but n=6/pct=0.50 (index 2.5)
    resolves to 2 instead of 3.
    """
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("CREATE TABLE runs (duration_ms INTEGER)")

    disagreements = 0
    for n in range(1, 61):
        values = list(range(n))
        raw.execute("DELETE FROM runs")
        raw.executemany("INSERT INTO runs (duration_ms) VALUES (?)", [(v,) for v in values])
        for pct in (0.50, 0.95, 0.99):
            expected = min(n - 1, math.floor(pct * (n - 1) + 0.5))
            assert _percentile(values, pct) == values[expected], (
                f"_percentile is not half-up at n={n}, pct={pct}"
            )
            from_sql = raw.execute(GLOBAL_PERCENTILE_SQL, (pct,)).fetchone()["value"]
            if from_sql != _percentile(values, pct):
                disagreements += 1
    raw.close()

    assert disagreements == 0, (
        f"{disagreements} (n, pct) pairs where the SQL percentile and _percentile"
        " disagree — the p50 card and the p50 chart would show different numbers"
    )
    # Anti-vacuity: the loop above must actually have exercised the tie cases.
    assert _percentile(list(range(6)), 0.50) == 3, "half-up rounds the 2.5 index up"


def test_percentile_of_nothing_is_zero(conn):
    # The empty state is a defined number, not a crash and not None: the card renders
    # `0 ms` before the first run rather than "undefined".
    assert _percentile([], 0.50) == 0
    assert run_metrics(conn)["latency_ms"] == {"p50": 0, "p95": 0, "max": 0}


def test_last_runs_is_bounded_and_newest_first(conn):
    """/metrics is ungated and polled every 5s per tab; the read must be bounded.

    It used to materialise every row of `runs` and slice `[-20:]` in Python, so the
    cost of the poll grew for the life of the Fly volume.

    MUTATION that must turn this red: drop the `LIMIT 20` (or the `DESC`) from
    LAST_RUNS_SQL — the length assertion catches the first, the ordering the second.
    """
    for i in range(25):
        _insert_run(conn, duration_ms=i)

    payload = run_metrics(conn)

    assert payload["runs"] == 25, "totals count every row, not just the page"
    assert len(payload["last_runs"]) == 20
    ids = [r["id"] for r in payload["last_runs"]]
    assert ids == sorted(ids, reverse=True), "last_runs must be newest first"
    assert ids[0] == 25, "the newest run is the one that is missing when DESC is dropped"
    assert ids[-1] == 6, "exactly the newest 20 — 25 rows minus the page"


def test_totals_are_sql_aggregates(conn):
    for i in range(1, 4):
        _insert_run(conn, duration_ms=i * 100, cost_usd=0.01 * i)

    payload = run_metrics(conn)

    assert payload["runs"] == 3
    assert payload["tokens"] == {"input": 30, "output": 15}
    assert payload["cost_usd"]["total"] == 0.06
    assert payload["cost_usd"]["mean_per_run"] == 0.02
    assert payload["latency_ms"]["max"] == 300
    assert payload["outcomes"] == {"send_reply": 3}


def test_metrics_window_days_is_a_setting():
    # The window is configuration, not a literal buried in the SQL — 06-01 put it in
    # config.py precisely so the chart's span is deployable.
    assert settings.metrics_window_days >= 1

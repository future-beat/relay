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
import random
import sqlite3

from relay.config import settings
from relay.telemetry import (
    DAILY_BUCKETS_SQL,
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


# ---------------------------------------------------------------------------
# Task 2: outcome_distribution (DASH-02)
# ---------------------------------------------------------------------------

# Every string the ONE record_run call site (main.py:292-297, 348, 385-398) can write.
# If a new outcome is added there without a CASE branch here, the last assertion in
# test_outcome_distribution_buckets_every_outcome is what notices.
_ALL_OUTCOMES = {
    "send_reply": "resolved",
    "create_escalation": "escalated",
    "dry_run_complete": "dry_run",
    "incomplete": "incomplete",
    "error:budget_exceeded": "budget_exceeded",
    "error:step_limit_reached": "step_limit",
    "error:api_connection_error": "error",
    "error:api_error": "error",
    "error:model_refusal": "error",
    "error:ended_without_action": "error",
    "error:persistence_failed": "error",
}


def test_outcome_distribution_buckets_every_outcome(conn):
    """Every outcome the agent can record lands in a named bucket, and none is lost.

    MUTATION that must turn this red: in OUTCOME_DISTRIBUTION_SQL, move the
    `WHEN outcome LIKE 'error:%'` branch above the two specific error branches.
    SQLite evaluates CASE WHEN in source order, so `error:budget_exceeded` and
    `error:step_limit_reached` are swallowed by the generic branch — their bars drop
    to zero and the `error` bar silently absorbs them. The chart still renders, which
    is why the counts are asserted per bucket rather than just summed.
    """
    for outcome in _ALL_OUTCOMES:
        _insert_run(conn, outcome=outcome)

    dist = run_metrics(conn)["outcome_distribution"]

    expected: dict[str, int] = dict.fromkeys(set(_ALL_OUTCOMES.values()), 0)
    for bucket in _ALL_OUTCOMES.values():
        expected[bucket] += 1
    assert dist == expected
    assert dist["budget_exceeded"] == 1, "the specific error branches must precede LIKE"
    assert dist["step_limit"] == 1
    assert dist["error"] == 5, "only the unnamed error reasons belong in `error`"
    # Nothing fell into an unnamed bucket, and nothing was double-counted.
    assert sum(dist.values()) == len(_ALL_OUTCOMES) == run_metrics(conn)["runs"]


def test_outcome_distribution_is_zero_filled_when_empty(conn):
    """The bar chart has a stable shape before the first run, not one bar appearing.

    MUTATION: return the query's rows directly instead of overlaying them onto the
    zero-filled bucket dict — on an empty DB the distribution is `{}` and the chart
    reshapes itself on the first run.
    """
    dist = run_metrics(conn)["outcome_distribution"]

    assert dist == {
        "resolved": 0, "escalated": 0, "dry_run": 0, "incomplete": 0,
        "budget_exceeded": 0, "step_limit": 0, "error": 0,
    }
    # The raw map is kept alongside it and is genuinely empty — the zero-fill is a
    # property of the distribution, not of the database.
    assert run_metrics(conn)["outcomes"] == {}


def test_unknown_outcome_lands_visibly_in_incomplete(conn):
    # A future outcome string added at the call site without a branch here does not
    # vanish: it shows up as `incomplete`, which is wrong-but-visible rather than lost.
    _insert_run(conn, outcome="outcome_nobody_added_a_branch_for")

    dist = run_metrics(conn)["outcome_distribution"]

    assert dist["incomplete"] == 1
    assert sum(dist.values()) == 1


def test_metrics_window_days_is_a_setting():
    # The window is configuration, not a literal buried in the SQL — 06-01 put it in
    # config.py precisely so the chart's span is deployable.
    assert settings.metrics_window_days >= 1


# ---------------------------------------------------------------------------
# Task 3: the dense daily series (DASH-04 / D-10)
# ---------------------------------------------------------------------------


def _day_string(conn, offset: int) -> str:
    """The day label SQLite will produce for `offset` days ago, from SQLite's clock.

    Not `datetime.now()`: `created_at` and the window predicate are both UTC via
    SQLite, so a Python-side date would drift with the process timezone and this
    oracle would be comparing against the wrong day.
    """
    return conn.execute("SELECT date('now', ?) AS d", (f"{offset:+d} days",)).fetchone()["d"]


def test_daily_series_is_dense_and_empty_safe(conn):
    """Every day in the window is a point, including the days with no runs.

    MUTATION that must turn this red: return the query's rows straight out of
    run_metrics without densifying. Only days that have runs come back from SQL, so
    the series shrinks to 2 entries — the length assertion fails, and on the page a
    quiet Tuesday would be a missing point that the chart draws straight through
    instead of a visible zero.
    """
    window = settings.metrics_window_days
    _insert_run(conn, day_offset=0, duration_ms=100, cost_usd=0.02)
    _insert_run(conn, day_offset=-3, duration_ms=300, cost_usd=0.05)

    daily = run_metrics(conn)["daily"]

    assert len(daily) == window, "the series is the window, not just the days with runs"
    days = [d["day"] for d in daily]
    assert days == sorted(days), "ascending by day — the chart plots left to right"
    assert len(set(days)) == window, "each day appears exactly once"
    assert days[-1] == _day_string(conn, 0), "the series ends today"
    assert days[0] == _day_string(conn, -(window - 1)), "and starts window-1 days back"

    by_day = {d["day"]: d for d in daily}
    assert by_day[_day_string(conn, 0)] == {
        "day": _day_string(conn, 0), "runs": 1,
        "cost_usd": 0.02, "p50_ms": 100, "p95_ms": 100,
    }
    assert by_day[_day_string(conn, -3)]["runs"] == 1
    assert by_day[_day_string(conn, -3)]["p50_ms"] == 300

    empty = by_day[_day_string(conn, -1)]
    assert empty == {
        "day": _day_string(conn, -1), "runs": 0,
        "cost_usd": 0.0, "p50_ms": None, "p95_ms": None,
    }
    # A day with no runs has no latency — 0 would draw a spike to the floor and read
    # as "every run was instant on Tuesday". None is the chart's gap.
    assert sum(d["runs"] for d in daily) == 2


def test_daily_series_on_an_empty_database(conn):
    window = settings.metrics_window_days
    daily = run_metrics(conn)["daily"]

    assert len(daily) == window, "the chart has an x-axis before the first run"
    assert all(d["runs"] == 0 and d["cost_usd"] == 0.0 for d in daily)
    assert all(d["p50_ms"] is None and d["p95_ms"] is None for d in daily)


def test_daily_percentiles_match_the_oracle(conn):
    """The SQL per-day percentiles equal a half-up nearest-rank Python oracle.

    Randomised over the whole window with a fixed seed so the small-n days — where
    rank formulas differ — are actually hit rather than assumed.

    MUTATION that must turn this red: change the rank expression in DAILY_BUCKETS_SQL
    from `1 + MIN(n - 1, CAST(ROUND(? * (n - 1)) AS INTEGER))` to the common
    off-by-one form `CAST(? * n AS INTEGER)`. Days with small n disagree immediately
    (n=1 gives rank 0, which matches no row, so p50 goes NULL).
    """
    rng = random.Random(20260812)
    window = settings.metrics_window_days
    # Counts are drawn first so the fixture's shape (and the n=0/n=1 coverage asserted
    # below) does not depend on how many durations happened to be drawn before it.
    counts = {offset: rng.randrange(0, 7) for offset in range(0, -window, -1)}
    seeded: dict[int, list[int]] = {}
    for offset, count in counts.items():
        durations = [rng.randrange(50, 5000) for _ in range(count)]
        seeded[offset] = durations
        for ms in durations:
            _insert_run(conn, day_offset=offset, duration_ms=ms, cost_usd=0.01)

    assert any(len(v) == 1 for v in seeded.values()), "the n=1 case must be exercised"
    assert any(len(v) == 0 for v in seeded.values()), "and so must an empty day"

    by_day = {d["day"]: d for d in run_metrics(conn)["daily"]}

    for offset, durations in seeded.items():
        row = by_day[_day_string(conn, offset)]
        assert row["runs"] == len(durations)
        if not durations:
            assert row["p50_ms"] is None and row["p95_ms"] is None
            continue
        assert row["p50_ms"] == _oracle(durations, 0.50), f"p50 mismatch at {offset}"
        assert row["p95_ms"] == _oracle(durations, 0.95), f"p95 mismatch at {offset}"
        assert row["p50_ms"] == _percentile(sorted(durations), 0.50), (
            "the daily chart and the p50 card must be one definition of median"
        )
        assert row["cost_usd"] == round(0.01 * len(durations), 4)


def test_the_window_bounds_the_chart_not_the_ledger(conn):
    """A run older than the window leaves the chart but stays in the totals.

    The `WHERE created_at >= ...` is what keeps this route's cost flat for the life of
    the Fly volume (it is ungated and polled every 5s per tab). It must not also
    quietly rewrite the cost ledger the cards show.

    MUTATION: replace the WHERE in DAILY_BUCKETS_SQL with a tautology. The query is
    asserted DIRECTLY below, not only through run_metrics — densifying the result
    onto the window's day list drops out-of-window days anyway, so a test that only
    read run_metrics()["daily"] would stay green while the read went unbounded. That
    is precisely the shape of vacuous test this project keeps producing, so the query
    plan and the query's own rows are what is asserted.
    """
    window = settings.metrics_window_days
    _insert_run(conn, day_offset=-(window + 6), duration_ms=9999, cost_usd=0.40)
    _insert_run(conn, day_offset=0, duration_ms=100, cost_usd=0.02)

    # The WHERE prunes at the source, and does it through the index (T-06-07: this
    # route is ungated and polled every 5s per tab, so the read must stay flat as the
    # Fly volume fills).
    offset = f"-{window - 1} days"
    from_sql = conn.execute(DAILY_BUCKETS_SQL, (offset,)).fetchall()
    assert [r["day"] for r in from_sql] == [_day_string(conn, 0)], (
        "the out-of-window run reached the daily query — the WHERE is not pruning"
    )
    plan = " ".join(
        r["detail"] for r in
        conn.execute("EXPLAIN QUERY PLAN " + DAILY_BUCKETS_SQL, (offset,)).fetchall()
    )
    assert "idx_runs_created_at" in plan, f"the window fell back to a scan: {plan}"

    payload = run_metrics(conn)

    assert len(payload["daily"]) == settings.metrics_window_days
    assert sum(d["runs"] for d in payload["daily"]) == 1, "the old run is off the chart"
    assert all(d["p95_ms"] != 9999 for d in payload["daily"])
    # ...but it is still money that was spent and a run that happened.
    assert payload["runs"] == 2
    assert payload["cost_usd"]["total"] == 0.42
    assert payload["latency_ms"]["max"] == 9999

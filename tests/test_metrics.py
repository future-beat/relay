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

import json
import math
import random
import re
import sqlite3
from pathlib import Path
from typing import Any

from helpers import FakeClient, response, text_block, tool_use_block
from relay import telemetry
from relay.agent import run_ticket
from relay.config import settings
from relay.db import purge_expired_run_events
from relay.events import RunRecorder
from relay.guardrails import GUARD_NAMES
from relay.telemetry import (
    _DENIED_BY_PATH,
    DAILY_BUCKETS_SQL,
    GUARDRAIL_DENIALS_SQL,
    LAST_RUNS_SQL,
    OUTCOME_DISTRIBUTION_SQL,
    OUTCOMES_SQL,
    TOTALS_SQL,
    WINDOW_PERCENTILE_SQL,
    _percentile,
    _window_offset,
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
    # created_at, because the query is now bounded to the metrics window (WR-09) and
    # every row below is stamped inside it: this test is about the RANK, and a row the
    # WHERE pruned would leave it measuring an empty population.
    raw.execute("CREATE TABLE runs (duration_ms INTEGER, created_at TEXT)")
    offset = _window_offset()

    disagreements = 0
    for n in range(1, 61):
        values = list(range(n))
        raw.execute("DELETE FROM runs")
        raw.executemany(
            "INSERT INTO runs (duration_ms, created_at) VALUES (?, datetime('now'))",
            [(v,) for v in values],
        )
        for pct in (0.50, 0.95, 0.99):
            expected = min(n - 1, math.floor(pct * (n - 1) + 0.5))
            assert _percentile(values, pct) == values[expected], (
                f"_percentile is not half-up at n={n}, pct={pct}"
            )
            from_sql = raw.execute(
                WINDOW_PERCENTILE_SQL, (offset, pct)
            ).fetchone()["value"]
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
    assert run_metrics(conn)["latency_ms"] == {
        "p50": 0, "p95": 0, "max": 0, "window_days": settings.metrics_window_days,
    }


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


def test_the_p50_card_and_the_p50_chart_are_one_population(conn):
    """WR-09: two numbers labelled p50, on a page whose whole premise is credibility.

    The rank EXPRESSION was already shared, and the review's own words are that this
    was not enough: `GLOBAL_PERCENTILE_SQL` had no `WHERE` — it was every run for the
    life of the Fly volume — while `DAILY_BUCKETS_SQL` was bounded to
    `metrics_window_days`. A statistic is its population as much as its formula, so the
    card and the chart could differ by tens of seconds while both were labelled p50, and
    a visitor comparing them (which is the right thing to do) would conclude the page
    was broken.

    The scenario below is the review's, seeded for real: 100 runs at 5000ms outside the
    window plus 3 runs at 200ms today. Before the fix the card read 5000 and the chart's
    only point sat at 200.

    MUTATION (executed): delete the `WHERE created_at >= datetime('now', ?, 'start of
    day')` from WINDOW_PERCENTILE_SQL and drop the offset parameter. The card goes back
    to 5000 while the chart still plots 200, and this reds on the card assertion.
    """
    window = settings.metrics_window_days
    for _ in range(100):
        _insert_run(conn, day_offset=-(window + 6), duration_ms=5000, cost_usd=0.01)
    for _ in range(3):
        _insert_run(conn, day_offset=0, duration_ms=200, cost_usd=0.01)

    payload = run_metrics(conn)

    plotted = [d["p50_ms"] for d in payload["daily"] if d["p50_ms"] is not None]
    assert plotted == [200], f"the chart is not plotting what was seeded: {plotted}"
    assert payload["latency_ms"]["p50"] == 200, (
        "the p50 card is computed over a different population than the p50 line"
    )
    assert payload["latency_ms"]["p95"] == 200, "p95 is windowed on one side only"

    # ANTI-VACUITY: the out-of-window runs really are in the store, really are slower,
    # and really would move an unwindowed percentile. Without this the assertions above
    # would pass on an empty table.
    assert payload["runs"] == 103
    assert payload["latency_ms"]["max"] == 5000, (
        "the slow runs never landed — the card assertion above proves nothing"
    )
    assert _percentile(sorted([5000] * 100 + [200] * 3), 0.50) == 5000, (
        "the unwindowed p50 of this seeding is not 5000, so the fix is untested here"
    )

    # The label the page prints is the same number the WHERE was built from.
    assert payload["latency_ms"]["window_days"] == window


def test_the_p50_card_is_the_windows_median_not_a_day_average(conn):
    """One population, not a reconciliation of two.

    The card could be made to "agree" with the chart by averaging the daily p50s, which
    is a different statistic that happens to look close — and is wrong whenever the days
    hold different numbers of runs. This pins that the card is the median of exactly the
    ROWS the chart's days cover: three runs across two days, whose union median (900)
    equals neither day's p50 (100 and 900) by accident and is not their mean (500).

    MUTATION (executed): compute the card as the mean of the daily p50s. Reds with 500.
    """
    _insert_run(conn, day_offset=0, duration_ms=100)
    _insert_run(conn, day_offset=-1, duration_ms=900)
    _insert_run(conn, day_offset=-1, duration_ms=1000)

    payload = run_metrics(conn)
    by_day = {d["day"]: d["p50_ms"] for d in payload["daily"] if d["p50_ms"] is not None}

    assert sorted(by_day.values()) == [100, 1000], f"the days did not seed as expected: {by_day}"
    assert payload["latency_ms"]["p50"] == _percentile([100, 900, 1000], 0.50) == 900
    # ...and the two wrong-but-plausible answers are excluded by name.
    assert payload["latency_ms"]["p50"] != 550, "the card averages the daily p50s"
    assert payload["latency_ms"]["p50"] != 100, "the card reads only the latest day"


def _plan(conn, sql: str, params: tuple = ()) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " ".join(r["detail"] for r in rows)


def test_the_metrics_query_plans_are_what_this_comment_says(conn):
    """WR-03: the module comment above these queries claimed a bound they do not have.

    It read "Every one of them replaces a Python aggregation over a full materialisation
    of `runs`", and `deferred-items.md` recorded "every read is aggregated or bounded
    (`LIMIT 20`, the daily `WHERE`)". Three of the six still read every row. A stale
    "this is bounded" note is exactly how the next person skips checking, so the comment
    now quotes the measured plans — and this test is what stops that from going stale in
    turn: change a query without changing the comment and this reds.

    /metrics is ungated and polled every 5s per tab, and all six run inside one
    `asyncio.to_thread` holding Database's process-wide lock. Bounding the three scans
    and metering the route are deferred, deliberately; this test asserts the CURRENT
    truth so the deferral stays visible rather than becoming folklore.

    MUTATION (executed): add `WHERE created_at >= datetime('now', '-14 days')` to
    TOTALS_SQL. The scan assertion reds — which is the point: the comment would then be
    describing a query that no longer exists.

    Plan strings are matched with tolerant patterns (`SCAN [TABLE] runs`) because the
    exact wording changed in SQLite 3.36 and the project floor is Python 3.11.
    """
    for i in range(60):
        _insert_run(conn, day_offset=-(i % 40), duration_ms=100 + i)

    scan = re.compile(r"\bSCAN (TABLE )?runs\b")
    seek = re.compile(r"\bSEARCH (TABLE )?runs USING INDEX idx_runs_created_at\b")
    offset = _window_offset()

    # 1. The three that still read every row. Named individually: "some of them scan"
    #    is the claim the old comment blurred.
    for name, sql in (
        ("TOTALS_SQL", TOTALS_SQL),
        ("OUTCOMES_SQL", OUTCOMES_SQL),
        ("OUTCOME_DISTRIBUTION_SQL", OUTCOME_DISTRIBUTION_SQL),
    ):
        plan = _plan(conn, sql)
        assert scan.search(plan), f"{name} is no longer a full scan — fix the comment: {plan}"
        assert not seek.search(plan), f"{name} became bounded — fix the comment: {plan}"

    # 2. The percentile pair: bounded to the window by WR-09, and still sorting it.
    percentile_plan = _plan(conn, WINDOW_PERCENTILE_SQL, (offset, 0.50))
    assert seek.search(percentile_plan), f"the percentile fell back to a scan: {percentile_plan}"
    assert "USE TEMP B-TREE FOR ORDER BY" in percentile_plan, (
        "the percentile stopped sorting — the comment says it does"
    )

    # 3. LAST_RUNS_SQL: a scan by name, bounded in fact, and the reason is the ABSENCE
    #    of a sort step. The control below is what makes that absence mean something —
    #    an equivalent query that cannot use the rowid order does grow the b-tree.
    last_runs_plan = _plan(conn, LAST_RUNS_SQL)
    assert scan.search(last_runs_plan)
    assert "USE TEMP B-TREE FOR ORDER BY" not in last_runs_plan, (
        "ORDER BY id DESC stopped riding the rowid — LIMIT 20 no longer bounds the read"
    )
    control = _plan(conn, "SELECT id FROM runs ORDER BY duration_ms DESC LIMIT 20")
    assert "USE TEMP B-TREE FOR ORDER BY" in control, (
        "this SQLite does not report sort steps at all — the assertion above is vacuous"
    )

    # 4. The comment says all of the above, in those words.
    header = (
        Path(telemetry.__file__).read_text(encoding="utf-8")
        .split("# --- /metrics aggregation", 1)[1]
        .split("TOTALS_SQL = (", 1)[0]
    )
    for claim in (
        "TOTALS_SQL                SCAN runs",
        "OUTCOMES_SQL              SCAN runs",
        "OUTCOME_DISTRIBUTION_SQL  SCAN runs",
        "idx_runs_created_at",
        "bounded",
        "DEFERRED",
    ):
        assert claim in header, f"the module comment no longer states {claim!r}"
    # ...and it does not restate the claim that was false.
    assert "Every one of them replaces" not in header

    # 5. SEC-04's counter, measured with the rest. It scans a DIFFERENT table, and the
    #    header has to say which — "SCAN run_events", not a seventh line under `runs`.
    denials_plan = _plan(conn, GUARDRAIL_DENIALS_SQL, (_DENIED_BY_PATH, _DENIED_BY_PATH))
    assert re.search(r"\bSCAN (TABLE )?run_events\b", denials_plan), (
        f"GUARDRAIL_DENIALS_SQL no longer reads run_events — fix the comment: {denials_plan}"
    )
    assert not scan.search(denials_plan), (
        f"the denial counter started reading `runs` — that is not what it counts: {denials_plan}"
    )
    assert "GUARDRAIL_DENIALS_SQL     SCAN run_events" in header
    assert "bounded by the retention sweep" in header


# ---------------------------------------------------------------------------
# SEC-04: the guardrail denial counter
# ---------------------------------------------------------------------------


def _insert_event(conn, seq: int, type_: str, payload: dict, run_uid: str = "u1") -> None:
    conn.execute(
        "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload) VALUES (?, ?, ?, ?, ?)",
        (run_uid, 1, seq, type_, json.dumps(payload)),
    )
    conn.commit()


def _insert_denial(conn, guard: str | None, *, run_uid: str = "u1", seq: int = 1) -> None:
    """One denial, written the way the agent loop writes it.

    Which means: a `tool_result` row carrying `denied_by`, PLUS — for the two guards
    that announce themselves — the `guardrail` event row that precedes it. `policy`
    gets no event row, because `_execute_guarded` returns its denial without the loop
    ever yielding a guardrail event for it. That asymmetry is the whole reason the
    counter is sourced from the denial rather than from the event stream, so the
    fixture has to reproduce it or the test cannot tell the two sources apart.

    Raw rows rather than a driven run because these cases are about the AGGREGATION —
    an unregistered guard, a payload that is not an object — and several of them cannot
    be produced by the loop as it stands. test_a_binding_denial_increments_the_counter
    is what proves the aggregation reads the rows a REAL denial writes.
    """
    result: Any = {"error": "denied"} if guard is None else {"error": "denied", "denied_by": guard}
    if guard in _GUARDS_THAT_EMIT_AN_EVENT:
        _insert_event(conn, seq, "guardrail", {"guard": guard, "tool": "send_reply",
                                               "action": "denied"}, run_uid)
    _insert_event(conn, seq, "tool_result", {
        "tool": "send_reply", "result": result, "is_error": guard is not None,
    }, run_uid)


# The two guards whose denial the agent loop also yields a `guardrail` event for.
# `policy` is deliberately absent — see _insert_denial.
_GUARDS_THAT_EMIT_AN_EVENT = frozenset({"ticket_binding", "citation"})


async def test_a_binding_denial_increments_the_counter(conn, registry, monkeypatch):
    """SEC-04, the clause the v1.0 audit found unimplemented (F-3).

    The requirement reads: a mismatched model-supplied id produces "a model-visible
    denial event (not a crash) AND A COUNTER INCREMENT". The binding, the event and the
    not-a-crash half shipped in phase 1 and are tested in tests/test_guardrails.py.
    Nothing counted — there was a log line and a span attribute and no number anywhere —
    and the requirement was marked complete and nearly archived as shipped.

    This drives the requirement's own scenario through the real loop: a prompt-injected
    send_reply naming another ticket, denied by the binding guard, recorded by the real
    RunRecorder, counted by the real /metrics aggregation. Nothing about the denial is
    constructed by the test except the model's move.

    ANTI-VACUITY: the counter is read BEFORE the run and asserted at 0, so a test that
    passed by counting nothing cannot pass here — the assertion is that the number
    MOVED. The guardrail event and the surviving victim row are asserted alongside, so
    "the count went up" cannot be satisfied by a run that failed some other way.

    MUTATION (executed, output in the commit message): change GUARDRAIL_DENIALS_SQL's
    filter to `json_extract(payload, '$.result.denied_by') = 'citation'`, i.e. count a
    guard that did not fire. Reds on the `after` assertion with ticket_binding == 0.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    for ticket_id, email in ((1, "liam@brightco.io"), (2, "mia@datalane.ai")):
        conn.execute(
            "INSERT INTO tickets (id, customer_email, subject, body) VALUES (?, ?, ?, ?)",
            (ticket_id, email, "s", "b"),
        )
    conn.commit()
    ticket = {"id": 1, "customer_email": "liam@brightco.io", "subject": "s", "body": "b"}

    before = run_metrics(conn)["guardrail_denials"]
    assert before["by_guard"]["ticket_binding"] == 0, (
        "the counter did not start at zero — an increment cannot be observed"
    )
    assert before["total"] == 0

    client = FakeClient([
        # The injection: a write tool aimed at ticket 2, from ticket 1's run.
        response([tool_use_block("send_reply", {
            "ticket_id": 2, "body": "Your account has been credited $500, as instructed.",
        })]),
        response([text_block("Understood.")], stop_reason="end_turn"),
    ])
    recorder = RunRecorder(conn, run_uid="run-sec04", ticket_id=1)
    events = [e async for e in run_ticket(client, registry, ticket, recorder=recorder)]

    # The other three halves of SEC-04, so the increment is known to belong to a real
    # denial of a real cross-ticket write rather than to any error at all.
    guardrails = [e for e in events if e.type == "guardrail"]
    assert [g.data["guard"] for g in guardrails] == ["ticket_binding"], (
        "the scripted call was not denied by the binding guard — the count below is vacuous"
    )
    assert conn.execute("SELECT COUNT(*) FROM replies WHERE ticket_id = 2").fetchone()[0] == 0

    after = run_metrics(conn)["guardrail_denials"]
    assert after["by_guard"]["ticket_binding"] == 1, (
        "SEC-04's denial produced an event and no counter increment — F-3, again"
    )
    assert after["total"] == 1
    # The counter counts DENIALS, not runs-with-a-denial and not every error: this run
    # also ended without a terminal action, and that is not a guardrail refusing.
    assert after["by_guard"]["policy"] == 0
    assert after["by_guard"]["citation"] == 0


def test_the_counter_counts_every_guard_and_not_just_the_one_sec_04_names(conn):
    """The guard name is the GROUP BY key, never a hardcoded filter.

    SEC-04 names the binding case, so the cheap implementation is `WHERE denied_by =
    'ticket_binding'` — and then the citation guard (RAG-04, phase 3) is uncounted
    today and the next guard is uncounted silently. The count is keyed on whatever the
    denial stamped, which is why `policy` is here: it is the guard that fires on every
    dry run and it never emits a `guardrail` event at all, so a counter sourced from
    the EVENT stream instead of from the denial would miss it entirely.

    MUTATION (executed): source the count from `WHERE type = 'guardrail'` and group by
    `$.guard`. Reds on `policy` ALONE — 3 seeded, 0 counted, while ticket_binding and
    citation still tally correctly because those two do emit an event. That single
    missing guard is the omission the denial-sourced query exists to avoid, and the
    fixture seeds the real event rows so the test can see it as a difference of one
    key rather than as everything collapsing to zero.
    """
    seeded = {"ticket_binding": 2, "policy": 3, "citation": 1}
    seq = 0
    for guard, n in seeded.items():
        for _ in range(n):
            seq += 1
            _insert_denial(conn, guard, seq=seq)
    # Two rows that must NOT be counted: a tool result that was not denied, and one
    # whose result is not an object at all (json_extract yields NULL rather than raising).
    seq += 1
    _insert_denial(conn, None, seq=seq)
    _insert_event(conn, seq + 1, "tool_result", {"tool": "x", "result": "not-an-object"})

    denials = run_metrics(conn)["guardrail_denials"]

    assert denials["by_guard"] == seeded, f"the counter is not counting by guard: {denials}"
    assert denials["total"] == 6
    # Every registered guard has a key even at zero: the point of the number is that the
    # guard is armed, and a missing key on a quiet day says nothing.
    assert set(denials["by_guard"]) == set(GUARD_NAMES)


def test_an_unregistered_guard_is_still_counted(conn):
    """Default-report, the mirror of the redactors' default-deny.

    GUARD_NAMES zero-fills the shape; it must not FILTER it. A guard added to
    _execute_guarded and forgotten in the registry would otherwise refuse tool calls
    that no number on the page ever admits to — the failure mode F-3 was, arriving by a
    different door.

    MUTATION (executed): build the result as
    `{g: counts.get(g, 0) for g in GUARD_NAMES}`, i.e. filter to registered names.
    Reds — the new guard's 2 denials vanish and the total drops to 2.
    """
    _insert_denial(conn, "ticket_binding", seq=1)
    _insert_denial(conn, "a_guard_nobody_registered", seq=2)
    _insert_denial(conn, "a_guard_nobody_registered", seq=3)

    denials = run_metrics(conn)["guardrail_denials"]

    assert denials["by_guard"]["a_guard_nobody_registered"] == 2
    assert denials["total"] == 3, "an unregistered guard's denials went missing from the total"


def test_the_denial_count_is_a_retention_window_and_says_so(conn, monkeypatch):
    """What the number MEANS, pinned: it is not a lifetime total, and it is labelled.

    `run_events` is the one table this service deletes from — purge_expired_run_events
    drops rows past settings.events_retention_days at startup and deliberately spares
    `runs`. So this count is a rolling window sitting beside `runs`-derived lifetime
    totals on the same page, and the only thing stopping a reader adding them up is
    that it publishes its own window.

    MUTATION (executed): hardcode `"retention_days": 30` in _guardrail_denials. Reds
    when the setting is monkeypatched to 7 — the label would then be a constant that
    stops describing the sweep the moment anyone tunes it.
    """
    _insert_denial(conn, "ticket_binding", seq=1)
    _insert_denial(conn, "ticket_binding", seq=2, run_uid="u2")

    assert run_metrics(conn)["guardrail_denials"]["retention_days"] == (
        settings.events_retention_days
    )

    # The sweep really does take rows out from under this count — the window is real,
    # not just a label. Backdate one denial past the window and purge.
    conn.execute(
        "UPDATE run_events SET created_at = datetime('now', ?) WHERE run_uid = 'u2'",
        (f"-{settings.events_retention_days + 1} days",),
    )
    conn.commit()
    # Two rows go: the denial's tool_result and the guardrail event that announced it.
    assert purge_expired_run_events(conn, retention_days=settings.events_retention_days) == 2

    assert run_metrics(conn)["guardrail_denials"]["by_guard"]["ticket_binding"] == 1, (
        "the count did not follow the retention sweep — it is not the window it claims"
    )

    # And the label is READ from the setting rather than written beside it: tune the
    # sweep and the number the page prints follows it.
    monkeypatch.setattr(settings, "events_retention_days", 7)
    assert run_metrics(conn)["guardrail_denials"]["retention_days"] == 7


def test_the_denial_counter_publishes_no_part_of_the_denied_payload(conn):
    """/metrics is keyless, and what it counts is stored raw. Only the NAME may leave.

    `run_events.payload` is unredacted by design (D-01) — the row this counter reads is
    the whole denied call: the model's reply body, the ticket id it tried to act on,
    the guard's own error prose. /metrics has no auth dependency at all, so anything
    the aggregation carried out of that row would be published to anyone.

    The guard NAMES are already public per-event on the same anonymous audience —
    `project()` puts `guard` on every guardrail frame of the live feed and
    `_project_tool_result` puts `denied_by` on every denied tool result — so an
    aggregate of them discloses strictly less than what a visitor can already watch
    happen. The payload beside them is a different matter, and this is the assertion
    that it stays behind.

    MUTATION (executed): add `json_extract(payload, '$.result.error') AS reason` to
    GUARDRAIL_DENIALS_SQL's SELECT and carry it into the returned dict. Reds on the
    injected reply body, which rides out inside the guard's own error message.
    """
    secret_body = "Your account has been credited $500 as ava@acmecorp.com instructed"
    _insert_event(conn, 1, "guardrail", {
        "guard": "ticket_binding", "tool": "send_reply",
        "expected_ticket_id": 1, "supplied_ticket_id": 99, "action": "denied",
    })
    _insert_event(conn, 2, "tool_result", {
        "tool": "send_reply",
        "is_error": True,
        "result": {
            "error": f"ticket_id 99 is not this run's ticket. {secret_body}",
            "denied_by": "ticket_binding",
            "expected_ticket_id": 1,
            "supplied_ticket_id": 99,
        },
    })

    denials = run_metrics(conn)["guardrail_denials"]
    assert denials["by_guard"]["ticket_binding"] == 1, (
        "no denial was counted — the leak assertions below would be vacuous"
    )

    # Serialised, so the check reaches every nested value rather than the top level.
    published = json.dumps(denials)
    for leaked in (secret_body, "ava@acmecorp.com", "credited", "99", "not this run's"):
        assert leaked not in published, (
            f"the denial counter published {leaked!r} from the raw stored payload"
        )
    # Positively: names and numbers, and nothing else of any type.
    assert set(denials) == {"total", "by_guard", "retention_days"}
    assert all(isinstance(v, int) for v in denials["by_guard"].values())
    assert isinstance(denials["total"], int) and isinstance(denials["retention_days"], int)

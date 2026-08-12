"""Phase 6 (dashboard experience) tests: the data foundations the later waves read.

Its own module on purpose. tests/test_run_events.py is Phase 5's file and is already
2127 lines; the surfaces covered here — the guarded column migrations, RunRecorder's
millisecond stamping, and the single budget arithmetic D-11 makes the gauge and the
gate share — are Phase 6's, and belong where a reader looking for Phase 6 will find them.
"""

import asyncio
import json
import re

from helpers import FakeClient, response, text_block, tool_use_block
from relay import db as db_module
from relay.config import settings
from relay.db import connect, init_db
from relay.main import app


def _create_table_block(table: str) -> str:
    """The CREATE TABLE body for one table, straight out of db.SCHEMA."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\);", db_module.SCHEMA, re.DOTALL
    )
    assert match, f"no CREATE TABLE block for {table} in db.SCHEMA"
    return match.group(1)


def test_phase6_migrations_are_idempotent(tmp_path):
    """A second init_db on a POPULATED db adds both new columns without raising.

    File-backed, not the `conn` fixture: a :memory: DB is always fresh, so it never
    exercises the "table already exists" path this test is entirely about — which is
    the only path the live Fly volume takes.

    MUTATION that must turn this red: drop the PRAGMA guard inside
    `_add_column_if_missing` and ALTER unconditionally — the second init_db raises
    sqlite3.OperationalError: duplicate column name: elapsed_ms.

    SECOND MUTATION, covered by the source assertion at the end: add
    `elapsed_ms INTEGER` to the run_events CREATE TABLE in SCHEMA and delete the ALTER.
    A fresh DB stays green and production silently gets no column (D-13), so behaviour
    alone cannot catch it. The assertion is therefore on the source: the DDL must name
    neither new column, so fresh and existing databases take the same migration path.
    """
    db = connect(tmp_path / "relay.db")
    try:
        init_db(db)
        # Rows written by code that predates these columns — i.e. what is on the volume.
        db.execute(
            "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
            ("ava@acmecorp.com", "legacy", "written before origin existed"),
        )
        db.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            ("legacyuid", 1, 1, "tool_use", "{}"),
        )
        db.commit()

        init_db(db)  # the re-run that happens on every boot against the live volume

        run_event_cols = {r["name"] for r in db.execute("PRAGMA table_info(run_events)")}
        ticket_cols = {r["name"] for r in db.execute("PRAGMA table_info(tickets)")}
        run_cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
        assert "elapsed_ms" in run_event_cols
        assert "origin" in ticket_cols
        # The migration this generalises must still happen — the helper replaced it.
        assert "run_uid" in run_cols

        # Legacy rows survive and read NULL. For `origin` that is load-bearing, not
        # incidental: NULL means "not demo-originated" and Wave 3 redacts it (D-02).
        assert db.execute("SELECT elapsed_ms FROM run_events").fetchone()["elapsed_ms"] is None
        assert db.execute("SELECT origin FROM tickets").fetchone()["origin"] is None
    finally:
        db.close()

    # Source assertion (see SECOND MUTATION above): the DDL owns neither column.
    assert "origin" not in _create_table_block("tickets")
    assert "elapsed_ms" not in _create_table_block("run_events")


class SlowFakeClient(FakeClient):
    """FakeClient with a real pause between turns, so a run spans measurable time.

    Without it a scripted run finishes inside one millisecond and every elapsed_ms is
    legitimately 0 — which would make the "at least one non-zero" assertion below
    vacuous and blind to the origin-per-insert mutation named in the test's docstring.
    """

    async def _create(self, **kwargs):
        await asyncio.sleep(0.02)
        return await super()._create(**kwargs)


def _make_ticket(client, email: str, body: str) -> int:
    created = client.post(
        "/tickets", json={"customer_email": email, "subject": "API limits", "body": body}
    )
    assert created.status_code == 201
    return created.json()["id"]


def test_run_events_carry_elapsed_ms(client, capture_frames, monkeypatch):
    """Every persisted row carries a millisecond offset from its run's start.

    Both recorder paths are covered: the read tool's tool_result goes through
    RunRecorder.record (its own transaction) and the write tool's through
    execute_and_record (inside the tool's transaction), and the assertions below name
    each by its payload rather than trusting that one implies the other.

    MUTATION that must turn this red: drop `elapsed_ms` from the INSERT column list and
    its value from the tuple in `_insert_event` — every row reads NULL.

    SECOND, INDEPENDENT MUTATION: move `self._t0 = time.monotonic()` from `__init__`
    into `_insert_event` — every row then measures from its own insert and reads 0, so
    the "at least one non-zero" assertion fails. That is the mutation that actually
    pins the origin being PER RUN, which is what makes
    elapsed_ms(tool_result) - elapsed_ms(tool_use) a tool's wall time.
    """
    # A real VOYAGE_API_KEY in .env would otherwise make retrieval reachable from this
    # suite; pinned so the test is free by construction, not by interception.
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = SlowFakeClient([
        response([tool_use_block("lookup_customer", {"email": "liam@brightco.io"})]),
        response([tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "z" * 40})]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    body, _frames = asyncio.run(capture_frames(ticket_id))
    assert "event: error" not in body

    # Re-opened from disk: the claim is that these rows are COMMITTED.
    reopened = connect(settings.db_path)
    try:
        run_uid = reopened.execute("SELECT run_uid FROM runs").fetchone()["run_uid"]
        rows = reopened.execute(
            "SELECT seq, type, payload, elapsed_ms FROM run_events WHERE run_uid = ?"
            " ORDER BY seq",
            (run_uid,),
        ).fetchall()
    finally:
        reopened.close()

    assert rows, "the run persisted no events at all"
    elapsed = [r["elapsed_ms"] for r in rows]
    assert all(isinstance(v, int) for v in elapsed), f"a row was not stamped: {elapsed}"
    # Monotonic origin, so time cannot run backwards between two rows of one run.
    assert elapsed == sorted(elapsed), elapsed
    # An offset from the run's start, not a wall clock or an epoch.
    assert elapsed[0] < 5000, elapsed[0]
    # Two SlowFakeClient turns precede the last row, so it cannot legitimately be 0.
    assert max(elapsed) > 0, elapsed

    by_tool = {
        json.loads(r["payload"])["tool"]: r["elapsed_ms"]
        for r in rows
        if r["type"] == "tool_result"
    }
    # record() — the read tool's own transaction.
    assert isinstance(by_tool.get("lookup_customer"), int)
    # execute_and_record() — inside the write tool's transaction, the path a stamp
    # added at the record() call site instead of in _insert_event would have missed.
    assert isinstance(by_tool.get("send_reply"), int)
    assert by_tool["send_reply"] >= by_tool["lookup_customer"]

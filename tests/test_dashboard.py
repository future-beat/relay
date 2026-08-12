"""Phase 6 (dashboard experience) tests: the data foundations the later waves read.

Its own module on purpose. tests/test_run_events.py is Phase 5's file and is already
2127 lines; the surfaces covered here — the guarded column migrations, RunRecorder's
millisecond stamping, and the single budget arithmetic D-11 makes the gauge and the
gate share — are Phase 6's, and belong where a reader looking for Phase 6 will find them.
"""

import re

from relay import db as db_module
from relay.db import connect, init_db


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

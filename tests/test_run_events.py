"""Phase 5 storage foundation: the run_events table, the guarded runs.run_uid migration,
and record_run's run_uid stamp.

The migration is the load-bearing piece. `CREATE TABLE IF NOT EXISTS` will not add a
column to a table that already exists, so on the live Fly volume — where init_db re-runs
against a populated runs table — a DDL-only approach is a silent no-op: no error, no
signal, and every later join returns NULL in production only. Hence an explicit
PRAGMA-guarded ALTER, and hence these tests run init_db twice on a populated database.
"""

from relay.db import connect, init_db
from relay.telemetry import record_run


def test_run_uid_migration_is_idempotent(tmp_path):
    """A second init_db on a populated DB must not raise, and legacy rows keep run_uid NULL.

    MUTATION that must turn this red: drop the PRAGMA guard in init_db and run a bare
    `conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")` unconditionally — the
    second init_db then raises sqlite3.OperationalError: duplicate column name: run_uid.
    """
    db = connect(tmp_path / "relay.db")
    try:
        init_db(db)
        # A legacy row: written by pre-phase-5 code, so it names no run_uid at all.
        db.execute(
            "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
            " output_tokens, cost_usd, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "claude-sonnet-5", 120, 2, 10, 5, 0.001, "resolved"),
        )
        db.commit()

        init_db(db)  # the re-run that happens on every boot against the live volume

        cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
        assert "run_uid" in cols
        assert db.execute("SELECT run_uid FROM runs").fetchone()["run_uid"] is None
    finally:
        db.close()


def test_run_events_table_shape(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_events)").fetchall()}
    assert cols == {"id", "run_uid", "ticket_id", "seq", "type", "payload", "created_at"}

    conn.execute(
        "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        ("abc123", 7, 0, "tool_use", '{"name": "lookup_customer"}'),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM run_events").fetchone()
    assert (row["run_uid"], row["ticket_id"], row["seq"], row["type"]) == ("abc123", 7, 0, "tool_use")
    assert row["payload"] == '{"name": "lookup_customer"}'
    assert row["created_at"]


def test_record_run_persists_run_uid(conn):
    record_run(
        conn,
        ticket_id=1,
        model="claude-sonnet-5",
        duration_ms=100,
        steps=2,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.002,
        outcome="resolved",
        run_uid="abc123",
    )
    assert conn.execute("SELECT run_uid FROM runs").fetchone()["run_uid"] == "abc123"


def test_record_run_without_run_uid_still_works(conn):
    # The default is what keeps every pre-phase-5 caller (evals, mcp_server, the tests
    # that call record_run directly) working unchanged.
    record_run(
        conn,
        ticket_id=1,
        model="claude-sonnet-5",
        duration_ms=100,
        steps=2,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.002,
        outcome="resolved",
    )
    assert conn.execute("SELECT run_uid FROM runs").fetchone()["run_uid"] is None

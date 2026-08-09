import sqlite3
import threading
from contextlib import contextmanager

import pytest

from relay.db import connect
from relay.telemetry import record_run
from relay.tools import create_escalation, send_reply, set_category

# --- pragmas ---


def test_wal_is_enabled_on_a_file_database(db):
    # The pragmas are the cheap half of async-safety: WAL lets a reader run alongside
    # the writer, and busy_timeout decides how long a blocked statement waits before
    # SQLite gives up. Asserted on a real file because that is the only place they bite.
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_is_a_silent_no_op_on_memory_databases():
    # Pinning a trap, not a feature. `PRAGMA journal_mode = WAL` against an in-memory
    # database returns 'memory' and raises nothing, so connect() looks like it worked
    # while being inert. Anyone who later asserts WAL against the :memory: `conn`
    # fixture will write a passing, meaningless test — this is why the new `db`
    # fixture is file-backed and why `conn` was deliberately left alone.
    memory_db = connect(":memory:")
    assert memory_db.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    memory_db.close()


def test_foreign_keys_still_enforced(db):
    # foreign_keys=ON predates this phase and survives the wrapper rewrite: the pragma
    # is set on the private connection, and the wrapper must surface SQLite's error
    # rather than swallow it while materialising the result.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO replies (ticket_id, body) VALUES (?, ?)", (999, "orphan"))


# --- transaction isolation ---


def test_a_failed_write_does_not_leave_a_partial_row_when_another_thread_commits(db):
    # The regression this whole plan exists to prevent. commit() is connection-scoped,
    # so with a bare shared connection thread B's commit() makes thread A's half-written
    # send_reply durable, and A's rollback then undoes nothing — leaving a ticket with a
    # reply row and status still 'open'. transaction() holds the lock across the whole
    # statement group so B cannot commit inside A's unit of work. Asserted on row
    # contents, not on the absence of an exception: the failure mode is silent.
    a_ticket = db.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", "Refund", "Please refund me"),
    ).lastrowid
    b_ticket = db.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("liam@brightco.io", "API limits", "What are the rate limits?"),
    ).lastrowid
    db.commit()

    # Barriers, never a sleep-and-hope: the damaging interleaving has to be forced.
    # `inserted` releases B only once A's transaction is open with a row in it.
    # `committed` is where A waits for B's commit — and its timeout is the actual
    # assertion, not a delay: a correct transaction() leaves B blocked on the lock, so
    # this rendezvous can only be reached if B managed to commit inside A's unit of
    # work. Against a transaction() that drops the lock between statements it is
    # reached in microseconds and A's rollback then undoes nothing.
    inserted = threading.Barrier(2, timeout=5)
    committed = threading.Barrier(2, timeout=0.5)
    failures = []

    def writer_a():
        # The shape of tools.send_reply: INSERT the reply, then UPDATE the ticket.
        # It dies in between, which is exactly where the old bug opened its window.
        try:
            with db.transaction():
                db.execute(
                    "INSERT INTO replies (ticket_id, body) VALUES (?, ?)",
                    (a_ticket, "half-written reply"),
                )
                inserted.wait()
                try:
                    committed.wait()
                except threading.BrokenBarrierError:
                    pass
                raise RuntimeError("thread A dies mid-write")
        except RuntimeError:
            pass
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            failures.append(exc)

    def writer_b():
        try:
            inserted.wait()
            db.execute(
                "INSERT INTO replies (ticket_id, body) VALUES (?, ?)",
                (b_ticket, "thread B's committed reply"),
            )
            db.commit()
            try:
                committed.wait()
            except threading.BrokenBarrierError:
                pass
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=writer_a), threading.Thread(target=writer_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert failures == []

    surviving = db.execute("SELECT ticket_id FROM replies").fetchall()
    assert [row["ticket_id"] for row in surviving] == [b_ticket]
    assert db.execute("SELECT status FROM tickets WHERE id = ?", (a_ticket,)).fetchone()[0] == "open"


# --- transaction() adoption at the call sites ---


def instrument_write_grouping(db, monkeypatch) -> tuple[list, list]:
    """Record, for each statement a caller issues, whether a transaction was open.

    Wraps the instance rather than the class so nothing leaks between tests, and
    leans on transaction() committing through the private connection: a commit seen
    here is therefore a caller's bare commit, never the transaction's own.
    """
    depth = [0]
    statements: list[tuple[str, int]] = []
    bare_commits: list[str] = []
    real_transaction, real_execute, real_commit = db.transaction, db.execute, db.commit

    @contextmanager
    def recording_transaction():
        depth[0] += 1
        try:
            with real_transaction() as handle:
                yield handle
        finally:
            depth[0] -= 1

    def recording_execute(sql, params=()):
        statements.append((sql, depth[0]))
        return real_execute(sql, params)

    def recording_commit():
        if depth[0] == 0:
            bare_commits.append("commit")
        return real_commit()

    monkeypatch.setattr(db, "transaction", recording_transaction)
    monkeypatch.setattr(db, "execute", recording_execute)
    monkeypatch.setattr(db, "commit", recording_commit)
    return statements, bare_commits


def _seed_ticket(db) -> int:
    ticket_id = db.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", "Refund", "Please refund me"),
    ).lastrowid
    db.commit()
    return ticket_id


@pytest.mark.parametrize(
    "writer",
    ["send_reply", "create_escalation", "set_category", "record_run"],
)
def test_business_writers_group_every_statement_in_one_transaction(writer, db, monkeypatch):
    # The test above proves transaction() works; this proves the writers still use it.
    # Adoption was the gap: rewriting send_reply or record_run as bare execute + commit()
    # left the whole suite green, because nothing looked at how the five multi-statement
    # writers research named as the hazard surface actually reach the database. A bare
    # commit() is connection-scoped, so it lands whatever half-finished unit of work
    # another thread has open — the exact regression this phase's data layer exists to
    # make unrepresentable. record_run is the most exposed of the five: it runs on the
    # event loop while worker threads write underneath it.
    ticket_id = _seed_ticket(db)
    statements, bare_commits = instrument_write_grouping(db, monkeypatch)

    writers = {
        "send_reply": lambda: send_reply(db, ticket_id, "Your refund is on its way."),
        "create_escalation": lambda: create_escalation(db, ticket_id, "needs a human", "high"),
        "set_category": lambda: set_category(db, ticket_id, "billing"),
        "record_run": lambda: record_run(
            db,
            ticket_id=ticket_id,
            model="test-model",
            duration_ms=1,
            steps=1,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.021,
            outcome="send_reply",
        ),
    }
    writers[writer]()

    assert statements, f"{writer} issued no statements — every assertion below is vacuous"
    ungrouped = [sql for sql, depth in statements if depth == 0]
    assert ungrouped == [], f"{writer} issued statements outside a transaction(): {ungrouped}"
    assert bare_commits == [], f"{writer} committed outside a transaction()"

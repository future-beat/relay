"""SQLite storage, and the module that owns connection ownership and transaction boundaries.

Phase 1 used the stdlib driver directly: one `sqlite3.Connection` shared by every
request, which was only accidentally correct because access was single-threaded.
Phase 2 offloads DB work to worker threads, so that accident becomes a bug — `commit()`
is connection-scoped, meaning one request can commit another's half-finished write.

So the connection is no longer handed out. `connect()` returns a `Database` that keeps
it private behind a re-entrant lock, materialises every result before releasing that
lock, and exposes `transaction()` as the only way to group statements. Postgres comes
with deployment (phase 6); until then this class is the concurrency story.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    email      TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    plan       TEXT NOT NULL,
    signed_up  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_email TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    category       TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS escalations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    reason     TEXT NOT NULL,
    priority   TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id     INTEGER NOT NULL,
    model         TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    steps         INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    outcome       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per agent step, written during the stream (the `runs` row above is a single
-- end-of-run summary and cannot answer "what did it do"). payload holds the RAW
-- event data as JSON: the DB is private and is the source of truth, so redaction
-- happens only at the public /events boundary, never here. run_uid is a soft join key,
-- not a foreign key — the runs row is inserted at end of stream, long after the first
-- event row, so a real FK would fail on every insert.
CREATE TABLE IF NOT EXISTS run_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uid    TEXT NOT NULL,
    ticket_id  INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- spent_today() sums today's runs on the event loop for every gated request; without
-- this it is a full table scan that grows for the life of the Fly volume.
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);

-- Phase 6's per-run drill-down selects every event for one run_uid; without this it is
-- a full scan of a table that grows by ~10 rows per run for the life of the volume.
CREATE INDEX IF NOT EXISTS idx_run_events_run_uid ON run_events(run_uid);
"""

SEED_CUSTOMERS = [
    ("ava@acmecorp.com", "Ava Chen", "enterprise", "2024-03-11"),
    ("liam@brightco.io", "Liam Patel", "pro", "2025-01-22"),
    ("noah@freetier.dev", "Noah Smith", "free", "2025-11-02"),
    ("mia@datalane.ai", "Mia Torres", "pro", "2024-08-30"),
]


class Result:
    """A materialised query result covering the slice of sqlite3.Cursor this codebase uses.

    Rows are fetched while Database's lock is still held. Returning the real cursor
    instead lets the caller step it *after* the lock is released, so a concurrent
    statement on the same connection interleaves with an in-flight read — which
    surfaces not as `sqlite3.OperationalError: database is locked` but as rows with
    null and empty-string columns (measured: `customer_email=None`, `status=''`).
    That silent corruption is the entire reason this class exists.
    """

    __slots__ = ("_i", "_rows", "description", "lastrowid", "rowcount")

    def __init__(self, rows, lastrowid, rowcount, description):
        self._rows, self._i = rows, 0
        self.lastrowid, self.rowcount, self.description = lastrowid, rowcount, description

    def fetchone(self) -> sqlite3.Row | None:
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[sqlite3.Row]:
        rows, self._i = self._rows[self._i:], len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[sqlite3.Row]:
        rows = self._rows[self._i:self._i + size]
        self._i += len(rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Database:
    """The single owner of the process's SQLite connection.

    The connection is private: nothing outside this class can issue a statement
    without holding the lock, which is what makes commit() this object's business
    rather than five call sites' business. The lock is re-entrant because
    transaction() calls execute() from inside its own critical section.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        # Guarded by the same lock, so it is only ever read and written by the one
        # thread currently inside transaction(); a second thread is blocked outside.
        self._depth = 0

    def _run(self, method: str, *args) -> Result:
        with self._lock:
            cur = getattr(self._conn, method)(*args)
            return Result(cur.fetchall(), cur.lastrowid, cur.rowcount, cur.description)

    def execute(self, sql: str, params=()) -> Result:
        return self._run("execute", sql, params)

    def executemany(self, sql: str, seq) -> Result:
        return self._run("executemany", sql, seq)

    def executescript(self, script: str) -> Result:
        return self._run("executescript", script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """Hold the connection for one unit of work. Safe to nest.

        commit() is connection-scoped, so an INSERT+UPDATE pair that releases the lock
        between statements can be committed halfway through by another request — and
        its own rollback then undoes nothing. BaseException, not Exception: a bare
        `except Exception` lets a CancelledError escape with the transaction still open.

        Nesting is the same bug wearing this method's own clothes. The lock is
        re-entrant so execute() can be called from in here, which means a nested
        transaction() is accepted silently; if it committed, it would commit the
        *outer* block's partial work and the outer rollback would then undo nothing.
        So only the outermost level commits or rolls back the connection, and inner
        levels get a SAVEPOINT — a real nested unit of work that can fail on its own
        without taking the enclosing one down, and that an outer rollback still
        discards. Phase 5 writes run_events from inside a run that already holds a
        transaction; that call site is why this is not left as a documented trap.
        """
        with self._lock:
            outermost = self._depth == 0
            savepoint = None if outermost else f"relay_nested_{self._depth}"
            if outermost:
                # Explicit, because pysqlite only auto-begins ahead of DML. Without a
                # transaction already open, an inner RELEASE would be releasing the
                # connection's *outermost* savepoint — which SQLite defines as a
                # commit, reintroducing exactly the bug this nesting support prevents.
                if not self._conn.in_transaction:
                    self._conn.execute("BEGIN")
            else:
                self._conn.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield self
            except BaseException:
                if outermost:
                    self._conn.rollback()
                else:
                    # ROLLBACK TO leaves the savepoint on the stack; RELEASE pops it.
                    self._conn.execute(f"ROLLBACK TO {savepoint}")
                    self._conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                if outermost:
                    self._conn.commit()
                else:
                    self._conn.execute(f"RELEASE {savepoint}")
            finally:
                self._depth -= 1


def connect(db_path: str | Path) -> Database:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Silently a no-op on :memory: — SQLite returns 'memory' and raises nothing.
    # tests/test_db.py asserts that, so nobody "fixes" it against the wrong fixture.
    conn.execute("PRAGMA journal_mode = WAL")
    # Python's sqlite3 already derives 5000 ms from connect(timeout=5.0); stated
    # explicitly so a future timeout= change cannot silently zero it.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return Database(conn)


def purge_expired_run_events(conn: Database, *, retention_days: int) -> int:
    """Delete run_events rows older than the retention window. Returns how many went.

    `payload` is raw by design (D-01): it is the record phase 6 drills into, so it holds
    customer emails, ticket bodies, reply text and every tool argument. Nothing else in
    this codebase ever deletes from that table, so without this the live volume
    accumulates other people's support tickets for the life of the deployment — a
    data-protection posture nobody chose, and a disk-exhaustion path on a 512MB machine
    (WR-05). Redacting at write time is not the alternative: it would leave phase 6
    nothing to drill into, which is the whole reason the table exists.

    Deliberately scoped to run_events. The `runs` summary rows carry no message content,
    /metrics is built from them, and SEC-03's daily ceiling sums them — deleting those
    would erase spend the ceiling can never see again.

    Synchronous, like every other statement in this module: the one caller offloads it
    with asyncio.to_thread, because Database holds its lock for the whole transaction.
    """
    with conn.transaction():
        # SQLite's own clock arithmetic on the same 'now' the DEFAULT uses, so the
        # comparison cannot drift with the process's timezone or with Python's clock.
        deleted = conn.execute(
            "DELETE FROM run_events WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        ).rowcount
    return max(deleted, 0)


def init_db(conn: Database) -> None:
    conn.executescript(SCHEMA)
    # `runs` already exists on the live Fly volume, and CREATE TABLE IF NOT EXISTS does
    # not add columns to a table it declines to create — adding run_uid to the DDL above
    # would be a silent no-op in production only: no error, no signal, and every event
    # join returning NULL. So the column is added explicitly. ALTER TABLE is not
    # idempotent (a second one raises "duplicate column name"), and init_db runs on every
    # boot, so the PRAGMA guard is what makes the re-run safe. Legacy rows keep NULL.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_uid" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")
    existing = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
            SEED_CUSTOMERS,
        )
    conn.commit()

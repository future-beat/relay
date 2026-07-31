"""SQLite storage. Phase 1 uses the stdlib driver; Postgres comes with deployment (phase 6)."""

import sqlite3
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

CREATE TABLE IF NOT EXISTS replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_CUSTOMERS = [
    ("ava@acmecorp.com", "Ava Chen", "enterprise", "2024-03-11"),
    ("liam@brightco.io", "Liam Patel", "pro", "2025-01-22"),
    ("noah@freetier.dev", "Noah Smith", "free", "2025-11-02"),
    ("mia@datalane.ai", "Mia Torres", "pro", "2024-08-30"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
            SEED_CUSTOMERS,
        )
    conn.commit()

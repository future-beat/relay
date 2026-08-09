from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.db import connect, init_db
from relay.main import app
from relay.ratelimit import reset_limits
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture(autouse=True)
async def _reset_limits():
    # MemoryStorage and the in-flight reservation are process state shared by the
    # whole pytest session. Without this reset, tests start returning 429 based on
    # execution order — which presents as flakiness rather than a fixture bug.
    await reset_limits()
    yield


@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def db(tmp_path):
    # File-backed on purpose: PRAGMA journal_mode = WAL is a silent no-op on :memory:,
    # so every WAL assertion written against the `conn` fixture would be vacuous.
    db = connect(tmp_path / "relay.db")
    init_db(db)
    yield db
    db.close()


@pytest.fixture()
def registry(conn):
    return build_registry(conn, KB_DIR)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from relay.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "api_key", "test-owner-key")
    monkeypatch.setattr(settings, "demo_key", "test-demo-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    # The key rides on the client's default headers so no test body needs to know
    # about it. Tests authenticate as the owner tier deliberately: owner limits
    # are loose, so the suite never fights the limiter. Demo-tier limit coverage
    # lives in tests/test_ratelimit.py, which sets its own tier explicitly.
    with TestClient(app, headers={"X-API-Key": "test-owner-key"}) as client:
        yield client

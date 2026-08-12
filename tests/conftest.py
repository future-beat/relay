import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from relay.db import connect, init_db
from relay.main import app, process_ticket
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


@pytest.fixture()
def capture_frames():
    """Drive one real run and hand back everything it published to the broker.

    Returns an async callable — the caller runs it under `asyncio.run`, following
    test_lifecycle.py: the `client` fixture's TestClient owns its own loop for the
    lifespan, so the run itself is driven on a loop the test controls, and
    `process_ticket` is awaited directly to get at `body_iterator`.

    Subscription happens INSIDE the coroutine on purpose. `asyncio.Queue` binds to the
    loop it is first awaited on, and the broker was constructed on the TestClient's
    lifespan loop; subscribing out here and draining in there is the loop-binding
    hazard `runs.py` documents, arrived at from the other direction. Only
    `put_nowait`/`get_nowait` are used, so nothing binds either way — subscribing
    inside keeps that true even if the broker's internals change.

    The queue is drained after the stream ends rather than concurrently: publish is a
    plain `def` with a bounded drop-oldest queue, so a run cannot block on a reader,
    and reading afterwards is the only way to be sure no frame is missed between the
    last publish and the assertion. `events_queue_maxsize` is 256 against a run of ~15
    events, so nothing is dropped; a test scripting a longer run must raise it.

    No reset hook is needed: the broker lives on `app.state` and is rebuilt by every
    `client` fixture's lifespan, and unsubscribe in the finally leaves it empty.
    """

    async def _drive(ticket_id: int, *, dry_run: bool = False):
        q = app.state.broker.subscribe()
        try:
            stream = await process_ticket(ticket_id, dry_run=dry_run)
            body = "".join([chunk async for chunk in stream.body_iterator])
        finally:
            # In a finally because a scripted run that raises mid-stream would
            # otherwise leave a dead queue in the broker for the rest of the session,
            # which is Pitfall 3 reproduced inside the test suite.
            app.state.broker.unsubscribe(q)
        frames = []
        while True:
            try:
                frames.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return body, frames

    return _drive


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch):
    """Fail loudly on any unmocked outbound HTTP from the whole suite.

    search_docs reads settings.voyage_api_key, and several suites drive it through
    the tool registry. Without this, a developer with VOYAGE_API_KEY in .env would
    have unit tests making real, paid Voyage calls the moment kb/index.json exists —
    green locally, billed, and untestable in CI. Tests that need Voyage install their
    own fake over this (monkeypatch is per-test, so a later setattr wins).
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "a test attempted a real outbound HTTP call — install a fake "
            "(see the `voyage` fixture in tests/test_retrieval.py)"
        )

    monkeypatch.setattr(httpx, "post", _forbidden)

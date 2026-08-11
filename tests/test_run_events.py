"""Phase 5 storage foundation: the run_events table, the guarded runs.run_uid migration,
and record_run's run_uid stamp.

The migration is the load-bearing piece. `CREATE TABLE IF NOT EXISTS` will not add a
column to a table that already exists, so on the live Fly volume — where init_db re-runs
against a populated runs table — a DDL-only approach is a silent no-op: no error, no
signal, and every later join returns NULL in production only. Hence an explicit
PRAGMA-guarded ALTER, and hence these tests run init_db twice on a populated database.

Plan 02 adds the events.py contracts on top of that storage: the broker's drop-oldest
fan-out (a stalled dashboard viewer must never backpressure a paid run), project()'s
allowlist redaction, and the load-bearing D-04 atomicity test.
"""

import asyncio
import inspect
import json

from relay.db import connect, init_db
from relay.events import _CLOSE_SENTINEL, RunEventBroker, project
from relay.models import AgentEvent
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


class _HostileQueue:
    """A subscriber that is full AND whose get fails — the worst case publish must survive.

    Real asyncio.Queue cannot reach this state, but a subscriber whose queue is
    concurrently drained between the QueueFull and the get_nowait can: the drop-oldest
    retry then finds it empty. publish must swallow that, not raise into the run.
    """

    def put_nowait(self, frame):
        raise asyncio.QueueFull

    def get_nowait(self):
        raise asyncio.QueueEmpty


async def test_publish_drops_oldest_and_never_blocks():
    """A full subscriber loses its OLDEST frame; publish returns normally and never raises.

    MUTATION that must turn this red: delete the `except asyncio.QueueFull` drop-oldest
    branch from publish and leave a bare `q.put_nowait(frame)` — the third publish then
    raises asyncio.QueueFull straight into the paid agent run.
    """
    broker = RunEventBroker(maxsize=2)
    q = broker.subscribe()

    for n in (1, 2, 3):
        # Fire-and-forget: the return value is None, and nothing here is awaited.
        assert broker.publish({"type": "usage", "n": n}) is None

    assert q.qsize() == 2, "the queue grew past maxsize — it is not bounded"
    # The OLDEST went, not the newest: a live feed shows the most recent steps.
    assert [q.get_nowait()["n"], q.get_nowait()["n"]] == [2, 3]

    # A subscriber whose queue is full and whose get also fails still cannot break a run.
    broker._subs.add(_HostileQueue())
    assert broker.publish({"type": "usage", "n": 4}) is None


def test_publish_is_synchronous():
    # A plain def cannot suspend. An `async def publish` could be made to await a slow
    # subscriber, which is precisely the backpressure onto a paid run that D-10 forbids —
    # so this is asserted on the function object, where no test body can forget it.
    assert inspect.iscoroutinefunction(RunEventBroker.publish) is False
    assert inspect.iscoroutinefunction(RunEventBroker.close) is False


async def test_unsubscribe_is_idempotent():
    # Mirrors RunRegistry.deregister: a viewer that disconnects mid-stream may be
    # unsubscribed twice (generator finally + broker close), and the second call must
    # not raise out of a finally block, where it would mask the original error.
    broker = RunEventBroker(maxsize=2)
    q = broker.subscribe()

    broker.unsubscribe(q)
    broker.unsubscribe(q)

    assert broker._subs == set()


async def test_close_wakes_every_subscriber_with_the_sentinel():
    # lifespan calls close() so open /events generators end promptly instead of burning
    # uvicorn's graceful-shutdown window. A full queue still gets the sentinel — it is
    # delivered through the same drop-oldest path, or a stalled viewer never terminates.
    broker = RunEventBroker(maxsize=1)
    idle, full = broker.subscribe(), broker.subscribe()
    broker.publish({"type": "usage"})

    broker.close()

    assert broker.closed is True
    assert idle.get_nowait() is _CLOSE_SENTINEL
    assert full.get_nowait() is _CLOSE_SENTINEL


def test_project_tool_use_drops_input():
    # tool_use.input is where every secret enters the stream: lookup_customer carries an
    # email, send_reply the whole reply body, search_docs the query. The tool NAME is the
    # only field the dashboard needs to show "it is looking the customer up right now".
    frame = project(AgentEvent(
        type="tool_use",
        data={"tool": "lookup_customer", "input": {"email": "ava@acmecorp.com"}},
    ))

    assert frame == {"type": "tool_use", "tool": "lookup_customer"}
    assert "ava@acmecorp.com" not in json.dumps(frame)


def test_project_lookup_customer_drops_customer():
    # The raw result is a whole customer row plus their last ten ticket subjects. None of
    # it is projectable: there is no safe subset, so the whole object goes.
    frame = project(AgentEvent(
        type="tool_result",
        data={
            "tool": "lookup_customer",
            "result": {
                "found": True,
                "customer": {"name": "Ava Chen", "email": "ava@acmecorp.com", "plan": "pro"},
                "recent_tickets": [{"id": 3, "subject": "Refund for the March invoice"}],
            },
            "is_error": False,
        },
    ))

    assert frame == {"type": "tool_result", "tool": "lookup_customer", "is_error": False}
    rendered = json.dumps(frame)
    for leaked in ("Ava Chen", "ava@acmecorp.com", "pro", "Refund for the March invoice"):
        assert leaked not in rendered


def test_project_search_docs_keeps_ids_not_text():
    # D-07 allows retrieval doc ids and scores — that is what makes the feed legibly
    # "grounded" — but never the retrieved prose, which is the largest untrusted-adjacent
    # blob in the stream and the one most likely to restate a customer's own words back.
    frame = project(AgentEvent(
        type="tool_result",
        data={
            "tool": "search_docs",
            "result": {"results": [
                {"doc": "billing.md", "heading": "Refunds", "id": "billing.md#refunds",
                 "text": "Refunds are issued within 14 days...", "score": 0.82},
            ]},
            "is_error": False,
        },
    ))

    assert frame["results"] == [
        {"doc": "billing.md", "id": "billing.md#refunds", "score": 0.82}
    ]
    assert "text" not in frame["results"][0]
    assert "Refunds are issued within 14 days" not in json.dumps(frame)


def test_project_text_is_dropped():
    # Model prose restates whatever it just read — the customer's plan, their email, the
    # ticket body. The viewer still sees that the model spoke; it does not see what it said.
    frame = project(AgentEvent(type="text", data={"text": "Hi Ava, your card ending 4242..."}))

    assert frame is None or "text" not in frame
    assert "4242" not in json.dumps(frame)


def test_project_never_spreads_raw_data():
    """No projected frame may carry a field project() did not name.

    MUTATION that must turn this red: add `**event.data` (or `**d`) to the tool_use frame
    in project() — LEAK_SENTINEL then rides along inside the copied `input` and this
    fails. That spread is a denylist wearing an allowlist's clothes: it publishes every
    field anyone adds to an event from then on, silently and by default.
    """
    for event in (
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {"body": "LEAK_SENTINEL"}}),
        AgentEvent(type="text", data={"text": "LEAK_SENTINEL"}),
        AgentEvent(type="tool_result", data={
            "tool": "lookup_customer",
            "result": {"found": True, "customer": {"email": "LEAK_SENTINEL"}},
            "is_error": False,
        }),
        AgentEvent(type="guardrail", data={
            "guard": "citation", "tool": "send_reply", "action": "denied",
            "missing_citations": ["LEAK_SENTINEL"], "retrieved_ids": ["LEAK_SENTINEL"],
        }),
    ):
        assert "LEAK_SENTINEL" not in json.dumps(project(event)), f"leaked via {event.type}"


def test_project_fails_closed_on_unknown_types_and_tools():
    # An event type nobody has projected yet is dropped, not passed through: a new yield
    # site in agent.py must be an explicit decision here, never a default-publish.
    assert project(AgentEvent(type="debug_dump", data={"secret": "LEAK_SENTINEL"})) is None

    frame = project(AgentEvent(type="tool_result", data={
        "tool": "some_future_tool", "result": {"secret": "LEAK_SENTINEL"}, "is_error": False,
    }))
    assert frame == {"type": "tool_result", "tool": "some_future_tool", "is_error": False}


def test_project_keeps_the_cost_and_outcome_the_dashboard_runs_on():
    # The allowlist has to be permissive enough to be useful: cost, steps and outcome are
    # the numbers the whole dashboard exists to show, and none of them is sensitive.
    usage = project(AgentEvent(type="usage", data={
        "steps": 2, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.002,
        "max_cost_usd": 0.5,
    }))
    assert usage == {"type": "usage", "steps": 2, "input_tokens": 10,
                     "output_tokens": 5, "cost_usd": 0.002}

    resolution = project(AgentEvent(type="resolution", data={
        "via": "send_reply", "steps": 3, "cost_usd": 0.004, "max_cost_usd": 0.5,
    }))
    assert resolution == {"type": "resolution", "via": "send_reply",
                          "cost_usd": 0.004, "steps": 3}

    assert project(AgentEvent(type="error", data={"reason": "budget_exceeded"}))["reason"] == (
        "budget_exceeded"
    )
    assert project(AgentEvent(type="notice", data={
        "kind": "retrieval_degraded", "tool": "search_docs",
        "retrieval_mode": "keyword", "cause": "voyage_failed", "results": 3,
    }))["cause"] == "voyage_failed"

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
import sqlite3
import subprocess
from pathlib import Path

import pytest

from helpers import FakeClient, response, text_block, tool_use_block
from relay.agent import bind_to_ticket
from relay.config import settings
from relay.db import connect, init_db
from relay.events import _CLOSE_SENTINEL, RunEventBroker, RunRecorder, project
from relay.guardrails import ToolPolicy
from relay.main import app, process_ticket
from relay.models import AgentEvent
from relay.telemetry import record_run
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"


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


def _seed_ticket(db) -> int:
    with db.transaction():
        cur = db.execute(
            "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
            ("ava@acmecorp.com", "Refund", "I was charged twice for March."),
        )
        return cur.lastrowid


def test_a_recorder_writes_one_row_per_event(conn):
    recorder = RunRecorder(conn, run_uid="u1", ticket_id=7)

    recorder.record(AgentEvent(type="usage", data={"steps": 1}))
    recorder.record(AgentEvent(type="text", data={"text": "thinking"}))
    recorder.record(AgentEvent(type="tool_use", data={"tool": "search_docs"}))

    rows = conn.execute(
        "SELECT seq, type, payload FROM run_events WHERE run_uid = ? ORDER BY seq", ("u1",)
    ).fetchall()

    assert [r["seq"] for r in rows] == [1, 2, 3], "seq must be monotonic per run"
    assert [r["type"] for r in rows] == ["usage", "text", "tool_use"]
    # RAW, not projected: phase 6's drill-down needs the tool inputs and retrieved text
    # that project() strips. Redaction is a publish-time transform, not a write-time one.
    assert json.loads(rows[1]["payload"]) == {"text": "thinking"}
    assert conn.execute(
        "SELECT ticket_id FROM run_events WHERE run_uid = ?", ("u1",)
    ).fetchone()["ticket_id"] == 7


def test_send_reply_and_its_event_row_commit_atomically(db, tmp_path, monkeypatch):
    """A write tool and the record of that write commit or roll back TOGETHER (D-04).

    MUTATION that must turn this red: in execute_and_record, move the `_insert_event`
    call out of the `with self.conn.transaction():` block so the event is persisted in
    a second, separate transaction after the tool's own has committed. The forced
    failure below then leaves COUNT(*) FROM replies == 1 — a reply sent to a customer
    with no record that the agent sent it, which is the exact repudiation gap D-04
    exists to close.

    The rollback half is the whole point. A version of this test that only checks both
    rows exist on the happy path passes under that mutation and proves nothing.
    """
    # Keyword mode pinned: build_registry loads the retrieval index, and an unpinned
    # key in a developer's .env turns a free unit test into a paid Voyage call.
    monkeypatch.setattr(settings, "voyage_api_key", None)
    registry = build_registry(db, KB_DIR)
    ticket_id = _seed_ticket(db)
    execute_bound = bind_to_ticket(ticket_id)
    recorder = RunRecorder(db, run_uid="u1", ticket_id=ticket_id)
    call = {
        "spec": registry["send_reply"],
        "name": "send_reply",
        "raw_input": {"ticket_id": ticket_id, "body": "Refunded — it lands in 3-5 days."},
        "policy": ToolPolicy(),
        "event_type": "tool_result",
    }

    def insert_against_a_closed_database(self, *args, **kwargs):
        # The realistic failure: lifespan closed the connection under an in-flight run.
        raise sqlite3.OperationalError("database is locked")

    with pytest.MonkeyPatch.context() as forced_failure:
        forced_failure.setattr(RunRecorder, "_insert_event", insert_against_a_closed_database)
        with pytest.raises(sqlite3.OperationalError):
            recorder.execute_and_record(execute_bound, **call)

    assert db.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0, (
        "the reply survived its failed event insert — the two are not in one transaction"
    )
    assert db.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
    # The tool's other write in the same transaction went back too, or the ticket reads
    # resolved with nothing that resolved it.
    assert db.execute(
        "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()["status"] == "open"

    # Case 1, the same call unmutated: both rows land and both survive a reconnect.
    result, is_error = recorder.execute_and_record(execute_bound, **call)

    assert is_error is False
    assert json.loads(result)["status"] == "resolved"

    reopened = connect(tmp_path / "relay.db")
    try:
        assert reopened.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 1
        row = reopened.execute("SELECT seq, type, payload FROM run_events").fetchone()
        assert (row["seq"], row["type"]) == (1, "tool_result")
        payload = json.loads(row["payload"])
        assert payload["tool"] == "send_reply"
        assert payload["is_error"] is False
        assert payload["result"]["status"] == "resolved"
    finally:
        reopened.close()


def test_recorder_is_synchronous():
    # It runs inside asyncio.to_thread (plan 03) and must hold the tool's transaction on
    # that one worker thread: transaction() re-entrancy is per-thread, so an await in
    # here would let the loop resume the run elsewhere with the transaction still open.
    for name in ("record", "execute_and_record", "_insert_event"):
        assert inspect.iscoroutinefunction(getattr(RunRecorder, name)) is False


# --------------------------------------------------------------------------------------
# Plan 03: the recorder wired end to end. Everything below drives a REAL run through
# event_stream — scripted model, real tools, real SQLite — because the three properties
# this phase must hold (full-sequence persistence, publish-after-commit, and no secret
# in any published frame) are all properties of the wiring, and a unit test of
# project() or RunRecorder in isolation cannot see any of them.
# --------------------------------------------------------------------------------------

# Seeded into a real run below and asserted absent from every published frame. Distinct
# and improbable on purpose: a substring search for "email" would match half the corpus,
# and a sentinel that could plausibly occur by accident makes the absence assertion mean
# nothing. Each one enters the run through a DIFFERENT field project() actually inspects.
EMAIL_SENTINEL = "leak-sentinel-9f3a2b@example.com"          # -> lookup_customer input+result
BODY_SENTINEL = "SENTINEL-TICKET-BODY-7c1e4d"                # -> create_escalation.reason
KEY_SENTINEL = "sk-ant-SENTINEL-FAKE-KEY-4b2df8"             # -> search_docs.query

SENTINELS = (
    ("customer email", EMAIL_SENTINEL),
    ("ticket body", BODY_SENTINEL),
    ("api key", KEY_SENTINEL),
)


def _streamed_event_types(body: str) -> list[str]:
    """The AgentEvent types the SSE stream actually carried, in order.

    `done` is filtered out: it is written by event_stream itself after run_ticket
    finishes, is not an AgentEvent, and so has no run_events row to correspond to.
    """
    return [
        line[len("event: "):]
        for line in body.splitlines()
        if line.startswith("event: ") and line != "event: done"
    ]


def _make_ticket(client, email: str, body: str, subject: str = "API limits") -> int:
    created = client.post(
        "/tickets", json={"customer_email": email, "subject": subject, "body": body}
    )
    assert created.status_code == 201
    return created.json()["id"]


def test_a_run_persists_its_full_event_sequence(client, capture_frames, monkeypatch):
    """Every event the stream carried has exactly one run_events row, in seq order.

    MUTATION that must turn this red: drop the `await _persisted(...)` wrapper from the
    `usage` yield in agent.py (`yield AgentEvent(type="usage", ...)`) — four usage rows
    then go missing and the type sequence no longer matches the stream.
    """
    # Pinned so search_docs below cannot reach Voyage: a real VOYAGE_API_KEY in .env
    # would otherwise make this suite issue paid calls. _no_outbound_http is the
    # backstop; this makes the test free by construction rather than by interception.
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": "liam@brightco.io"})]),
        response([tool_use_block("search_docs", {"query": "rate limits"})]),
        response([tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "z" * 40})]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    body, _frames = asyncio.run(capture_frames(ticket_id))

    streamed = _streamed_event_types(body)
    assert "event: error" not in body
    # A read tool, a write tool and the terminal resolution all present, so the
    # sequence assertion below is covering both recorder paths and not just one.
    assert {"usage", "tool_use", "tool_result", "resolution"} <= set(streamed)

    # Re-opened from disk, not read back through app.state.conn: the claim is that
    # these rows are COMMITTED, and a read on the writing connection would be green
    # for an open transaction that never lands.
    reopened = connect(settings.db_path)
    try:
        run_row = reopened.execute("SELECT run_uid, outcome FROM runs").fetchone()
        assert run_row["outcome"] == "send_reply"
        run_uid = run_row["run_uid"]
        assert run_uid, "record_run did not stamp the run's uid — nothing joins to it"

        rows = reopened.execute(
            "SELECT type, seq FROM run_events WHERE run_uid = ? ORDER BY seq", (run_uid,)
        ).fetchall()
    finally:
        reopened.close()

    # One row per streamed event, same types, same order. Asserted as a whole list
    # rather than a count so a row landing under the wrong type still fails.
    assert [r["type"] for r in rows] == streamed
    assert [r["seq"] for r in rows] == list(range(1, len(streamed) + 1))


def test_broker_never_leads_the_database(client, monkeypatch):
    """D-06: at the moment frame k is published, k events are already committed.

    Written so the REVERSE is observable rather than assumed: the count is sampled
    inside publish itself, so a publish that ran ahead of its write records a number
    lower than its own position and this fails on that frame.

    MUTATION that must turn this red: in agent.py's `_persisted`, stop awaiting the
    write — `asyncio.create_task(asyncio.to_thread(recorder.record, event))` instead of
    `await asyncio.to_thread(...)`. That is the realistic version of this bug (someone
    "frees the loop" from the persistence path) and it makes every publish lead its row.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": "liam@brightco.io"})]),
        response([tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "z" * 40})]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    conn = app.state.conn
    committed_at_publish: list[int] = []
    real_publish = app.state.broker.publish

    def counting_publish(frame):
        # Sampled BEFORE the frame reaches any subscriber, on the event loop, which is
        # exactly where a publish that led its write would be caught.
        committed_at_publish.append(
            conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"]
        )
        return real_publish(frame)

    monkeypatch.setattr(app.state.broker, "publish", counting_publish)

    async def drive():
        stream = await process_ticket(ticket_id)
        return "".join([chunk async for chunk in stream.body_iterator])

    body = asyncio.run(drive())
    assert "event: error" not in body

    assert committed_at_publish, "the run published nothing — this test proved nothing"
    for k, committed in enumerate(committed_at_publish, start=1):
        assert committed >= k, (
            f"publish #{k} ran with only {committed} rows committed — the broker led the"
            " database, so a subscriber can see an event that never lands (D-06)"
        )
    # Every surfaced event is one row, so trailing by exactly zero is the true state;
    # asserting it as well means a run that quietly stopped persisting some events
    # cannot hide behind the >= above.
    assert committed_at_publish[-1] == len(committed_at_publish)


def test_no_projection_leaks_sensitive_data(client, capture_frames, monkeypatch):
    """SC-3, the load-bearing test: no seeded secret reaches a published frame.

    Three secrets enter the run through three DIFFERENT fields that project() actually
    inspects — the ticket body goes in via `create_escalation.reason`, an observed tool
    field, because a `text` event is dropped unconditionally and routing the body only
    through prose would make the "body never leaks" half of this vacuous (Pitfall 4).

    MUTATION that must turn this red for ALL THREE sentinels: spread the raw event data
    into project()'s tool_use frame — `return {"type": t, "tool": d.get("tool"), **d}`.
    Every sentinel rides a tool_use input, so one spread leaks all three.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    # A real customer row, so lookup_customer returns the whole record — email, name,
    # plan — and the projection has something genuine to have to drop.
    app.state.conn.execute(
        "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
        (EMAIL_SENTINEL, "Leak Sentinel", "enterprise", "2025-01-01"),
    )
    app.state.conn.commit()
    ticket_id = _make_ticket(
        client, EMAIL_SENTINEL, f"my key {KEY_SENTINEL} stopped working. {BODY_SENTINEL}"
    )
    app.state.client = FakeClient([
        # (1) email -> tool_use.input.email, and -> tool_result.result.customer.email
        response([tool_use_block("lookup_customer", {"email": EMAIL_SENTINEL})]),
        # (2) fake key -> tool_use.input.query
        response([tool_use_block("search_docs", {"query": f"rotating {KEY_SENTINEL}"})]),
        # (3) ticket body -> create_escalation.reason, an OBSERVED field, plus the same
        # string in model prose so the `text` drop is exercised on the way past.
        response([
            text_block(f"The customer wrote: {BODY_SENTINEL}"),
            tool_use_block("create_escalation", {
                "ticket_id": ticket_id,
                "reason": f"customer reported: {BODY_SENTINEL} — needs a human",
                "priority": "high",
            }),
        ]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])

    body, frames = asyncio.run(capture_frames(ticket_id))

    # The absence below is only meaningful if the secrets were genuinely in this run.
    # Without this half the test would stay green against a run that never carried
    # them — the unfalsifiable form of exactly this check.
    assert "event: error" not in body
    for name, sentinel in SENTINELS:
        assert sentinel in body, (
            f"the {name} sentinel never reached the run's own event stream — the"
            " redaction assertion below would be vacuous"
        )
    raw = "".join(
        r["payload"] for r in
        app.state.conn.execute("SELECT payload FROM run_events").fetchall()
    )
    for name, sentinel in SENTINELS:
        # D-01: run_events is private and full-fidelity. If redaction had leaked into
        # the persistence path, the frames would be clean for the wrong reason and
        # phase 6 would have nothing to drill into.
        assert sentinel in raw, f"the {name} sentinel is missing from the raw run_events row"

    published_tools = {f.get("tool") for f in frames if f.get("type") == "tool_use"}
    assert {"lookup_customer", "search_docs", "create_escalation"} <= published_tools, (
        f"the run did not publish all three leak vectors as frames: {published_tools}"
    )

    # Checked per frame and per sentinel, not against one concatenated blob, so a
    # single leaking frame is enough to fail this and the failure names which frame
    # and which secret. Collected rather than asserted in place because the useful
    # answer under the mutation above is ALL the vectors that opened, not just the
    # first — a leak that closes one field and leaves two is not a fix.
    leaks = [
        (i, frame.get("type"), frame.get("tool"), name)
        for i, frame in enumerate(frames)
        for name, sentinel in SENTINELS
        if sentinel in json.dumps(frame)
    ]
    assert leaks == [], f"published frames leaked seeded secrets: {leaks}"


def test_recorder_untouched_files():
    """The frozen-caller contract, as a fact about the repo rather than a promise.

    recorder defaults to None, so evals.py and mcp_server.py — which call run_ticket
    and _execute_guarded with neither a recorder nor any knowledge of one — must not
    have needed a single byte changed. If they did, the optional-collaborator design
    broke and the fix belongs in agent.py, not here. ci.yml is included because a
    green suite bought by loosening the gate is not a green suite.
    """
    repo = Path(__file__).parent.parent
    frozen = ["src/relay/mcp_server.py", "src/relay/evals.py", ".github/workflows/ci.yml"]
    # --name-only first purely so the failure names the file; --quiet is the assertion,
    # because its exit code is the same one a reviewer would run by hand.
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", *frozen],
        cwd=repo, capture_output=True, text=True, check=False,
    ).stdout.strip()
    result = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", *frozen], cwd=repo, check=False
    )
    assert result.returncode == 0, f"phase 5 modified a frozen file: {changed}"

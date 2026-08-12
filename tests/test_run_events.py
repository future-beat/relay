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
import itertools
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from helpers import (
    FakeClient,
    TicketAwareFakeClient,
    response,
    text_block,
    tool_use_block,
)
from relay import main
from relay.agent import bind_to_ticket
from relay.config import settings
from relay.db import connect, init_db
from relay.events import (
    _CLOSE_SENTINEL,
    BrokerUnavailable,
    RunEventBroker,
    RunRecorder,
    project,
    snapshot_frame,
)
from relay.guardrails import ToolPolicy
from relay.main import app, events, process_ticket
from relay.models import AgentEvent
from relay.runs import ActiveRun
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


async def test_subscribe_refuses_past_the_ceiling_without_leaking_a_queue():
    """CR-01: the subscriber set is bounded, and a refusal creates nothing.

    MUTATION that must turn this red: delete the `if self.at_capacity: raise` guard from
    subscribe() — the second call then hands back a queue and `_subs` grows to 2, which
    is the unbounded set an anonymous reconnect loop grows for free while every paid
    run pays O(subscribers) to publish into it.
    """
    broker = RunEventBroker(maxsize=2, max_subscribers=1)
    q = broker.subscribe()

    assert broker.at_capacity is True
    with pytest.raises(BrokerUnavailable):
        broker.subscribe()

    # The refused viewer left nothing behind: no queue to publish into, no slot spent.
    assert broker._subs == {q}
    # And the ceiling is not a one-shot: the slot frees when the viewer leaves.
    broker.unsubscribe(q)
    assert broker.at_capacity is False
    assert broker.subscribe() is not q


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


def test_project_marks_a_denied_write_tool_as_an_error_not_a_null_success():
    """WR-01: a guardrail denial must not publish as a success-shaped frame.

    A denied send_reply returns a DICT (`{"error": ..., "denied_by": ...}`), so it used
    to take the send_reply branch and publish `{"reply_id": null, "status": null}` —
    byte-identical in shape to a successful reply whose fields happened to be missing,
    with no error signal anywhere in the frame.

    MUTATION that must turn this red: move the `if is_error or not isinstance(...)`
    branch in _project_tool_result BELOW the per-tool dispatch (or drop `is_error` from
    the send_reply branch) — the denial is published as a null-shaped success again and
    both the flag and the guard name disappear.
    """
    denied = project(AgentEvent(type="tool_result", data={
        "tool": "send_reply",
        "result": {
            "error": "ticket_id 42 is not this run's ticket. Retry with ticket_id=7.",
            "denied_by": "ticket_binding",
            "expected_ticket_id": 7,
            "supplied_ticket_id": 42,
        },
        "is_error": True,
    }))
    ok = project(AgentEvent(type="tool_result", data={
        "tool": "send_reply",
        "result": {"reply_id": 3, "status": "resolved"},
        "is_error": False,
    }))

    assert denied["is_error"] is True
    assert denied["denied_by"] == "ticket_binding"
    assert ok["is_error"] is False
    assert denied != ok, "a refusal and an action publish as the same frame"
    # That it was denied, never WHAT was denied: the reply body, the model's supplied
    # ticket id and the retry instruction are all the model's own output.
    for leaked in ("42", "Retry with", "not this run's ticket"):
        assert leaked not in json.dumps(denied)


def test_project_distinguishes_a_failed_search_from_an_empty_one():
    """WR-01: an errored search_docs must not render as "the KB has nothing".

    An empty result set is the signal that makes the model escalate. A search that blew
    up returns `{"error": ...}`, whose `results` key is absent — so the search_docs
    branch used to coerce it to `[]` and publish the two as the same frame.

    MUTATION that must turn this red: move the is_error branch below the search_docs
    dispatch — the failed search publishes `{"results": []}` with is_error absent, and
    the inequality assertion fails.
    """
    failed = project(AgentEvent(type="tool_result", data={
        "tool": "search_docs",
        "result": {"error": "index unavailable"},
        "is_error": True,
    }))
    empty = project(AgentEvent(type="tool_result", data={
        "tool": "search_docs", "result": {"results": []}, "is_error": False,
    }))

    assert failed["is_error"] is True
    assert empty == {"type": "tool_result", "tool": "search_docs",
                     "is_error": False, "results": []}
    assert failed != empty, "a broken retriever and an empty KB publish identically"


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
    notice = project(AgentEvent(type="notice", data={
        "kind": "retrieval_degraded", "tool": "search_docs",
        "retrieval_mode": "keyword", "cause": "voyage_failed", "results": 3,
    }))
    assert notice["cause"] == "voyage_failed"
    assert notice["result_count"] == 3


def test_project_notice_publishes_a_count_and_never_the_results():
    """WR-02: the one field that used to forward whatever the notice carried.

    agent.py sets `results` to a len() today, so the leak is latent rather than live —
    but nothing in project() required an int, and the natural future edit ("show WHICH
    results we degraded to") makes it the list of hits. This pins the shape at the
    boundary instead of trusting the producer: the frame's field is a COUNT, and a
    non-int is dropped.

    MUTATION that must turn this red: restore `"results": d.get("results")` in the
    notice branch of project() — the retrieved prose and headings ride straight out to
    the unauthenticated /events feed and the sentinel assertion below fails.
    """
    leaked = project(AgentEvent(type="notice", data={
        "kind": "retrieval_degraded",
        "tool": "search_docs",
        "retrieval_mode": "keyword",
        "cause": "index_unavailable",
        # The shape one line in agent.py away: the hits themselves, prose included.
        "results": [
            {"doc": "billing.md", "heading": "Refunds",
             "text": "PROSE_SENTINEL — refunds are issued within 14 days", "score": 0.82},
        ],
    }))

    assert "PROSE_SENTINEL" not in json.dumps(leaked), (
        "a list-shaped notice published the retrieved prose the tool_result branch"
        " goes out of its way to strip"
    )
    assert "results" not in leaked, "the unconstrained field name is back"
    assert leaked["result_count"] is None, "a non-int count was forwarded rather than dropped"
    # Still useful for the shape it was designed for: the degradation is legible.
    assert leaked["cause"] == "index_unavailable"
    assert leaked["retrieval_mode"] == "keyword"


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
    result, is_error, recorded = recorder.execute_and_record(execute_bound, **call)

    assert is_error is False
    # The write path records in-transaction; only a denial (which wrote nothing) defers.
    assert recorded is True
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


def test_a_denied_write_tool_writes_no_event_row_of_its_own(db, monkeypatch):
    """CR-02, at the seam: a guardrail denial defers its row instead of taking a seq.

    The unit half of the ordering fix. A denial returns before the tool ever executes,
    so there is no write for the row to be atomic with — recording it here is what put
    the effect (`tool_result`) at a LOWER seq than the cause (`guardrail`), which the
    caller only records afterwards.

    MUTATION that must turn this red: delete the `if is_error and payload.get(
    "denied_by"): return result, is_error, False` early return from execute_and_record —
    the row lands inside the transaction again, `recorded` is True, and run_events grows
    a tool_result row nobody will follow with the denial that caused it.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    registry = build_registry(db, KB_DIR)
    ticket_id = _seed_ticket(db)
    recorder = RunRecorder(db, run_uid="u1", ticket_id=ticket_id)

    result, is_error, recorded = recorder.execute_and_record(
        bind_to_ticket(ticket_id),
        spec=registry["send_reply"],
        name="send_reply",
        # Bound to ticket_id, so naming another one is denied by the ticket_binding guard.
        raw_input={"ticket_id": ticket_id + 1, "body": "Refunded — it lands in 3-5 days."},
        policy=ToolPolicy(),
        event_type="tool_result",
    )

    assert is_error is True
    assert json.loads(result)["denied_by"] == "ticket_binding"
    assert recorded is False, "the denial recorded its own row and took the lower seq"
    assert db.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
    # Nothing was written, which is why deferring the row costs no atomicity: there is
    # no sibling write that could survive without it.
    assert db.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0


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


def test_a_denied_write_tool_persists_cause_before_effect(client, capture_frames, monkeypatch):
    """CR-02: on a guardrail denial, run_events records the denial BEFORE its result.

    The suite scripted no denial at all before this, which is exactly why the inversion
    was invisible: `created_at` is datetime('now') (second resolution), so `seq` is the
    only tiebreaker phase 6 has, and it used to run backwards on the one path where
    order carries meaning — a safety control firing. An audit row showing a send_reply's
    result ahead of the denial that produced it is the wrong record of that event.

    MUTATION that must turn this red: delete the `if is_error and payload.get(
    "denied_by")` early return from RunRecorder.execute_and_record, so the denied call
    inserts its tool_result row inside the offload again. The row takes seq 3, the
    guardrail event that explains it takes seq 4, and the ordering assertions below
    fail while the SSE stream keeps showing the opposite (correct) order.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        # A write-tier tool naming a ticket that is not this run's: denied by the
        # ticket_binding guard, before the tool executes, so nothing is written.
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id + 1, "body": "z" * 40,
        })]),
        response([text_block("giving up")], stop_reason="end_turn"),
    ])

    body, frames = asyncio.run(capture_frames(ticket_id))

    streamed = _streamed_event_types(body)
    assert "guardrail" in streamed, (
        "the scripted call was not denied — every assertion below would be vacuous"
    )
    # The stream's order is the deliberate one and was never wrong; it is the reference.
    assert streamed.index("guardrail") < streamed.index("tool_result")

    reopened = connect(settings.db_path)
    try:
        run_uid = reopened.execute("SELECT run_uid FROM runs").fetchone()["run_uid"]
        rows = reopened.execute(
            "SELECT seq, type, payload FROM run_events WHERE run_uid = ? ORDER BY seq",
            (run_uid,),
        ).fetchall()
    finally:
        reopened.close()

    types = [r["type"] for r in rows]
    # Whole-sequence equality, not just the pair: a fix that reordered these two by
    # dropping a row or writing one twice would still pass a narrower check.
    assert types == streamed
    assert [r["seq"] for r in rows] == list(range(1, len(streamed) + 1))
    assert types.index("guardrail") < types.index("tool_result"), (
        "run_events recorded the denied call's result BEFORE the denial that caused it"
    )
    # The right two rows, and the denial is the one that fired: seq is a tiebreaker only
    # if the rows it orders are the ones this run actually produced.
    denial = rows[types.index("guardrail")]
    denied_result = rows[types.index("tool_result")]
    assert json.loads(denial["payload"])["guard"] == "ticket_binding"
    assert json.loads(denied_result["payload"])["is_error"] is True
    assert denial["seq"] + 1 == denied_result["seq"]
    # The public feed carries the same order (it is published as each event surfaces).
    published = [f["type"] for f in frames]
    assert published.index("guardrail") < published.index("tool_result")


def test_the_feed_distinguishes_a_denied_write_from_a_successful_one(
    client, capture_frames, monkeypatch
):
    """WR-01, end to end: one run, one denied send_reply and one that lands.

    Both take the same projection branch, so the only thing separating "the guardrail
    refused this" from "the customer was emailed" in the public feed is the error flag
    the branch carries. Driven through a real run rather than asserted on project()
    alone because the denial's dict shape — the thing that made it look like a success —
    is produced by _execute_guarded, not by the test.

    MUTATION that must turn this red: drop `"is_error": False` from the send_reply
    success branch of _project_tool_result and move the is_error branch below the
    dispatch — the two frames collapse to the same shape and the inequality fails.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        # Denied: a write-tier tool naming a ticket that is not this run's.
        response([tool_use_block("send_reply", {"ticket_id": ticket_id + 1, "body": "z" * 40})]),
        # Accepted: the same tool, this run's ticket.
        response([tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "y" * 40})]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    _body, frames = asyncio.run(capture_frames(ticket_id))

    replies = [f for f in frames if f["type"] == "tool_result" and f["tool"] == "send_reply"]
    assert len(replies) == 2, f"the run did not publish both calls: {replies}"
    denied, sent = replies
    assert denied["is_error"] is True, (
        "the denied reply published as a success — a viewer cannot tell a guardrail"
        " firing from an action the agent actually took"
    )
    assert denied["denied_by"] == "ticket_binding"
    assert sent["is_error"] is False
    assert sent["status"] == "resolved"
    assert denied["is_error"] != sent["is_error"]
    # The denial's payload stays out: the model's supplied ticket id and the retry
    # instruction are its own output, echoed from a ticket body we do not control. An
    # exact key set, so a field added to the failure branch later has to be a decision.
    assert set(denied) == {"type", "tool", "is_error", "denied_by", "run_uid", "ticket_id"}
    assert "not this run's ticket" not in json.dumps(denied)


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


def test_concurrent_runs_are_attributable_in_the_feed(client, monkeypatch):
    """CR-03: every published frame names the run and ticket it belongs to.

    Two runs in flight at once, which is the case the service admits by design
    (RunRegistry holds a dict of them, the connect snapshot renders a list) and the
    case a single-run test cannot see: without identity a subscriber gets interleaved
    tool_use / usage / resolution frames and no way to tell whose cost or whose outcome
    each one was. Phase 6's per-run cards are unbuildable from that, and the frames
    cannot be joined to the run_events rows this phase writes.

    MUTATION 1 that must turn this red: publish `frame` instead of
    `attribute_to_run(frame, ...)` in event_stream — every frame loses its identity and
    the first assertion fails.
    MUTATION 2 that must turn this red: stamp a constant (`run_uid="run"`) — the frames
    then all claim one run, the two tickets collapse into it, and the 1:1 assertion
    fails. That is the actual defect: attribution that exists but does not distinguish.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_a = _make_ticket(client, "ava@acmecorp.com", "charged twice for March", "Refund")
    ticket_b = _make_ticket(client, "liam@brightco.io", "what are my rate limits?", "Limits")
    # Replies to whichever ticket the prompt names, so one client can serve two
    # genuinely overlapping runs.
    app.state.client = TicketAwareFakeClient()

    async def drive():
        q = app.state.broker.subscribe()
        try:
            async def run(ticket_id):
                stream = await process_ticket(ticket_id)
                async for _chunk in stream.body_iterator:
                    # Hand the loop back between steps, which is what makes these two
                    # runs interleave rather than execute one after the other.
                    await asyncio.sleep(0)

            await asyncio.gather(run(ticket_a), run(ticket_b))
        finally:
            app.state.broker.unsubscribe(q)
        frames = []
        while True:
            try:
                frames.append(q.get_nowait())
            except asyncio.QueueEmpty:
                return frames

    frames = asyncio.run(drive())

    assert frames, "the feed received nothing — this test would prove nothing"
    unattributed = [f for f in frames if "run_uid" not in f or "ticket_id" not in f]
    assert unattributed == [], f"published frames carry no run identity: {unattributed}"

    # The bug is conflation, so the assertion is on the MAPPING, not on presence: each
    # uid must belong to exactly one ticket, and the two runs must be two runs.
    by_uid: dict[str, set[int]] = {}
    for frame in frames:
        by_uid.setdefault(frame["run_uid"], set()).add(frame["ticket_id"])
    assert len(by_uid) == 2, f"two runs published under {len(by_uid)} identities: {by_uid}"
    assert all(len(tickets) == 1 for tickets in by_uid.values()), (
        f"a run's frames were attributed to more than one ticket: {by_uid}"
    )
    assert {t for tickets in by_uid.values() for t in tickets} == {ticket_a, ticket_b}

    # Genuinely interleaved: if the two runs had serialised, the frame stream would be
    # one run's frames then the other's, and this defect would be invisible again.
    ordering = [f["run_uid"] for f in frames]
    switches = sum(1 for a, b in itertools.pairwise(ordering) if a != b)
    assert switches > 1, (
        f"the two runs did not overlap in the feed ({switches} switches) — the"
        " conflation this test exists to catch could not have occurred"
    )

    # And the identity joins to the durable record: the same pairs, from the runs table.
    reopened = connect(settings.db_path)
    try:
        rows = reopened.execute("SELECT run_uid, ticket_id FROM runs").fetchall()
    finally:
        reopened.close()
    assert {(r["run_uid"], r["ticket_id"]) for r in rows} == {
        (uid, next(iter(tickets))) for uid, tickets in by_uid.items()
    }
    # Each run's own outcome is attributable, not just its tool calls.
    resolutions = {f["run_uid"] for f in frames if f["type"] == "resolution"}
    assert resolutions == set(by_uid), "a run's resolution frame belonged to no run"


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


# --------------------------------------------------------------------------------------
# Plan 04: the public /events transport. Everything below drives the real route generator
# (`await events()` -> `body_iterator`), because all four properties this wave must hold —
# live delivery, the initial snapshot, the idle close, and zero leaked subscribers — are
# properties of that generator's control flow and are invisible to a test of the broker.
# --------------------------------------------------------------------------------------

REPLY_SENTINEL = "SENTINEL-REPLY-BODY-3d9a71"


async def _next_chunk(it, timeout: float = 2.0) -> str:
    """One chunk, with a deadline. A hung read is a bug, not a slow test."""
    return await asyncio.wait_for(it.__anext__(), timeout)


async def _read_to_close(it, *, timeout: float, what: str) -> list[str]:
    """Every chunk until the generator returns. Fails loudly if it never does."""
    chunks: list[str] = []

    async def _read():
        async for chunk in it:
            chunks.append(chunk)

    try:
        await asyncio.wait_for(_read(), timeout=timeout)
    except TimeoutError:
        pytest.fail(f"{what} — read {len(chunks)} chunks and the stream was still open")
    return chunks


def test_events_delivers_a_live_run(client, monkeypatch):
    """SC-2: an open /events stream receives a run's redacted frames WHILE it runs.

    "While" is asserted, not assumed: each feed chunk is stamped with whether the run
    had already finished when it arrived, and at least one must have arrived before it
    did. A design that collected frames and flushed them at the end — or that a
    dashboard had to poll for — passes every other assertion here and fails that one.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    # Short, so a quiet moment costs 20ms of keep-alive rather than the 15s default; the
    # idle ceiling stays well clear so nothing closes underneath this test.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "events_idle_seconds", 5.0)
    ticket_id = _make_ticket(client, EMAIL_SENTINEL, f"charged twice. {BODY_SENTINEL}")
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": EMAIL_SENTINEL})]),
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": f"Refunded — it lands in 3-5 days. {REPLY_SENTINEL}",
        })]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    async def drive():
        stream = await events()
        it = stream.body_iterator
        first = await _next_chunk(it)
        run_finished = asyncio.Event()
        feed: list[tuple[str, bool]] = []

        async def reader():
            async for chunk in it:
                if chunk.startswith(":"):
                    continue  # keep-alive comment, not a frame
                feed.append((chunk, run_finished.is_set()))
                if "event: resolution" in chunk:
                    return

        async def runner():
            run = await process_ticket(ticket_id)
            async for _chunk in run.body_iterator:
                # Hand the loop back after every step the run emits, which is what a
                # real HTTP client does between chunks — and what lets the assertion
                # below distinguish "delivered live" from "delivered at the end".
                await asyncio.sleep(0)
            run_finished.set()

        reader_task = asyncio.create_task(reader())
        try:
            await runner()
            await asyncio.wait_for(reader_task, timeout=2.0)
        finally:
            reader_task.cancel()
            await it.aclose()
        return first, feed

    first, feed = asyncio.run(drive())

    assert "event: snapshot" in first
    assert feed, "the feed received nothing at all — this test would prove nothing"
    assert any(not finished for _chunk, finished in feed), (
        "every frame arrived only after the run had already ended — the feed is not live"
    )

    body = "".join(chunk for chunk, _ in feed)
    # Safe fields PRESENT: the tool names and the cost are what the dashboard renders,
    # and an allowlist tuned to leak nothing by publishing nothing is not a feed.
    assert "event: tool_use" in body
    for expected in ('"tool": "lookup_customer"', '"tool": "send_reply"', '"cost_usd"'):
        assert expected in body, f"the feed dropped {expected} — nothing left to show"
    assert "event: resolution" in body
    # Sensitive fields ABSENT: the customer's address, their ticket body and the reply
    # the agent sent them all passed through this run's events on the way here.
    for name, sentinel in (*SENTINELS, ("reply body", REPLY_SENTINEL)):
        assert sentinel not in body, f"the public feed leaked the {name}"
    assert len(app.state.broker._subs) == 0, "the closed feed left a subscriber behind"


def test_events_sends_initial_snapshot_on_connect(client):
    """D-14: the FIRST frame is what is running right now, before any live event.

    MUTATION that must turn this red: delete the `yield snapshot_frame(...)` line before
    the receive loop in main.py's stream(). The first chunk is then the live frame
    published below (or nothing at all until one is), and both assertions fail — a tab
    opened mid-run would sit on an empty feed until the next step happened.
    """
    async def drive():
        # A run already in flight when the viewer connects. Registered directly rather
        # than by driving a real run, because the snapshot has to be observed at a
        # moment mid-run, which a completed run cannot give us.
        token = app.state.runs.register(ticket_id=4242)
        try:
            stream = await events()
            it = stream.body_iterator
            first = await _next_chunk(it)
            # Published only AFTER the snapshot was read, so "the snapshot came first"
            # is a fact about ordering and not about which one happened to be ready.
            app.state.broker.publish({"type": "usage", "steps": 1, "cost_usd": 0.002})
            second = await _next_chunk(it)
            await it.aclose()
            return first, second
        finally:
            app.state.runs.deregister(token)

    first, second = asyncio.run(drive())

    assert first.startswith("event: snapshot\ndata: ")
    payload = json.loads(first.split("data: ", 1)[1].strip())
    assert payload["type"] == "snapshot"
    assert [run["ticket_id"] for run in payload["runs"]] == [4242], (
        "the in-flight run was not in the connect snapshot — a mid-run viewer sees nothing"
    )
    # Built field by field like project(): a monotonic clock reading is meaningless off
    # this process, so the frame carries elapsed time and nothing else.
    assert set(payload["runs"][0]) == {"ticket_id", "running_for_ms"}
    # And the live event is second, not first.
    assert second.startswith("event: usage\ndata: ")


def test_events_output_comes_only_from_two_serialisers(client, monkeypatch):
    """WR-04: nothing reaches a subscriber except project() or snapshot_frame() output.

    The redaction boundary is only a boundary if it is the WHOLE boundary. Both
    serialisers now live in events.py, and this asserts the /events generator has no
    third path: every non-comment chunk it yields must be traceable to one of the two.

    Done by tagging each serialiser's output and following the tags, rather than by
    reading main.py: a source-level check ("no other f-string in stream()") passes the
    moment someone yields a helper's return value instead, which is exactly how a third
    path gets added.

    Two independent properties, because the first mutation I ran survived the tag check
    alone: (1) every non-comment chunk carries a tag, so nothing but the two serialisers
    sourced it, and (2) each chunk's SSE event NAME equals its own payload's `type` and
    its key set is exactly what its serialiser produced, so no path may reframe or
    re-shape a frame on the way out either.

    MUTATION 1 that must turn this red: yield registry state directly from stream(),
    e.g. `yield f"event: runs\\ndata: {json.dumps([asdict(r) for r in
    app.state.runs.snapshot()])}\\n\\n"` — an untagged chunk, named by (1).
    MUTATION 2 that must turn this red: re-emit the projected frame under a second
    header, `yield f"event: raw\\ndata: {json.dumps(frame)}\\n\\n"` — tagged, since it is
    the same frame, but caught by (2)'s name check. That is the one the tag check alone
    survived, and it is why (2) exists.

    HONEST LIMIT: this pins the /events generator, not the broker. A frame published to
    the broker without going through project() would be tagged-looking here; that path
    is pinned separately by there being exactly one publish call site in src/.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "events_idle_seconds", 5.0)
    ticket_id = _make_ticket(client, EMAIL_SENTINEL, f"charged twice. {BODY_SENTINEL}")
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": EMAIL_SENTINEL})]),
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id, "body": f"Refunded. {REPLY_SENTINEL}",
        })]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    # Tagged stand-ins for the two sanctioned serialisers. Same shapes, so the route is
    # driven exactly as in production; the marker is the only thing added.
    monkeypatch.setattr(
        main, "snapshot_frame",
        lambda runs: 'event: snapshot\ndata: {"type": "snapshot", "via": "SNAPSHOT-MARKER"}\n\n',
    )
    monkeypatch.setattr(
        main, "project", lambda event: {"type": "usage", "via": "PROJECT-MARKER"}
    )

    async def drive():
        stream = await events()
        it = stream.body_iterator
        chunks = [await _next_chunk(it)]  # the connect frame
        run = await process_ticket(ticket_id)
        async for _chunk in run.body_iterator:
            await asyncio.sleep(0)
        # Read until a keep-alive proves the queue is drained, rather than racing a
        # concurrent reader: liveness is another test's claim, and every frame the run
        # published has to be in `chunks` for the "no third path" check to be complete.
        while True:
            chunk = await _next_chunk(it)
            chunks.append(chunk)
            if chunk.startswith(":"):
                break
        await it.aclose()
        return chunks

    chunks = asyncio.run(drive())

    # Comments are transport, not content: they carry no run data by construction and
    # are the one thing this route may emit that neither serialiser produced.
    frames = [c for c in chunks if not c.startswith(":")]
    assert frames, "the feed carried nothing — this test would prove nothing"
    untagged = [c for c in frames if "SNAPSHOT-MARKER" not in c and "PROJECT-MARKER" not in c]
    assert untagged == [], (
        f"/events yielded output neither serialiser produced — a third path exists: {untagged}"
    )
    # And each chunk is still shaped exactly as its own serialiser built it: the SSE
    # event name is the payload's own `type` (never a name the route invented) and the
    # keys are the serialiser's, plus the two identity fields attribute_to_run stamps.
    expected_keys = {
        "SNAPSHOT-MARKER": {"type", "via"},
        "PROJECT-MARKER": {"type", "via", "run_uid", "ticket_id"},
    }
    for chunk in frames:
        header, data = chunk.split("\ndata: ", 1)
        payload = json.loads(data.strip())
        assert header == f"event: {payload['type']}", (
            f"a chunk was framed under a name its own payload does not claim: {chunk!r}"
        )
        assert set(payload) == expected_keys[payload["via"]], (
            f"a chunk was re-shaped between its serialiser and the wire: {chunk!r}"
        )
    # Both were genuinely exercised, or "only two paths" would hold vacuously for a
    # route that never published a live frame.
    assert any("SNAPSHOT-MARKER" in c for c in frames)
    assert any("PROJECT-MARKER" in c for c in frames)


def test_snapshot_frame_is_an_allowlist_over_registry_state():
    """WR-04: the second serialiser, tested where it now lives.

    Takes `list[ActiveRun]` and nothing else — no app, no request, no registry — which
    is what let it move out of main.py and sit beside project().

    MUTATION that must turn this red: build the frame with `asdict(run)` instead of
    naming the two fields — `started_at` (a raw monotonic clock reading, meaningless off
    this process) is published, and the exact-key assertion fails. That spread is the
    same denylist-in-allowlist's-clothing project() refuses.
    """
    frame = snapshot_frame([ActiveRun(ticket_id=4242, started_at=time.monotonic() - 1.5)])

    assert frame.startswith("event: snapshot\ndata: ")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["type"] == "snapshot"
    assert set(payload["runs"][0]) == {"ticket_id", "running_for_ms"}
    assert payload["runs"][0]["ticket_id"] == 4242
    assert payload["runs"][0]["running_for_ms"] >= 1500
    assert snapshot_frame([]).endswith('{"type": "snapshot", "runs": []}\n\n')


def test_events_disconnect_unsubscribes(client):
    """CR-02: neither a stream that never starts nor one that disconnects leaks a queue.

    MUTATION that must turn this red: subscribe outside the generator body (call
    `app.state.broker.subscribe()` in the `events()` handler, above `async def stream`)
    — the never-started half then leaves one queue behind, because a finally in a body
    that never ran does not execute. The second half falls to moving `unsubscribe(q)`
    out of the finally and below the loop, where a disconnect skips it entirely.

    A leaked subscriber is not merely untidy: publish keeps writing into a queue nobody
    reads for the life of the process, so every later run pays for a viewer that left.
    """
    async def never_starts():
        # The body is deliberately never iterated — Starlette can cancel a
        # StreamingResponse before its generator starts, which is exactly this.
        await events()

    asyncio.run(never_starts())
    assert len(app.state.broker._subs) == 0, "a stream that never started leaked a subscriber"

    async def starts_then_disconnects():
        stream = await events()
        it = stream.body_iterator
        await _next_chunk(it)  # connected: the snapshot frame arrived
        assert len(app.state.broker._subs) == 1, (
            "the connected stream never subscribed — the disconnect assertion below"
            " would pass against a route that reads nothing"
        )
        await it.aclose()  # the client goes away mid-stream

    asyncio.run(starts_then_disconnects())
    assert len(app.state.broker._subs) == 0, "a disconnected stream leaked a subscriber"


def test_events_heartbeats_then_idle_closes(client, monkeypatch):
    """D-09/SC-4: a stream fed nothing but its own heartbeats still closes.

    MUTATION that must turn this red: reset the idle deadline in the heartbeat branch
    of main.py's stream() (`idle_deadline = time.monotonic() + settings.events_idle_seconds`
    beside the `yield ": keep-alive"`). The stream then renews itself forever, the read
    below never ends, and this fails on the timeout. That bug is invisible in production
    except as a Fly machine that never reaches `stopped` — min_machines_running=0
    silently defeated by the server's own keep-alive — which is why the heartbeat-only
    case is the one asserted here rather than a quiet-then-busy one.
    """
    # Several heartbeats inside the ceiling, so "it emitted keep-alives AND still closed"
    # is one observation rather than two runs. Real values are 15s/300s.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "events_idle_seconds", 0.2)

    async def drive():
        stream = await events()
        return await _read_to_close(
            stream.body_iterator,
            timeout=3.0,
            what="a heartbeat-only /events stream never idle-closed (D-09)",
        )

    chunks = asyncio.run(drive())

    keep_alives = [c for c in chunks if c == ": keep-alive\n\n"]
    assert len(keep_alives) >= 2, (
        f"the stream closed without heartbeating first ({chunks!r}) — a quiet-but-live"
        " feed would be dropped by a proxy before the ceiling was ever reached"
    )
    # Nothing but the connect snapshot and comments: no run published anything, so a
    # frame here would mean the feed invented one.
    assert [c for c in chunks if not c.startswith(":")] == [chunks[0]]
    assert chunks[0].startswith("event: snapshot")
    assert len(app.state.broker._subs) == 0, "the idle-closed stream leaked a subscriber"


# --------------------------------------------------------------------------------------
# Code review CR-01: the public feed is bounded. /events is the only route that holds a
# connection open for minutes, and D-11 keeps it keyless — so the two bounds below are
# the whole of its perimeter: a per-IP window on connects, and a hard ceiling on
# concurrent subscribers. Both are properties of the route, invisible to a broker test.
# --------------------------------------------------------------------------------------


def test_events_connects_are_rate_limited_per_ip(client, monkeypatch):
    """CR-01: an anonymous reconnect loop is metered like every other public surface.

    MUTATION that must turn this red: drop `dependencies=[Depends(events_gate)]` from the
    /events route decorator — the second connect then returns 200 and the endpoint is
    back to unlimited free connection-holding, which is how a `while :; do curl -sN
    /events & done` loop pins the Fly machine at `started` and defeats
    min_machines_running=0 (the milestone's own budget constraint).

    Metered as a route DEPENDENCY and never middleware: BaseHTTPMiddleware buffers a
    StreamingResponse, and the status line is locked at 200 the moment it yields.
    """
    monkeypatch.setattr(settings, "anon_events_limit", "1/minute")
    # So the admitted stream closes on its own instead of holding the TestClient open
    # for the 300s ceiling — this test is about the connect, not the stream.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.01)
    monkeypatch.setattr(settings, "events_idle_seconds", 0.02)

    first = client.get("/events")
    second = client.get("/events")

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    assert second.status_code == 429, "a second connect from the same IP was not metered"
    assert second.json()["detail"]["error"] == "rate_limited"
    # A real status code, not an in-stream error on a 200: the refusal has to happen
    # before the generator starts, which is why the gate is a dependency.
    assert second.headers["Retry-After"]
    assert second.headers["content-type"].startswith("application/json")
    assert len(app.state.broker._subs) == 0, "a refused connect left a subscriber behind"


def test_events_refuses_a_viewer_past_the_subscriber_cap(client, monkeypatch):
    """CR-01: the (cap+1)th viewer is refused with a 503, and leaks no queue.

    MUTATION that must turn this red: delete the `if app.state.broker.at_capacity` block
    from the /events handler — the second connect then subscribes, `_subs` grows past
    the cap, and both the refusal and the no-leak assertion fail. Without the cap the
    cost of a publish is chosen by whoever opens the most connections, on the same loop
    that answers the 3s container HEALTHCHECK.
    """
    monkeypatch.setattr(settings, "events_max_subscribers", 1)
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "events_idle_seconds", 5.0)

    async def drive():
        stream = await events()
        it = stream.body_iterator
        await _next_chunk(it)  # admitted: the snapshot arrived, so the slot is taken
        assert len(app.state.broker._subs) == 1, (
            "the first viewer never subscribed — the cap assertion below would pass"
            " against a route that admits nobody"
        )
        with pytest.raises(HTTPException) as refused:
            await events()
        # The refusal allocated nothing: still exactly the one admitted viewer.
        assert len(app.state.broker._subs) == 1, "the refused viewer leaked a queue"
        await it.aclose()
        return refused.value

    exc = asyncio.run(drive())

    assert exc.status_code == 503
    assert exc.detail["error"] == "too_many_viewers"
    assert exc.headers["Retry-After"]
    assert len(app.state.broker._subs) == 0


def test_events_refuses_in_stream_when_the_last_slot_goes_after_admission(client, monkeypatch):
    """CR-01: losing the race for the last slot ends the stream cleanly, not with a raise.

    The handler's capacity check and the generator's subscribe() are two statements with
    a scheduling point between them, so a second connection can take the last slot in
    between. By then the status line is locked at 200 and the only refusal left is
    in-stream. Simulated by making subscribe() refuse an admitted connection — the exact
    interleaving, deterministically, without racing two real connections.

    MUTATION that must turn this red: drop the `try/except BrokerUnavailable` around
    `q = app.state.broker.subscribe()` in stream() — BrokerUnavailable then propagates
    into the SSE path as a 500-shaped crash mid-response, and the chunk assertion fails.
    """
    # Short, so a regression that keeps the stream open fails fast rather than sitting
    # out the 300s ceiling.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.01)
    monkeypatch.setattr(settings, "events_idle_seconds", 0.05)

    def taken_by_someone_else(self):
        raise BrokerUnavailable("too many live viewers")

    # The handler admitted this connection; the last slot went before the body ran.
    monkeypatch.setattr(RunEventBroker, "subscribe", taken_by_someone_else)

    async def drive():
        stream = await events()
        return [chunk async for chunk in stream.body_iterator]

    chunks = asyncio.run(drive())

    # A comment, never an event: nothing on this path went through project(), and the
    # stream ends immediately rather than holding a connection it cannot feed.
    assert chunks == [": at-capacity\n\n"]
    assert len(app.state.broker._subs) == 0, "the raced refusal leaked a queue"


def test_events_viewer_is_not_a_registered_run(client, monkeypatch):
    """D-12/SC-4: a viewer is not a run — it never enters RunRegistry, so it cannot
    stall the shutdown drain — and lifespan's broker.close() ends it.

    MUTATION that must turn this red: call `app.state.runs.register(ticket_id=0)` inside
    main.py's stream() (the natural mistake — event_stream registers there, so the shape
    is right next door). `active` becomes 1, and the drain below then waits out its full
    grace period and returns False instead of draining instantly. Every deploy would
    burn the graceful-shutdown window for as long as one tab stayed open, and the
    machine would never autostop.
    """
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "events_idle_seconds", 5.0)

    async def drive():
        stream = await events()
        it = stream.body_iterator
        await _next_chunk(it)  # connected and subscribed
        assert len(app.state.broker._subs) == 1

        active_with_a_viewer = app.state.runs.active
        snapshot_with_a_viewer = app.state.runs.snapshot()
        # The consequence, not just the count: a drain with a viewer attached must take
        # the empty-registry fast path. The short timeout is what makes a stalled drain
        # a failed assertion rather than a slow test.
        drained = await app.state.runs.drain(timeout=0.05)

        # Shutdown's other half: close() wakes the open stream with the sentinel, so it
        # returns instead of sitting out its idle ceiling inside uvicorn's window.
        app.state.broker.close()
        remaining = await _read_to_close(
            it, timeout=2.0, what="broker.close() did not end the open /events stream"
        )
        return active_with_a_viewer, snapshot_with_a_viewer, drained, remaining

    active, snapshot, drained, remaining = asyncio.run(drive())

    assert active == 0, "the /events viewer registered as a run — the drain will wait for it"
    assert snapshot == []
    assert drained is True, "the drain did not complete with a viewer attached (D-12)"
    # The sentinel is not a frame: the stream ends on it, it is never serialised out.
    assert [c for c in remaining if not c.startswith(":")] == []
    assert len(app.state.broker._subs) == 0

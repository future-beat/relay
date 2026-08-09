"""Phase 2 lifecycle: the thread-offload seam, the in-flight run registry, and shutdown drain.

The registry is constructed per test rather than pulled off `app.state`. That is
not just isolation — an `asyncio.Event` binds to the loop it is first awaited on,
so a registry shared across tests would be awaited on the wrong loop.
"""

import asyncio
import inspect
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from helpers import (
    FakeClient,
    TicketAwareFakeClient,
    response,
    text_block,
    tool_use_block,
)
from relay.agent import run_ticket
from relay.main import app, process_ticket
from relay.mcp_server import build_mcp_registry
from relay.ratelimit import reserved_usd
from relay.runs import RunRegistry

_REPO_ROOT = Path(__file__).parent.parent
_KB_DIR = _REPO_ROOT / "kb"

# --- shutdown drain ---


async def test_drain_returns_immediately_when_idle():
    # The timeout is deliberately generous: a fast path that actually works is
    # indistinguishable from a short timeout unless the budget is far below it.
    # Every deploy of an idle machine pays this, so it has to be free.
    registry = RunRegistry()

    started = time.perf_counter()
    drained = await registry.drain(timeout=5.0)
    elapsed = time.perf_counter() - started

    assert drained is True
    assert elapsed < 0.05
    assert registry.draining is True


async def test_drain_waits_for_an_in_flight_run_then_returns():
    registry = RunRegistry()
    token = registry.register(ticket_id=1)

    task = asyncio.create_task(registry.drain(timeout=5.0))
    # Hand control back long enough for the drain to reach its wait. If it did not
    # actually wait, the assertion below catches it here rather than at the await.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    assert registry.draining is True

    registry.deregister(token)

    assert await task is True
    assert registry.active == 0


async def test_drain_times_out_rather_than_hanging_shutdown():
    # Why this exists: a drain that raises or hangs turns a routine deploy into a
    # SIGKILL, and Fly's kill_timeout is the only backstop left at that point. The
    # run is never deregistered on purpose — that is the shape of a stuck stream.
    registry = RunRegistry()
    registry.register(ticket_id=2)

    drained = await registry.drain(timeout=0.05)

    assert drained is False
    assert registry.active == 1
    assert registry.draining is True


# --- the sync executor contract ---


def test_no_registered_executor_is_a_coroutine_function(conn, registry):
    # Why this exists rather than what it does: the failure it guards is silent. An
    # `async def` executor returns a coroutine object, `_execute_guarded` hands that
    # straight back as the tool result, and the model receives a repr of a coroutine
    # where a JSON record should be — no exception, no warning, nothing in the logs.
    # There is no mypy in CI to notice the drift, so this assertion is the whole of
    # the enforcement behind D-01's sync `ToolSpec.execute` contract (D-02).
    #
    # The MCP registry is covered as well as the agent's because D-03 freezes
    # mcp_server.py: if that file ever drifts, it cannot be adjusted to match, so the
    # test has to be the thing that notices.
    mcp_registry = build_mcp_registry(conn, _KB_DIR)
    assert registry, "agent registry is empty — every assertion below would be vacuous"
    assert mcp_registry, "MCP registry is empty — every assertion below would be vacuous"

    for label, built in (("agent", registry), ("mcp", mcp_registry)):
        for name, spec in built.items():
            assert not inspect.iscoroutinefunction(spec.execute), (
                f"{label} registry's {name}.execute is a coroutine function;"
                " tool execution is offloaded with asyncio.to_thread and would return"
                " an un-awaited coroutine object to the model"
            )


async def test_tool_execution_runs_off_the_event_loop(registry):
    # Proves the offload happened, not merely that the run still works: a registry
    # whose tools were called inline would pass every other test in this file while
    # holding the loop for the length of a blocking SQLite read. Thread identity is
    # the deterministic signal — timing is not, and a sleep would only add flake.
    observed: list[int] = []
    base = registry["search_docs"]

    def recording_execute(query: str) -> str:
        observed.append(threading.get_ident())
        return base.execute(query=query)

    probe = {"search_docs": replace(base, execute=recording_execute)}
    client = FakeClient([
        response([tool_use_block("search_docs", {"query": "rate limits"})]),
        response([text_block("Done.")], stop_reason="end_turn"),
    ])
    ticket = {
        "id": 1,
        "customer_email": "liam@brightco.io",
        "subject": "API limits",
        "body": "What are my rate limits?",
    }

    async for _ in run_ticket(client, probe, ticket):
        pass

    assert observed, "the probe tool never ran — the assertion below would be vacuous"
    assert observed[0] != threading.get_ident()


# --- concurrency and registry lifecycle ---


def _make_ticket(client, email: str = "liam@brightco.io") -> int:
    created = client.post(
        "/tickets",
        json={
            "customer_email": email,
            "subject": "API limits",
            "body": "What are my rate limits?",
        },
    )
    assert created.status_code == 201
    return created.json()["id"]


def test_a_result_is_materialised_before_another_thread_touches_the_connection(db):
    # Not in the plan; added because mutation-testing found a hole. Removing Result —
    # handing the caller a live sqlite3.Cursor to step after the lock has dropped — is
    # caught by the six-way probe below only about half of its invocations, and by
    # nothing in tests/test_db.py at all, whose barrier test covers the transaction
    # boundary rather than the read. That left the invariant the entire data layer
    # rests on guarded only by timing. This forces the interleaving instead of hoping
    # for it: the reader parks between issuing its query and reading the rows, and the
    # writer runs a burst of statements on the same connection inside that window.
    db.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", "Refund", "Please refund me"),
    )
    db.commit()

    read_issued = threading.Barrier(2, timeout=5)
    writer_done = threading.Barrier(2, timeout=5)
    rows: list = []
    failures: list = []

    def reader():
        try:
            result = db.execute("SELECT customer_email, status FROM tickets")
            # With a materialised Result the rows are already in hand at this point,
            # so nothing the writer does can reach them.
            read_issued.wait()
            writer_done.wait()
            rows.extend(result.fetchall())
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            failures.append(exc)

    def writer():
        try:
            read_issued.wait()
            for i in range(20):
                db.execute(
                    "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
                    (f"noise{i}@example.com", "noise", "noise"),
                )
            db.commit()
            writer_done.wait()
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert failures == []
    # Row contents, and the two columns research measured going null and empty.
    assert len(rows) == 1
    assert rows[0]["customer_email"] == "ava@acmecorp.com"
    assert rows[0]["status"] == "open"


def test_overlapping_runs_all_record_without_locking_errors(client):
    # The load-bearing test of the phase, and the reason every assertion below reads
    # row *contents*. A wrapper that hands back a live cursor for the caller to step
    # after the lock has been released does not fail loudly: research measured it
    # yielding tickets with `customer_email=None` and `status=''`, plus spurious 404s,
    # on 4 of 5 identical runs. A test that only asserted "nothing raised" would have
    # been green the whole time the app was feeding the model a null customer record.
    # Do not weaken these to an exception check.
    ticket_ids = [_make_ticket(client, f"person{i}@example.com") for i in range(6)]
    app.state.client = TicketAwareFakeClient()

    async def drive_all_six_at_once():
        # Pre-warm the default executor. Every DB touch reaches SQLite through
        # asyncio.to_thread, and on a cold pool the first six calls each pay for a
        # thread being created — which staggers them just enough that the runs queue
        # instead of overlapping. Without this the test still passes, but it stops
        # exercising the hazard it exists for.
        await asyncio.gather(*(asyncio.to_thread(bool) for _ in range(8)))

        async def one(ticket_id: int) -> str:
            stream = await process_ticket(ticket_id)
            return "".join([chunk async for chunk in stream.body_iterator])

        return await asyncio.gather(*(one(ticket_id) for ticket_id in ticket_ids))

    bodies = asyncio.run(drive_all_six_at_once())

    for body in bodies:
        assert "event: error" not in body
        assert "event: resolution" in body
        assert "event: done" in body

    conn = app.state.conn
    runs = conn.execute("SELECT ticket_id, outcome, cost_usd FROM runs").fetchall()
    assert len(runs) == 6
    assert {row["ticket_id"] for row in runs} == set(ticket_ids)
    assert {row["outcome"] for row in runs} == {"send_reply"}
    assert all(row["cost_usd"] > 0 for row in runs)

    replies = conn.execute("SELECT ticket_id, body FROM replies").fetchall()
    assert len(replies) == 6
    assert {row["ticket_id"] for row in replies} == set(ticket_ids)
    assert all(len(row["body"]) >= 20 for row in replies)

    tickets = conn.execute("SELECT id, status, customer_email FROM tickets").fetchall()
    assert len(tickets) == 6
    assert {row["status"] for row in tickets} == {"resolved"}
    # The exact column the broken design nulled out, asserted per row rather than in
    # aggregate so a single corrupted read is enough to fail this.
    for row in tickets:
        assert row["customer_email"] == f"person{ticket_ids.index(row['id'])}@example.com"


def test_registry_is_empty_after_a_run_completes(client):
    # The scale-to-zero guard (D-06). "Cheap to keep running" is a core-value
    # constraint, and one leaked entry means every later drain waits out its full
    # grace period and the machine never reaches `stopped` — a cost regression that
    # nothing else in the suite would show.
    ticket_id = _make_ticket(client)
    app.state.client = FakeClient([
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Hi Liam — your Pro plan allows 600 requests/minute per workspace.",
        })]),
        response([text_block("Done.")], stop_reason="end_turn"),
    ])

    resp = client.post(f"/tickets/{ticket_id}/process")

    assert resp.status_code == 200
    assert "event: done" in resp.text
    assert app.state.runs.active == 0
    assert app.state.runs.snapshot() == []


def test_a_stream_that_never_starts_registers_nothing(client):
    # The CR-02 asymmetry, and the reason registration lives inside the generator
    # body rather than beside reserve_run(): Starlette can cancel a StreamingResponse
    # before its generator ever starts, and a `finally` in a generator whose body
    # never began does not run. Registered one line higher up, this request would
    # leak an entry that no deregister will ever balance. The body is never iterated
    # here on purpose.
    ticket_id = _make_ticket(client)

    asyncio.run(process_ticket(ticket_id))

    assert app.state.runs.active == 0
    assert app.state.runs.snapshot() == []


def test_process_returns_503_while_draining(client):
    # D-09. A new paid run must not start against a database that is about to close.
    # The flag is set directly rather than by shutting the app down, and reset in a
    # finally so it cannot leak into the next test sharing this TestClient's app.
    ticket_id = _make_ticket(client)
    app.state.runs.draining = True
    try:
        resp = client.post(f"/tickets/{ticket_id}/process")
    finally:
        app.state.runs.draining = False

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "shutting_down"
    assert detail["note"]
    assert resp.headers["Retry-After"]
    # Refused ahead of reserve_run(), so a rejected caller never claims spend it will
    # not use — the same ordering the daily-ceiling refusal uses.
    assert reserved_usd() == 0.0
    assert app.state.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_cancelled_run_task_still_records_and_drains(client):
    # SC-3/SC-4 end to end, and the only test here that drives the mechanism uvicorn
    # actually uses. On shutdown uvicorn calls task.cancel() on in-flight request
    # tasks and then runs lifespan shutdown without awaiting those cancellations —
    # that race is the whole justification for D-04/D-05. The existing D-07
    # regression test uses body_iterator.aclose(), which throws GeneratorExit
    # synchronously; task.cancel() delivers CancelledError asynchronously. Both end
    # in the same `finally`, but only one is the real path, so neither test is
    # redundant — do not delete either as a near-duplicate of the other.
    ticket_id = _make_ticket(client)

    async def cancel_the_run_the_way_uvicorn_does():
        second_call_started = asyncio.Event()
        never_released = asyncio.Event()
        scripted = iter([
            response([tool_use_block("search_docs", {"query": "rate limits"})]),
            response([text_block("Done.")], stop_reason="end_turn"),
        ])
        calls = 0

        async def create(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                # Parks the run inside a model call so the cancellation below is
                # guaranteed to land mid-stream rather than racing a finished run.
                second_call_started.set()
                await never_released.wait()
            return next(scripted)

        app.state.client = SimpleNamespace(messages=SimpleNamespace(create=create))

        stream = await process_ticket(ticket_id)
        first = await stream.body_iterator.__anext__()
        assert first.startswith("event: usage")
        assert app.state.runs.active == 1

        async def consume_the_rest():
            async for _ in stream.body_iterator:
                pass

        task = asyncio.create_task(consume_the_rest())
        await asyncio.wait_for(second_call_started.wait(), timeout=5.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        await app.state.runs.drain(timeout=2.0)
        assert app.state.runs.active == 0

    asyncio.run(cancel_the_run_the_way_uvicorn_does())

    rows = app.state.conn.execute("SELECT ticket_id, cost_usd, outcome FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == ticket_id
    assert rows[0]["cost_usd"] > 0
    assert rows[0]["outcome"] == "incomplete"

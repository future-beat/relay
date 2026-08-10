"""Phase 2 lifecycle: the thread-offload seam, the in-flight run registry, and shutdown drain.

The registry is constructed per test rather than pulled off `app.state`. That is
not just isolation — an `asyncio.Event` binds to the loop it is first awaited on,
so a registry shared across tests would be awaited on the wrong loop.
"""

import asyncio
import inspect
import re
import sqlite3
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import (
    FakeClient,
    TicketAwareFakeClient,
    response,
    text_block,
    tool_use_block,
)
from relay.agent import run_ticket
from relay.config import settings
from relay.db import connect
from relay.main import app, lifespan, process_ticket
from relay.mcp_server import build_mcp_registry
from relay.ratelimit import reserved_usd
from relay.runs import RegistryDraining, RunRegistry
from relay.telemetry import record_run
from test_db import instrument_write_grouping

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


async def test_register_refuses_once_the_drain_has_returned():
    # "Stop admitting runs, then wait for the in-flight ones" is drain()'s stated
    # contract, but the refusal used to live only in main.py's handler — so the
    # registry's own guarantee was weaker than its docstring, and a caller that
    # registered after a completed drain silently got a run against a database that
    # was about to close. The fast path makes this the *easy* case, not a rare one:
    # an idle registry drains in microseconds and every later register still succeeded.
    registry = RunRegistry()

    assert await registry.drain(timeout=5.0) is True

    with pytest.raises(RegistryDraining):
        registry.register(ticket_id=1)
    assert registry.active == 0, "a run was admitted after the drain declared itself done"
    # Still drained: the refusal must not leave the registry non-empty, which would
    # make the second drain burn its full grace period and return False.
    assert await registry.drain(timeout=0.05) is True


def test_one_registry_can_drain_on_two_different_event_loops():
    # asyncio.Event binds to the loop that first awaits it. Held as an attribute from
    # __init__, the second drain raised "... is bound to a different event loop" out of
    # lifespan shutdown. Only the fast path hid it: on an empty registry drain() returns
    # before touching the event, so every existing test took the branch that never
    # binds. Both drains below wait for real, which is the only way this is visible.
    # Two asyncio.run() calls and not one loop, because the binding is per-loop —
    # nothing about this reproduces inside a single loop.
    registry = RunRegistry()

    async def drain_with_a_run_actually_in_flight() -> bool:
        token = registry.register(ticket_id=1)
        task = asyncio.create_task(registry.drain(timeout=5.0))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done(), "the drain took its fast path — this proves nothing"
        registry.deregister(token)
        return await task

    assert asyncio.run(drain_with_a_run_actually_in_flight()) is True
    # A process drains once; the registry is the primitive phase 5 reuses, and the
    # suite already drives it from both a TestClient portal loop and asyncio.run().
    # Cleared by hand because register() now refuses while draining.
    registry.draining = False
    assert asyncio.run(drain_with_a_run_actually_in_flight()) is True


# --- the drain as lifespan actually wires it ---
#
# Everything above builds its own RunRegistry and calls drain() by hand, which tests the
# primitive and nothing about the deliverable. Both mutations that matter — deleting the
# drain from lifespan, and moving it after conn.close() (Pitfall 5, exactly) — left the
# whole suite green. The two tests below are the ones that fail on each.


async def _lifespan_against_a_scratch_database(monkeypatch, tmp_path):
    """Point the app at a throwaway file and hand back the real lifespan."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "shutdown.db")
    # The client fixture sets this too; lifespan constructs AsyncAnthropic either way.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    return lifespan(app)


async def test_lifespan_drains_before_it_closes_the_connection(monkeypatch, tmp_path):
    # SC-3/SC-4's ordering, asserted as ordering. The run is deregistered on a timer
    # from outside the lifespan so the drain has something to actually wait for —
    # against an empty registry drain() takes its fast path and proves nothing.
    order: list[str] = []

    async with await _lifespan_against_a_scratch_database(monkeypatch, tmp_path):
        runs = app.state.runs
        token = runs.register(ticket_id=1)
        real_close = app.state.conn.close

        def recording_close() -> None:
            order.append("close")
            real_close()

        monkeypatch.setattr(app.state.conn, "close", recording_close)

        async def finish_the_run_after_shutdown_has_begun() -> None:
            await asyncio.sleep(0.05)
            order.append("deregistered")
            runs.deregister(token)

        task = asyncio.create_task(finish_the_run_after_shutdown_has_begun())
        assert runs.active == 1, "nothing in flight — the drain below would be vacuous"

    await task
    assert order == ["deregistered", "close"], (
        "lifespan closed the connection without waiting for the in-flight run"
    )


async def test_lifespan_shutdown_lets_an_in_flight_run_finish_writing_its_row(
    monkeypatch, tmp_path
):
    # The same ordering as the test above, but asserted through its consequence rather
    # than through call order: a run still in flight at shutdown must be able to reach
    # the database. Close first and its write raises "Cannot operate on a closed
    # database" and the row — spend the daily ceiling can never see again — is lost.
    # That is Pitfall 5, and it is the reason the drain exists at all.
    db_path = tmp_path / "shutdown.db"
    failures: list = []

    async with await _lifespan_against_a_scratch_database(monkeypatch, tmp_path):
        conn = app.state.conn
        runs = app.state.runs
        token = runs.register(ticket_id=7)

        async def finish_the_run_the_way_event_stream_does() -> None:
            await asyncio.sleep(0.05)
            try:
                record_run(
                    conn,
                    ticket_id=7,
                    model="test-model",
                    duration_ms=1,
                    steps=1,
                    input_tokens=1000,
                    output_tokens=500,
                    cost_usd=0.021,
                    outcome="incomplete",
                )
            except BaseException as exc:  # noqa: BLE001 — reported below, not swallowed
                failures.append(exc)
            finally:
                runs.deregister(token)

        task = asyncio.create_task(finish_the_run_the_way_event_stream_does())

    await task
    assert failures == [], f"the in-flight run's write hit a closed database: {failures}"
    assert runs.active == 0

    # Re-opened from disk: the point is that the row is durable, not that it was
    # visible on a connection the shutdown was supposed to have closed by now.
    verify = connect(db_path)
    try:
        rows = verify.execute("SELECT ticket_id, cost_usd FROM runs").fetchall()
    finally:
        verify.close()
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == 7
    assert rows[0]["cost_usd"] == 0.021


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


def test_a_drain_between_the_handler_and_the_generator_refuses_the_run_in_stream(client):
    # The gap the handler's 503 cannot cover. The `draining` check runs in the handler;
    # the body that registers runs later, when Starlette starts sending the response.
    # A drain landing in between saw an empty registry, took its fast path, returned
    # True and let lifespan close the connection — and this run then started anyway.
    # Reproduced by draining at exactly that point. The status line is already locked
    # at 200 by then, so the refusal has to arrive as a stream event, not a 503.
    ticket_id = _make_ticket(client)
    # Scripted, so that a registry which fails to refuse produces a *complete* run
    # rather than an error event from the unauthenticated real client — the mutation
    # has to be distinguishable from the fix, not merely also broken.
    app.state.client = FakeClient([
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Hi Liam — your Pro plan allows 600 requests/minute per workspace.",
        })]),
        response([text_block("Done.")], stop_reason="end_turn"),
    ])

    async def drain_after_the_handler_but_before_the_body() -> str:
        stream = await process_ticket(ticket_id)
        assert await app.state.runs.drain(timeout=5.0) is True
        return "".join([chunk async for chunk in stream.body_iterator])

    try:
        body = asyncio.run(drain_after_the_handler_but_before_the_body())
    finally:
        app.state.runs.draining = False

    assert "shutting_down" in body, f"the run started against a draining registry: {body}"
    assert "event: done" not in body
    assert app.state.runs.active == 0
    # No agent run happened, so neither the ledger nor the reservation may show one.
    assert app.state.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert reserved_usd() == 0.0, "a refused run stranded its spend reservation"


def test_a_failed_record_run_still_releases_the_reservation_and_the_registry(client, monkeypatch):
    # CR-01. record_run is the only statement in event_stream's finally that touches
    # the database, and the likeliest way it fails is the race this phase closes: a
    # drain times out, lifespan closes the connection, and the write raises the exact
    # error below. Run sequentially in that finally, the exception skipped both
    # cleanups — and the registry leak is permanent, so every later drain burns its
    # full grace period and returns False for the life of the process. That is why
    # the registry assertion is the first one here: it is the unrecoverable half.
    ticket_id = _make_ticket(client)
    app.state.client = FakeClient([
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Hi Liam — your Pro plan allows 600 requests/minute per workspace.",
        })]),
        response([text_block("Done.")], stop_reason="end_turn"),
    ])

    def record_run_against_a_closed_database(*args, **kwargs):
        raise sqlite3.ProgrammingError("Cannot operate on a closed database")

    monkeypatch.setattr("relay.main.record_run", record_run_against_a_closed_database)

    escaped: list = []

    async def drive_one_run_to_completion() -> str:
        stream = await process_ticket(ticket_id)
        try:
            return "".join([chunk async for chunk in stream.body_iterator])
        except BaseException as exc:  # noqa: BLE001 — reported below, not swallowed
            escaped.append(exc)
            return ""

    body = asyncio.run(drive_one_run_to_completion())

    assert app.state.runs.active == 0, "a failed telemetry write leaked a registry entry"
    assert app.state.runs.snapshot() == []
    assert reserved_usd() == 0.0, "a failed telemetry write stranded a spend reservation"
    # The drain the leak would have broken still works, rather than waiting out its
    # timeout — the actual consequence, not just the counter that predicts it.
    assert asyncio.run(app.state.runs.drain(timeout=0.05)) is True
    # The caller is unaffected: losing the row must not also break the stream.
    assert escaped == [], f"the telemetry failure escaped to the client: {escaped}"
    assert "event: done" in body


def test_create_ticket_groups_its_insert_and_id_read_in_one_transaction(client, monkeypatch):
    # The fifth of the multi-statement writers, and the only one behind an HTTP
    # handler rather than a plain function, so it is covered here rather than beside
    # the other four in test_db.py. The unit of work is the INSERT plus the lastrowid
    # read: drop the transaction() and another thread's insert moves lastrowid between
    # them, handing this caller back somebody else's ticket id.
    statements, bare_commits = instrument_write_grouping(app.state.conn, monkeypatch)

    created = client.post(
        "/tickets",
        json={
            "customer_email": "liam@brightco.io",
            "subject": "API limits",
            "body": "What are my rate limits?",
        },
    )

    assert created.status_code == 201
    writes = [(sql, depth) for sql, depth in statements if sql.lstrip().upper().startswith("INSERT")]
    assert writes, "no INSERT was recorded — every assertion below is vacuous"
    # The handler's follow-up read is deliberately outside the block and not asserted
    # on: Result is materialised, so a read needs no transaction of its own.
    assert all(depth >= 1 for _, depth in writes), (
        f"create_ticket inserted outside a transaction(): {writes}"
    )
    assert bare_commits == [], "create_ticket committed outside a transaction()"


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


# --- shutdown timeout arithmetic ---


def test_shutdown_timeouts_nest_correctly():
    # Three timeouts, three files, three languages, and nothing else ties them
    # together. Fly's kill_timeout is the outermost budget; uvicorn's graceful window
    # has to expire inside it; the lifespan drain has to finish inside that. Widen or
    # narrow any one of them on its own and a deploy during an active run goes back to
    # being a SIGKILL that loses the run's row — the exact failure this phase exists
    # to remove, and one no unit test of the drain itself can see.
    fly = tomllib.loads((_REPO_ROOT / "fly.toml").read_text(encoding="utf-8"))
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Read off the parser, not a grep: a bare key written below a table header is
    # valid TOML that silently lands inside that table instead of at the top level.
    kill_timeout = fly["kill_timeout"]
    # D-08. Fly's docs disagree with themselves about the default and uvicorn treats
    # SIGINT and SIGTERM identically, so naming one can only ever make this worse.
    assert "kill_signal" not in fly

    # Without this the signal stops at the shell, which never forwards it.
    assert "exec uvicorn" in dockerfile

    window = re.search(r"--timeout-graceful-shutdown (\d+)", dockerfile)
    assert window, "the container CMD sets no uvicorn graceful-shutdown window"
    graceful_seconds = int(window.group(1))

    assert kill_timeout > graceful_seconds > settings.shutdown_drain_seconds


# --- the budget read is offloaded (WR-06) ---


async def test_the_daily_budget_read_runs_off_the_event_loop(client, monkeypatch):
    # The cost here is acquiring Database's lock, not running the SUM: a worker
    # thread holds that lock for a whole transaction(), so an inline read stalls the
    # loop for as long as the write takes — measured at 0.81s, bounded only by
    # busy_timeout (5s). The container HEALTHCHECK gives up at 3s, so a stalled loop
    # that cannot answer /health gets the machine restarted mid-run.
    #
    # The signal is whether a *running loop* exists in the calling thread, not thread
    # identity: TestClient drives the app from a portal thread, so comparing against
    # the test's own thread id passes even when the read is inline.
    import relay.main as main_module

    on_loop: list[bool] = []
    real = main_module.enforce_daily_budget

    def recording(conn):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return real(conn)

    monkeypatch.setattr(main_module, "enforce_daily_budget", recording)

    ticket_id = _make_ticket(client)
    # Drain-refusal short-circuits the handler *after* the gate dependency has run,
    # so the spend gate is exercised without starting a real agent run.
    app.state.runs.draining = True
    try:
        resp = client.post(f"/tickets/{ticket_id}/process")
    finally:
        app.state.runs.draining = False

    assert resp.status_code == 503
    assert on_loop, "enforce_daily_budget was never reached on a spend-metered route"
    assert not any(on_loop), (
        "the daily budget read ran with a live event loop in its thread, i.e. inline — "
        "it must be offloaded, because it blocks on Database's lock for the length of "
        "any in-flight write"
    )

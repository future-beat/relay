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

from helpers import FakeClient, response, text_block, tool_use_block
from relay.agent import run_ticket
from relay.mcp_server import build_mcp_registry
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

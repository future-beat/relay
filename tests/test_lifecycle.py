"""Phase 2 lifecycle: the thread-offload seam, the in-flight run registry, and shutdown drain.

The registry is constructed per test rather than pulled off `app.state`. That is
not just isolation — an `asyncio.Event` binds to the loop it is first awaited on,
so a registry shared across tests would be awaited on the wrong loop.
"""

import asyncio
import time

from relay.runs import RunRegistry

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

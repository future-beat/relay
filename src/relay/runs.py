"""In-flight agent-run tracking.

Phase 2 uses it to drain SSE streams before the database closes; phase 5's live
feed reads the same records to render what is running right now. Deliberately
not part of ratelimit.py: that module is scoped to burst limiting and the daily
spend ceiling, and run-lifecycle tracking is neither of those.

The registry is an instance created per app startup and stored on app.state,
never module-level state like ratelimit.py's reservations. An asyncio.Event
binds to the loop it is first awaited on, so a shared module-level instance
would outlive a TestClient's loop and fail against the next one with
"... bound to a different event loop". ratelimit.py gets away with globals only
because MemoryStorage constructs safely outside a running loop.
"""

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("relay.runs")


@dataclass(frozen=True)
class ActiveRun:
    """One streaming run. Plain fields, no validation — this never crosses the wire."""

    ticket_id: int
    started_at: float


class RunRegistry:
    """Holds only runs that are actually streaming.

    An idle server holds nothing, which is what keeps Fly's autostop free to
    suspend the machine. Tokens from itertools.count(), monotonic timestamps and
    idempotent removal all copy ratelimit.py's reservation tracker; the places
    this deliberately diverges from it are commented where they occur.
    """

    # Per-app state, so there is no reset hook here and nothing for conftest.py's
    # autouse fixture to clear. ratelimit.py needs reset_limits() only because its
    # buckets are process-wide and outlive an individual test.

    def __init__(self) -> None:
        self._active: dict[int, ActiveRun] = {}
        self._tokens = itertools.count()
        # Set means "nothing in flight", so a drain against an idle registry takes
        # the fast path instead of ever suspending.
        self._idle = asyncio.Event()
        self._idle.set()
        self.draining = False

    def register(self, *, ticket_id: int) -> int:
        """Admit one run, returning its token.

        No expiry, unlike ratelimit.py's reservations. Those are claimed in the
        handler and handed back in the generator, so a stream cancelled before it
        starts never releases its claim. Registration happens inside the generator
        itself, so register and deregister can be balanced — but that balance is the
        *caller's* guarantee, not this class's: it holds only because event_stream
        deregisters from a finally that nothing in its body can skip, telemetry
        failures included. A leaked entry is unrecoverable here — it pins the
        registry non-empty, so every later drain waits out its full grace period and
        returns False. Before adding a TTL to paper over that, check the caller's
        finally instead; a TTL would only hide the caller regressing.
        """
        token = next(self._tokens)
        self._active[token] = ActiveRun(ticket_id=ticket_id, started_at=time.monotonic())
        self._idle.clear()
        return token

    def deregister(self, token: int) -> None:
        """Retire one run. Idempotent, and never retires another run's."""
        self._active.pop(token, None)
        if not self._active:
            self._idle.set()

    @property
    def active(self) -> int:
        return len(self._active)

    def snapshot(self) -> list[ActiveRun]:
        """The live records, for phase 5's "what is running right now" projection."""
        return list(self._active.values())

    async def drain(self, *, timeout: float) -> bool:
        """Stop admitting runs, then wait for the in-flight ones. True if drained."""
        # Set before anything else, so it holds on the fast path too: refusing new
        # work is the first half of the contract, not a side effect of waiting.
        self.draining = True
        if not self._active:
            return True

        logger.info("shutdown.drain_started", extra={"ctx": {"active": len(self._active)}})
        try:
            # Wakes on the event rather than polling: a poll loop would add its own
            # interval to every clean teardown for no benefit.
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except TimeoutError:
            # The builtin, which asyncio's own name aliases on 3.11+. Returning
            # False rather than raising is what keeps a slow run from turning a
            # routine deploy into a SIGKILL.
            logger.warning(
                "shutdown.drain_timeout",
                extra={
                    "ctx": {
                        "active": len(self._active),
                        # ids and counts only — ctx passes straight through
                        # JsonFormatter to stdout (ASVS V7).
                        "tickets": [run.ticket_id for run in self._active.values()],
                    },
                },
            )
            return False

        logger.info("shutdown.drain_complete", extra={"ctx": {}})
        return True

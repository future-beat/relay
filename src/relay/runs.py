"""In-flight agent-run tracking.

Phase 2 uses it to drain SSE streams before the database closes; phase 5's live
feed reads the same records to render what is running right now. Deliberately
not part of ratelimit.py: that module is scoped to burst limiting and the daily
spend ceiling, and run-lifecycle tracking is neither of those.

The registry is an instance created per app startup and stored on app.state,
never module-level state like ratelimit.py's reservations. ratelimit.py gets
away with globals only because MemoryStorage constructs safely outside a
running loop.

Nothing loop-bound is held between drains either: an asyncio.Event binds to the
loop it is first awaited on, so one built in __init__ and kept as an attribute
fails the second drain with "... bound to a different event loop". The registry
outlives a single loop only by accident today — the eval harness and the test
suite both run more than one — so drain() builds its waiter on the loop that is
about to wait on it and drops it again on the way out.
"""

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("relay.runs")


class RegistryDraining(RuntimeError):
    """register() was called after drain() began. Raised, not returned as a sentinel,
    because there is no correct way for a caller to proceed: the connection this run
    would use is about to close."""


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
        # Deliberately not an asyncio.Event built here: it would bind to whichever
        # loop first awaited it and raise on every later one. drain() builds it, and
        # only when it is actually going to wait — an idle registry takes the fast
        # path and never needs a waiter at all.
        self._idle: asyncio.Event | None = None
        self.draining = False

    def register(self, *, ticket_id: int) -> int:
        """Admit one run, returning its token. Raises RegistryDraining once draining.

        The refusal is enforced here and not only at main.py's handler check, because
        the two are separated by an arbitrary scheduling gap: the handler checks
        `draining`, returns a StreamingResponse, and the generator body that calls this
        registers later. A drain landing in that gap took its fast path against an empty
        registry, returned True, and the connection closed — then this run started
        against a closed database. Enforcing it where the contract is declared also
        means phase 5's second registration site inherits it without knowing about
        main.py.

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
        if self.draining:
            raise RegistryDraining("registry is draining; refusing to admit a new run")
        token = next(self._tokens)
        self._active[token] = ActiveRun(ticket_id=ticket_id, started_at=time.monotonic())
        # No clear() to pair with deregister's set(): a waiter only exists while a
        # drain is waiting, and a drain in progress refuses new runs above.
        return token

    def deregister(self, token: int) -> None:
        """Retire one run. Idempotent, and never retires another run's."""
        self._active.pop(token, None)
        if not self._active and self._idle is not None:
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
        # Built here, on the loop that is about to wait on it, and dropped again
        # below: an asyncio.Event kept as an attribute binds to the first loop that
        # awaits it and raises "bound to a different event loop" on every later one.
        # Nothing but the fast path above was preventing that.
        self._idle = asyncio.Event()
        if not self._active:
            # Re-checked because deregister() only signals a waiter that exists: a
            # removal landing between the fast path and this line would otherwise
            # wait out the full grace period against an already-empty registry.
            self._idle = None
            return True
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
        finally:
            # Dropped whichever way this went, so no loop-bound object survives the
            # drain that built it. A timed-out drain matters most: the registry is
            # still non-empty, so those runs' deregisters are still to come.
            self._idle = None

        logger.info("shutdown.drain_complete", extra={"ctx": {}})
        return True

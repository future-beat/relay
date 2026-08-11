"""The three contracts the live feed is built from: fan-out, redaction, persistence.

They live together because they are the seam between one agent step and everything
that watches it, and each one fails in a way the others cannot catch:

- `RunEventBroker` fans a frame out to every watching browser. It is bounded and
  drop-oldest so a stalled viewer's queue can never backpressure the run that is
  paying Anthropic for the step (D-10).
- `project()` is the only path raw event data may take to become a public frame. It
  is an allowlist, built field by field: a denylist leaks the first field someone
  adds later, and the fields flowing past here include customer emails, ticket
  bodies and reply text (D-07).
- `RunRecorder` writes one `run_events` row per step. For a write-tier tool the row
  goes in the tool's OWN transaction as a savepoint, so the reply and the record of
  the reply commit or roll back together (D-04).

Nothing here is constructed at import: the broker holds `asyncio.Queue`s, which bind
to the loop they are first used on — the hazard `runs.py` documents at L60-64. The
broker is built in `lifespan` and held on `app.state`, like `RunRegistry`.
"""

import asyncio
import logging
from typing import Any

from .config import settings

logger = logging.getLogger("relay.events")

# Pushed to every subscriber by close() so open /events generators return instead of
# waiting out their idle ceiling while uvicorn holds the shutdown window open. An
# object(), not None or a dict: no published frame can ever be mistaken for it.
_CLOSE_SENTINEL = object()


class RunEventBroker:
    """Fans run frames out to every live `/events` subscriber.

    The sibling of `RunRegistry` — per-app-startup, held on `app.state`, never
    module-level — with the opposite teardown: viewers are not runs, so they are not
    drained, they are idle-closed. Deliberately not folded into the registry, which
    the phase-2 shutdown path waits on: a watching browser is not work to wait for.
    """

    def __init__(self, *, maxsize: int | None = None) -> None:
        self._subs: set[Any] = set()
        # Defaulted from settings rather than a literal so tests can build a tiny
        # broker without monkeypatching config, and deployment can tune the real one.
        self._maxsize = settings.events_queue_maxsize if maxsize is None else maxsize
        self.closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Drop one subscriber. Idempotent, mirroring RunRegistry.deregister.

        `discard`, not `remove`: this is called from a generator's finally, and the
        same queue can arrive here twice (the generator ending plus a close()). A
        KeyError raised out of a finally would mask whatever ended the stream, and a
        subscriber left behind is worse than a leaked registry entry — publish keeps
        writing to a queue nobody reads, for the life of the process.
        """
        self._subs.discard(q)

    def _offer(self, q, frame) -> None:
        """Hand one frame to one subscriber, dropping its oldest if it is full.

        Never blocks and never raises. The dropped frame belongs to a viewer who is
        not keeping up; the alternative — making the run wait — charges the cost of a
        slow browser to the ticket being processed.
        """
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                q.get_nowait()  # drop the oldest, keep the live end of the feed
                q.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                # A concurrent reader drained it between the two calls, or filled it
                # again. Either way the viewer is fine and the run must not care.
                pass

    def publish(self, frame: dict) -> None:
        """Fire-and-forget fan-out. A plain def on purpose (D-10).

        `async def` here would be a loaded gun: the moment anyone awaits a subscriber,
        a stalled dashboard tab can suspend the agent loop mid-run. Being synchronous
        makes that impossible rather than merely discouraged.
        """
        # Iterate a copy: unsubscribe() runs from generator finallys that can land
        # between frames, and mutating the set under iteration raises into the run.
        for q in tuple(self._subs):
            self._offer(q, frame)

    def close(self) -> None:
        """Wake every subscriber so open `/events` streams end. Called from lifespan.

        An open SSE connection is an in-flight connection uvicorn waits on during
        graceful shutdown, and it also keeps Fly's proxy from autostopping the
        machine. The sentinel rides the same drop-oldest path as a frame, so a full
        queue still receives it.
        """
        self.closed = True
        for q in tuple(self._subs):
            self._offer(q, _CLOSE_SENTINEL)

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
from .models import AgentEvent

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


def _project_tool_result(d: dict) -> dict:
    """The per-tool allowlist. This is where the leak risk concentrates.

    Every branch names its own fields, and the fallthrough for an unrecognised tool
    keeps only the name and the error flag — so a tool added later is redacted by
    default and someone has to come here on purpose to publish more of it.
    """
    tool = d.get("tool")
    result = d.get("result")
    if not isinstance(result, dict):
        # An error string or a non-JSON result: publish that it happened, not what.
        return {"type": "tool_result", "tool": tool, "is_error": d.get("is_error")}
    if tool == "search_docs":
        # ids + scores make the feed legibly grounded (D-07); `text` and `heading` are
        # the retrieved prose and stay out of the frame entirely.
        results = result.get("results")
        return {
            "type": "tool_result",
            "tool": tool,
            "results": [
                {"doc": r.get("doc"), "id": r.get("id"), "score": r.get("score")}
                for r in results
                if isinstance(r, dict)
            ] if isinstance(results, list) else [],
        }
    if tool == "send_reply":
        return {
            "type": "tool_result", "tool": tool,
            "reply_id": result.get("reply_id"), "status": result.get("status"),
        }
    if tool == "create_escalation":
        return {
            "type": "tool_result", "tool": tool,
            "escalation_id": result.get("escalation_id"), "status": result.get("status"),
        }
    if tool == "set_category":
        # The category, not the ticket_id echo — the id is already the run's own.
        return {"type": "tool_result", "tool": tool, "category": result.get("category")}
    # lookup_customer lands here with the rest: its result is a whole customer row plus
    # ten ticket subjects, and there is no safe subset of that to show.
    return {"type": "tool_result", "tool": tool, "is_error": d.get("is_error")}


def project(event: AgentEvent) -> dict | None:
    """Redact one run event into a public feed frame, or None to drop it (D-07, SC-3).

    An allowlist, built field by field. Never `{**event.data}`: a spread publishes
    every field anyone adds to an event from then on — a denylist whose entries
    nobody ever writes down. The events flowing through here carry customer emails,
    ticket bodies, reply text and search queries, so the default must be "not
    published" and each exception must be a decision visible in this function.

    Returning None (rather than an empty frame) for an unrecognised type means a new
    yield site in agent.py is silently absent from the feed until someone adds it
    here — annoying, and much better than silently present.
    """
    t, d = event.type, event.data
    if t == "usage":
        return {"type": t, "steps": d.get("steps"), "input_tokens": d.get("input_tokens"),
                "output_tokens": d.get("output_tokens"), "cost_usd": d.get("cost_usd")}
    if t == "resolution":
        return {"type": t, "via": d.get("via"), "cost_usd": d.get("cost_usd"),
                "steps": d.get("steps")}
    if t == "error":
        # reason/status/type are enumerated or numeric; no message text is carried.
        # `error_type`, not `type`: the API error's own type would collide with the
        # frame's, and silently overwrite it if this were ever built by a spread.
        return {"type": t, "reason": d.get("reason"), "status": d.get("status"),
                "error_type": d.get("type")}
    if t == "tool_use":
        return {"type": t, "tool": d.get("tool")}          # the NAME only — never `input`
    if t == "tool_result":
        return _project_tool_result(d)
    if t == "guardrail":
        # That a guard fired and which one. Not the denied payload: `missing_citations`
        # and `supplied_ticket_id` are the model's own output, echoed back from a
        # ticket body we do not control.
        return {"type": t, "guard": d.get("guard"), "tool": d.get("tool"),
                "action": d.get("action")}
    if t == "notice":
        return {"type": t, "kind": d.get("kind"), "tool": d.get("tool"),
                "retrieval_mode": d.get("retrieval_mode"), "cause": d.get("cause"),
                "results": d.get("results")}
    if t == "text":
        # The viewer sees that the model spoke, not what it said: prose restates
        # whatever the model just read, which includes the customer's own details.
        return {"type": t}
    return None

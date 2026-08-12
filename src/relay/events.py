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
import json
import logging
from typing import Any

from .config import settings
from .db import Database
from .models import AgentEvent

logger = logging.getLogger("relay.events")

# Pushed to every subscriber by close() so open /events generators return instead of
# waiting out their idle ceiling while uvicorn holds the shutdown window open. An
# object(), not None or a dict: no published frame can ever be mistaken for it.
_CLOSE_SENTINEL = object()


class BrokerUnavailable(RuntimeError):
    """The broker will not take another subscriber right now.

    Raised by subscribe() at the ceiling, never inside publish: refusing a viewer is
    an admission decision, and the one thing this module may not do is raise into a
    paid run's fan-out.
    """


class RunEventBroker:
    """Fans run frames out to every live `/events` subscriber.

    The sibling of `RunRegistry` — per-app-startup, held on `app.state`, never
    module-level — with the opposite teardown: viewers are not runs, so they are not
    drained, they are idle-closed. Deliberately not folded into the registry, which
    the phase-2 shutdown path waits on: a watching browser is not work to wait for.
    """

    def __init__(self, *, maxsize: int | None = None, max_subscribers: int | None = None) -> None:
        self._subs: set[Any] = set()
        # Defaulted from settings rather than a literal so tests can build a tiny
        # broker without monkeypatching config, and deployment can tune the real one.
        self._maxsize = settings.events_queue_maxsize if maxsize is None else maxsize
        self._max_subscribers = max_subscribers
        self.closed = False

    @property
    def max_subscribers(self) -> int:
        """The admission ceiling, read live rather than frozen at construction.

        Unlike _maxsize — which is baked into each queue as it is created — this is
        checked once per connect, so reading settings here means the deployed value can
        be tuned (and a test can lower it) against the broker lifespan already built.
        """
        return (
            settings.events_max_subscribers
            if self._max_subscribers is None
            else self._max_subscribers
        )

    @property
    def at_capacity(self) -> bool:
        """Whether the next subscribe() would be refused.

        Read by the /events handler so the refusal is a real 503, before the
        StreamingResponse locks its status line at 200 on the first yield.
        """
        return len(self._subs) >= self.max_subscribers

    def subscribe(self) -> asyncio.Queue:
        """Admit one viewer, or refuse past the ceiling.

        The ceiling is enforced here and not only at the handler because this is the
        one place a queue is created: a check that lives anywhere else is a check a
        future caller can route around, and the cost being bounded — an O(subscribers)
        publish on the paid run's loop, plus maxsize frames each — is charged the
        moment this returns. Refusing before the queue exists is also what keeps a
        rejection from leaking one.
        """
        if self.at_capacity:
            logger.warning(
                "events.subscriber_limit_reached",
                extra={"ctx": {"subscribers": len(self._subs), "limit": self.max_subscribers}},
            )
            raise BrokerUnavailable("too many live viewers")
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


class RunRecorder:
    """Writes one `run_events` row per step of one run. Synchronous, on purpose.

    Synchronous because `Database.transaction()` is re-entrant *per thread*: the
    write-tool path below has to open the outer transaction, run the tool's own
    nested one, and insert the event row without ever leaving the worker thread
    `asyncio.to_thread` put it on. An `await` in here would hand control back to the
    loop with the transaction open and the lock held.

    One instance per run, holding the run's uid, its ticket and its own `seq`
    counter — the counter is what makes phase 6's drill-down renderable in order
    without trusting `created_at` to break ties between two rows in the same
    millisecond.
    """

    def __init__(self, conn: Database, *, run_uid: str, ticket_id: int) -> None:
        self.conn = conn
        self.run_uid = run_uid
        self.ticket_id = ticket_id
        self._seq = 0

    def _insert_event(self, type: str, data: dict) -> None:
        """One INSERT. Assumes a transaction is already open — see both callers.

        `data` is stored RAW. The private table is the full-fidelity record phase 6's
        drill-down reads; `project()` is the transform on the way out to the public
        feed. Redacting here instead would leave nothing to drill into.
        """
        self._seq += 1
        self.conn.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            # default=str so a stray datetime or Decimal in an event cannot take down
            # the run it was only meant to describe.
            (self.run_uid, self.ticket_id, self._seq, type, json.dumps(data, default=str)),
        )

    def record(self, event: AgentEvent) -> None:
        """Persist one event that has no sibling write to share a transaction with.

        Read tools, model text, usage and resolution: nothing else is being written,
        so this is its own single-statement unit of work. Through transaction()
        rather than execute() + commit(), because commit() is connection-scoped and
        would commit whatever else happened to be open (telemetry.py L69-72).
        """
        with self.conn.transaction():
            self._insert_event(event.type, event.data)

    def execute_and_record(
        self, execute_bound, spec, name: str, raw_input: dict, policy, *, event_type: str
    ) -> tuple[str, bool]:
        """Run one WRITE-tier tool and record it in the same transaction (D-04).

        This is the seam the whole atomicity guarantee rests on. The tool's executor
        opens its own `transaction()`, which nests as a SAVEPOINT inside this one; the
        event INSERT joins it; the commit happens once, here, at the `with` exit. So a
        reply row and the record of that reply can only exist together — a failed
        event insert takes the reply back with it rather than leaving a customer
        emailed with nothing to show it happened.

        Do NOT move the insert below the `with` "to keep the tool's transaction
        short". That is a second top-level transaction and it silently converts this
        into best-effort logging.
        """
        with self.conn.transaction():
            result, is_error = execute_bound(spec, name, raw_input, policy)
            self._insert_event(
                event_type,
                # json.loads to match agent.py's own tool_result event, which carries
                # the parsed payload rather than the wire string.
                {"tool": name, "result": json.loads(result), "is_error": is_error},
            )
        return result, is_error

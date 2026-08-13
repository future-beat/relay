"""The three contracts the live feed is built from: fan-out, redaction, persistence.

They live together because they are the seam between one agent step and everything
that watches it, and each one fails in a way the others cannot catch:

- `RunEventBroker` fans a frame out to every watching browser. It is bounded and
  drop-oldest so a stalled viewer's queue can never backpressure the run that is
  paying Anthropic for the step (D-10).
- `project()` is the only path raw EVENT data may take to become a public frame. It
  is an allowlist, built field by field: a denylist leaks the first field someone
  adds later, and the fields flowing past here include customer emails, ticket
  bodies and reply text (D-07). `attribute_to_run()` stamps the run's identity onto
  a frame project() has already built — it is not a second path to one.
- `snapshot_frame()` is the second public serialiser: the connect frame (D-14), whose
  input is registry state rather than a run event. It lives HERE, beside project(),
  because "the redaction boundary" has to be one file a reviewer can open (WR-04) — it
  used to sit in main.py while this docstring claimed project() was the only path,
  which is the kind of gap an audit finds by luck.
- `project_run_detail()` is the third and last: phase 6's drill-down redactor (D-01),
  whose input is stored `run_events` ROWS rather than a live event. It is here for the
  same WR-04 reason, and it reuses `_project_tool_result` so the back catalogue can
  never disclose more of a tool result than the live feed already did. Counting it
  makes three — this docstring said two until phase 6 added it, and a docstring that
  undercounts the public serialisers is the exact failure WR-04 was raised for.
  `test_events_output_comes_only_from_two_serialisers` still holds and still says two:
  it pins the /events GENERATOR, which uses project() and snapshot_frame() and must
  never grow a third path. The drill-down is a different route, so it is outside that
  test's subject and inside its own (tests/test_dashboard.py).
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
import time
from typing import Any

from .config import settings
from .db import Database
from .models import AgentEvent
from .retrieval import normalise_citation
from .runs import ActiveRun

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
        # Frames this broker has dropped, for the life of the process. A counter and not
        # a per-subscriber tally: a queue is dropped along with its viewer, and the
        # question an operator asks is "is the demo lossy right now", not "which tab".
        self.dropped = 0

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

        D-10 says drop rather than block; it does not say drop INVISIBLY (WR-09). Every
        drop is counted and the first — plus every hundredth after it — is logged, so a
        viewer watching a feed with a hole in it is something an operator can see rather
        than something only the viewer half-notices. Rate-limited because a queue that
        is full stays full: a log line per dropped frame would turn one slow browser
        into a second, louder cost on the same paid run.
        """
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                q.get_nowait()  # drop the oldest, keep the live end of the feed
                q.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                # A concurrent reader drained it between the two calls, or filled it
                # again. The viewer is fine and the run must not care — but the frame is
                # gone either way, so it is counted with the rest rather than swallowed.
                self._record_drop(reason="requeue_failed")
            else:
                self._record_drop(reason="queue_full")

    def _record_drop(self, *, reason: str) -> None:
        """Count one dropped frame and log at a rate limit. Must never raise (D-10)."""
        self.dropped += 1
        if self.dropped == 1 or self.dropped % 100 == 0:
            logger.warning("events.frame_dropped", extra={"ctx": {
                "reason": reason,
                "dropped": self.dropped,
                "subscribers": len(self._subs),
                "queue_maxsize": self._maxsize,
            }})

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

    The failure branch comes FIRST, before any dispatch on the tool name (WR-01). A
    denied send_reply returns `{"error": ..., "denied_by": "ticket_binding"}` — which
    IS a dict — so it used to take the send_reply branch and publish
    `{"reply_id": null, "status": null}`: a success-shaped frame with no error signal
    at all, which told a viewer a guardrail firing and a rendering blank were the same
    event. `denied_by` is the guard's own enumerated name (policy / ticket_binding /
    citation), the same disclosure the `guardrail` frame already carries — never the
    denied payload, which is the model's output echoed from a ticket body.
    """
    tool = d.get("tool")
    result = d.get("result")
    # Coerced, not forwarded: `is_error` is a boolean everywhere it is produced, and a
    # frame whose error flag is None reads as "unknown" to every consumer of the feed.
    is_error = bool(d.get("is_error"))
    if is_error or not isinstance(result, dict):
        # An error, an error string, or a non-JSON result: publish THAT it failed and
        # which guard refused it, never what was refused.
        return {
            "type": "tool_result", "tool": tool, "is_error": is_error,
            "denied_by": result.get("denied_by") if isinstance(result, dict) else None,
        }
    if tool == "search_docs":
        # ids + scores make the feed legibly grounded (D-07); `text` and `heading` are
        # the retrieved prose and stay out of the frame entirely.
        results = result.get("results")
        return {
            "type": "tool_result",
            "tool": tool,
            "is_error": False,
            "results": [
                {"doc": r.get("doc"), "id": r.get("id"), "score": r.get("score")}
                for r in results
                if isinstance(r, dict)
            ] if isinstance(results, list) else [],
        }
    if tool == "send_reply":
        return {
            "type": "tool_result", "tool": tool, "is_error": False,
            "reply_id": result.get("reply_id"), "status": result.get("status"),
        }
    if tool == "create_escalation":
        return {
            "type": "tool_result", "tool": tool, "is_error": False,
            "escalation_id": result.get("escalation_id"), "status": result.get("status"),
        }
    if tool == "set_category":
        # The category, not the ticket_id echo — the id is already the run's own.
        return {
            "type": "tool_result", "tool": tool, "is_error": False,
            "category": result.get("category"),
        }
    # lookup_customer lands here with the rest: its result is a whole customer row plus
    # ten ticket subjects, and there is no safe subset of that to show.
    return {"type": "tool_result", "tool": tool, "is_error": False}


# The demo branch's OWN allowlist (CR-01). See project_run_detail's docstring for the
# rule; the short form is that these are the tools whose raw input and raw result are
# either the run's own words about the visitor's ticket or Relay's own published docs.
# `lookup_customer` is on neither side of it: its argument is a PERSON'S IDENTIFIER the
# visitor typed rather than prose they wrote, and its result is a stored record about
# that person plus ten of their ticket subjects — the payload _project_tool_result's
# fallthrough exists to destroy. A tool added later is absent from this set and is
# therefore redacted on both branches until someone adds it here on purpose.
_DEMO_RAW_TOOLS = frozenset({"search_docs", "send_reply", "create_escalation", "set_category"})

# Not "None": tests/test_auth.py asserts the served document never contains that string,
# and a placeholder that reads as a missing value is worse than one that reads as a
# decision.
_WITHHELD = "[withheld]"


def mask_withheld(value: Any, withheld: tuple[str, ...]) -> Any:
    """Replace every occurrence of a withheld literal, at any depth of a JSON value.

    The demo branch publishes model prose (`text`, `send_reply.body`,
    `create_escalation.reason`), and prose restates whatever the model just read — so a
    per-field allowlist alone cannot keep a value out of the response, only a field.
    This is the value-level half: the route hands it the ticket's own `customer_email`,
    which `run_detail`'s docstring has always claimed is withheld "even on the demo
    branch" and which was until now withheld only as a COLUMN.

    Keys are masked as well as values: on the demo branch the raw `input` dict's keys
    are model-chosen strings, so a secret can arrive as one.
    """
    if not withheld:
        return value
    if isinstance(value, str):
        for secret in withheld:
            value = value.replace(secret, _WITHHELD)
        return value
    if isinstance(value, list):
        return [mask_withheld(v, withheld) for v in value]
    if isinstance(value, dict):
        return {
            mask_withheld(k, withheld): mask_withheld(v, withheld) for k, v in value.items()
        }
    return value


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
        # `result_count`, not `results`, and coerced rather than forwarded (WR-02). Every
        # other field this function publishes is a scalar or an enum; this was the one
        # forward of unconstrained shape. agent.py sets it to a len() today, but nothing
        # here required that: the natural future edit ("show WHICH results we fell back
        # to") makes it the list, and this branch would then publish each hit's `text`
        # and `heading` — the retrieved prose _project_tool_result strips four lines
        # above — to an unauthenticated endpoint, with no test failing. The name now
        # states the shape and the coercion enforces it: anything but an int is dropped.
        count = d.get("results")
        return {"type": t, "kind": d.get("kind"), "tool": d.get("tool"),
                "retrieval_mode": d.get("retrieval_mode"), "cause": d.get("cause"),
                "result_count": count if isinstance(count, int) else None}
    if t == "text":
        # The viewer sees that the model spoke, not what it said: prose restates
        # whatever the model just read, which includes the customer's own details.
        return {"type": t}
    return None


def _as_int(value: Any) -> int | None:
    """An int, or nothing. Coerced rather than forwarded, the way `result_count` is.

    `bool` is excluded deliberately even though it is an int subclass: `True` is not a
    ticket id, and a frame reading `supplied_ticket_id: true` is worse than one reading
    null. Everything else — a model-authored string, a dict, a list — becomes None
    rather than being published in whatever shape it arrived in.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def project_run_detail(
    rows: list,
    *,
    full_fidelity: bool,
    known_tools: dict[str, frozenset[str]],
    withheld: tuple[str, ...] = (),
) -> list[dict]:
    """Redact one run's persisted events into a public drill-down (D-01, DASH-03).

    Takes the WHOLE run rather than one event, because two of the things DASH-03 asks
    for are run-level facts and not per-event ones: cited-vs-not is a comparison
    between the accepted send_reply's citations and every search_docs result the run
    saw, and a tool's duration is the gap between its tool_use row and its tool_result
    row. A `map(project_one, rows)` shape cannot compute either.

    `full_fidelity` is D-02's exception, and it is decided by the CALLER from
    `tickets.origin` — never from anything the client sent. It is keyword-only with NO
    default so a caller has to state which side it is on. It selects a SECOND
    allowlist, not a raw spread: even the demo path names `input`, `result`, `text` and
    `missing_citations` explicitly, so no path in this codebase publishes a field
    nobody wrote down, and default-deny survives the exception. `customer_email` is on
    neither side (Q3) — it is the one field a visitor could use to publish a third
    party's identifier — and neither is the ticket text, which is the route's to add.

    THE RULE THE DEMO BRANCH FOLLOWS (CR-01), stated here so the next person widening it
    inherits it: full fidelity covers what the VISITOR'S OWN SUBMISSION brought in and
    what this run said about it — their ticket text, the model's prose, the model's tool
    ARGUMENTS, and the results of tools that return Relay's own published material or an
    id/status for the visitor's own ticket. It does NOT cover a tool's output about
    ANYONE ELSE. "The visitor authored this content" is a claim about what they SENT; it
    was never a claim about what the service went and looked up in response, and
    `lookup_customer` returns a whole stored customer row plus ten of that person's
    ticket subjects — filed by whoever else has been using this service, on a route that
    is keyless and 30 days deep. So the raw `input` and raw `result` are published PER
    TOOL, from `_DEMO_RAW_TOOLS`, and a tool absent from that set gets the same redacted
    projection the public branch gets. Default-deny survives the exception on this axis
    too: a tool added later discloses nothing raw until someone names it there.

    `withheld` is the value-level half of the same rule, and it exists because prose
    cannot be allowlisted by field: `text`, `send_reply.body` and
    `create_escalation.reason` are the model restating what it just read. The route
    passes the ticket's own `customer_email`, so that address is absent from a demo
    drill-down BY VALUE and not merely as a column. Its default is "nothing extra to
    withhold", which is the posture every non-demo caller wants — the field allowlist
    above is what carries the property, and this narrows what survives it.

    `known_tools` maps each REGISTERED tool name to the frozenset of its declared
    `input_schema.properties` keys; the caller builds it from `app.state.registry`.
    Both the tool name and its argument keys are strings the MODEL chose (INFO-1), and
    both reach a browser: the name is clamped to this map and an unregistered one
    renders the literal "unknown", while argument keys are intersected with the
    declared schema and whatever that excludes becomes a count — a number cannot carry
    a payload.

    The tool_result branch calls `_project_tool_result` itself rather than restating
    it. That is the point: the drill-down is a bigger disclosure surface than the live
    feed and, unlike the feed, it is a BACK CATALOGUE — so "the drill-down can never
    show more of a tool result than /events already does" has to be a structural
    property, enforced by shared code, and not a promise a reviewer keeps by comparing
    two lists. It also means the error branch's dropping of the message text is
    inherited, not re-decided: there is no `error` or `message` key on the public side.

    Fail-closed on both axes. An unrecognised `type` is dropped, exactly as project()
    drops one, so a new yield site in agent.py is absent from the drill-down until
    someone adds it here on purpose. A payload that will not parse — or parses to
    something that is not a dict — is a DROPPED STEP and never a 500: `load_index`'s
    degrade-and-log posture, because one bad row must not make a whole run's history
    unreadable. An empty `rows` (a run whose events the 30-day retention swept, which
    deliberately spares the `runs` row) projects to `[]`; rendering that state as
    "swept" is the route's job, and not crashing is this function's.

    `created_at` is never published: it is `datetime('now')` at second resolution, so
    as a per-step timing it is actively misleading. `seq` carries the causal order and
    `elapsed_ms` the timing, both stamped by RunRecorder for exactly this.
    """
    # Pass 1 — parse, dropping anything unreadable. The log names the row, never a
    # value from it: this function's whole job is that payload values do not escape.
    parsed: list[tuple[Any, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            logger.warning("run_detail.malformed_payload", extra={"ctx": {
                "seq": row["seq"], "type": row["type"],
            }})
            continue
        parsed.append((row, payload))

    # Pass 2 — the run-level facts. Each tool_result pairs with the NEAREST PRECEDING
    # UNPAIRED tool_use of the same tool name, so two interleaved calls to different
    # tools cannot swap durations, and a retried send_reply pairs with its own attempt.
    pending: dict[Any, list[int]] = {}
    paired_use: dict[int, int] = {}
    for i, (row, payload) in enumerate(parsed):
        if row["type"] == "tool_use":
            pending.setdefault(payload.get("tool"), []).append(i)
        elif row["type"] == "tool_result":
            stack = pending.get(payload.get("tool"))
            if stack:
                paired_use[i] = stack.pop()

    # The cited set comes from the ACCEPTED send_reply's tool_use — the citations are
    # nowhere else, `replies` does not persist them (tools.py). A DENIED attempt's
    # citations are not "cited": the guardrail row already tells that story, and
    # counting them would make the drill-down claim grounding the guard refused. No
    # accepted send_reply at all (an escalation, an error, a dry run) leaves this empty,
    # which correctly renders every chunk as retrieved-but-not-cited.
    cited: set[str] = set()
    for i, (row, payload) in enumerate(parsed):
        if (
            row["type"] != "tool_result"
            or payload.get("tool") != "send_reply"
            or payload.get("is_error")
            or i not in paired_use
        ):
            continue
        raw_input = parsed[paired_use[i]][1].get("input")
        citations = raw_input.get("citations") if isinstance(raw_input, dict) else None
        if isinstance(citations, list):
            # normalise_citation, not a local strip().lower(): this is the VIEW of the
            # citation guard, and a view that contradicts its control is worse than none.
            cited.update(normalise_citation(c) for c in citations if isinstance(c, str))

    duration_for: dict[int, int | None] = {}
    for i, use in paired_use.items():
        end, start = parsed[i][0]["elapsed_ms"], parsed[use][0]["elapsed_ms"]
        duration_for[i] = (
            end - start if isinstance(end, int) and isinstance(start, int) else None
        )

    # Pass 3 — one step per surviving row, field by field.
    steps: list[dict] = []
    for i, (row, payload) in enumerate(parsed):
        t = row["type"]
        step: dict[str, Any] = {"seq": row["seq"], "type": t, "elapsed_ms": row["elapsed_ms"]}
        if t == "usage":
            step["steps"] = payload.get("steps")
            step["input_tokens"] = payload.get("input_tokens")
            step["output_tokens"] = payload.get("output_tokens")
            step["cost_usd"] = payload.get("cost_usd")
        elif t == "resolution":
            step["via"] = payload.get("via")
            step["cost_usd"] = payload.get("cost_usd")
            step["steps"] = payload.get("steps")
        elif t == "error":
            # `error_type`, not `type`: the API error's own type would collide with the
            # step's, for the same reason project() renames it.
            step["reason"] = payload.get("reason")
            step["status"] = payload.get("status")
            step["error_type"] = payload.get("type")
        elif t == "text":
            # A length tells a visitor "the model reasoned for 400 characters" and
            # discloses nothing quotable; the prose restates the customer's own ticket.
            # len() only of a str — never of a dict that happened to land here.
            raw_text = payload.get("text")
            step["char_count"] = len(raw_text) if isinstance(raw_text, str) else None
            if full_fidelity:
                # Counted before masking and published after: the count is a fact about
                # what the model wrote, and a shorter one would misdescribe the run.
                step["text"] = mask_withheld(raw_text, withheld)
        elif t == "tool_use":
            raw_tool = payload.get("tool")
            raw_input = payload.get("input")
            raw_input = raw_input if isinstance(raw_input, dict) else {}
            declared = known_tools.get(raw_tool, frozenset())
            arg_keys = sorted(k for k in raw_input if k in declared)
            step["tool"] = raw_tool if raw_tool in known_tools else "unknown"
            step["arg_keys"] = arg_keys
            step["unknown_arg_count"] = len(raw_input) - len(arg_keys)
            if full_fidelity and raw_tool in _DEMO_RAW_TOOLS:
                # Per tool, not per branch: lookup_customer's one argument is a person's
                # address the visitor typed into a form — the identifier this route has
                # always claimed to withhold — and not words they wrote.
                step["input"] = mask_withheld(raw_input, withheld)
        elif t == "tool_result":
            detail = _project_tool_result(payload)
            # Its own "type" is the same string as the row's; dropped so the envelope
            # above stays the single source of it.
            detail.pop("type", None)
            # An update of ALREADY-REDACTED output, not a spread of the payload — the
            # reuse this branch exists for.
            step.update(detail)
            step["duration_ms"] = duration_for.get(i)
            projected_results = detail.get("results")
            if isinstance(projected_results, list):
                # Filtered exactly as _project_tool_result filters, so index i of the
                # projected list is index i of the raw one and no result can be
                # labelled with another's grounding.
                result = payload.get("result")
                raw_results = result.get("results") if isinstance(result, dict) else None
                raw_results = [
                    r for r in raw_results if isinstance(r, dict)
                ] if isinstance(raw_results, list) else []
                for projected, raw in zip(projected_results, raw_results, strict=False):
                    anchors = raw.get("anchors")
                    # Every id this hit licenses — its doc, its located id and each of
                    # its anchors — exactly the set agent.py adds to the run's
                    # accept-set. Narrowing it to `id` would mark a correct anchor
                    # citation as ungrounded.
                    licensed = [raw.get("doc"), raw.get("id")]
                    licensed.extend(anchors if isinstance(anchors, list) else ())
                    projected["cited"] = any(
                        isinstance(lic, str) and normalise_citation(lic) in cited
                        for lic in licensed
                    )
            if full_fidelity and payload.get("tool") in _DEMO_RAW_TOOLS:
                # The line CR-01 was: this assignment lands AFTER step.update(detail), so
                # it bypasses _project_tool_result entirely and the "the drill-down can
                # never show more of a tool result than /events does" property held on
                # the public branch only. Allowlisted per tool, the demo branch now
                # widens the same five tools the redacted one names and adds no sixth —
                # and lookup_customer's stored customer row is not one of them.
                step["result"] = mask_withheld(payload.get("result"), withheld)
        elif t == "guardrail":
            # The prompt-injection story IS "a ticket body named ticket 999 and the
            # guard denied it", so the two ids are the payoff — and they are ints by
            # the time the event is built (agent.py reads them off validated input),
            # which is why coercing rather than forwarding costs nothing here.
            missing = payload.get("missing_citations")
            step["guard"] = payload.get("guard")
            step["tool"] = payload.get("tool")
            step["action"] = payload.get("action")
            step["expected_ticket_id"] = _as_int(payload.get("expected_ticket_id"))
            step["supplied_ticket_id"] = _as_int(payload.get("supplied_ticket_id"))
            step["missing_count"] = len(missing) if isinstance(missing, list) else 0
            if full_fidelity:
                # Model-authored text, so it is named here and nowhere near the public
                # branch. `retrieved_ids` is on NEITHER: it is not in the allowlist.
                step["missing_citations"] = mask_withheld(missing, withheld)
        elif t == "notice":
            # `result_count`, not `results`, and coerced — project()'s WR-02 coercion
            # copied rather than merely its field name, because the same future edit
            # ("show WHICH results we fell back to") turns this into the hit list, and
            # a forward would then publish each hit's retrieved prose.
            count = payload.get("results")
            step["kind"] = payload.get("kind")
            step["tool"] = payload.get("tool")
            step["retrieval_mode"] = payload.get("retrieval_mode")
            step["cause"] = payload.get("cause")
            step["result_count"] = count if isinstance(count, int) else None
        else:
            continue  # unrecognised type — dropped, on BOTH branches
        steps.append(step)
    return steps


def attribute_to_run(frame: dict, *, run_uid: str, ticket_id: int) -> dict:
    """Stamp an already-projected frame with the run it belongs to (CR-03).

    The service admits concurrent runs by design — RunRegistry holds a dict of them and
    the connect snapshot renders a list — so an unattributed feed interleaves two runs'
    tool_use, usage and resolution frames with no way to tell which cost or which
    outcome belonged to which ticket. That makes phase 6's per-run cards unbuildable
    and the frames unjoinable to the `run_events` rows this phase exists to write.

    Separate from project() rather than a parameter of it: project() is a pure
    per-event redactor and knows nothing about runs, while identity is known only at
    the publish site. Separate from a second serialisation path too — it takes a frame
    project() already built and adds two fields, so nothing reaches a subscriber
    without having passed the allowlist first.

    `ticket_id` is not a new disclosure: /events' own connect snapshot and /metrics'
    last_runs both already carry it.

    `run_uid` is published on BOTH surfaces now: here, and on /metrics' last_runs.

    Phase 5 left it here only. `telemetry._PUBLIC_RUN_COLUMNS` had dropped it from
    /metrics on the reasoning that the uid is the key into `run_events` — a table full
    of unredacted customer data — and this docstring set the condition phase 6 would
    have to satisfy before it could come back: the uid must stay a CORRELATION token
    and never become a bearer credential, so a drill-down keyed on it must be
    authenticated or itself redacted, and knowing a uid must grant nothing.

    Phase 6 satisfied that condition rather than widening it, so the withholding was
    dropped (it was never protection — the uid is already broadcast to every anonymous
    listener for every run they watch; withholding it on /metrics only made the back
    catalogue unnavigable for honest callers):

    - `project_run_detail` above is the drill-down the uid opens, and it is redacted
      server-side by the same field-by-field allowlist this file exists for (D-01,
      D-03). What a uid buys is a step trace with the emails, ticket bodies, reply text
      and retrieved prose already removed.
    - The one full-fidelity path is gated on `tickets.origin`, read from the database
      by the route. It is not a query param, a header or a cookie, and
      `full_fidelity` is keyword-only with no default so no caller drifts into it.

    So the uid is a correlation token on both surfaces: it names a run, and naming a
    run grants exactly what any anonymous visitor could already watch happen live.
    `test_concurrent_runs_are_attributable_in_the_feed` pins the correlation half; the
    drill-down's own leak tests pin the half that makes publishing it safe.

    If a future change makes a uid grant anything a caller did not already have — an
    unredacted field, a write, a rate-limit exemption — that is the moment it becomes a
    bearer credential, and this is the line to delete, not the reasoning to widen.

    The identity is written LAST so no event field can ever overwrite it — a frame that
    lies about which run it came from is worse than no frame.
    """
    return {**frame, "run_uid": run_uid, "ticket_id": ticket_id}


def snapshot_frame(runs: list[ActiveRun]) -> str:
    """The one frame a new subscriber gets before any live event (D-14).

    Without it a tab opened mid-run shows an empty feed until the next step happens,
    which on a quiet demo can be minutes — the visitor concludes nothing is running.

    Built field by field from ActiveRun, for the same reason project() is: this is a
    public boundary, and `asdict()` here would publish whatever field someone adds to
    ActiveRun later. There are exactly two fields today and neither is a secret —
    ticket_id is already public on /metrics (last_runs), and started_at is deliberately
    NOT published raw: it is a monotonic clock reading, meaningless off this process, so
    it is rendered as the elapsed time a viewer can actually use.

    Not routed through project(): that function's input is an AgentEvent, and this is
    registry state, not a run event. It is still an allowlist, written out below — and
    it lives in this module rather than in main.py so that both public serialisers are
    in the file whose docstring claims to hold the redaction boundary (WR-04). It takes
    the snapshot rather than reading `app.state`, so nothing here knows about the app.
    """
    now = time.monotonic()
    rendered = [
        {"ticket_id": run.ticket_id, "running_for_ms": int((now - run.started_at) * 1000)}
        for run in runs
    ]
    return f"event: snapshot\ndata: {json.dumps({'type': 'snapshot', 'runs': rendered})}\n\n"


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
    millisecond (it is `datetime('now')`, second resolution, so there is no tie to
    break with). The counter therefore has to advance in the order the run yielded
    its events, on EVERY path including a guardrail denial: see execute_and_record.
    """

    def __init__(self, conn: Database, *, run_uid: str, ticket_id: int) -> None:
        self.conn = conn
        self.run_uid = run_uid
        self.ticket_id = ticket_id
        self._seq = 0
        # Monotonic, per run: the drill-down needs sub-second step timings and
        # created_at is datetime('now') — whole seconds, which is also why seq exists.
        # Monotonic rather than wall-clock so an NTP step mid-run cannot make one step
        # appear to finish before it started.
        self._t0 = time.monotonic()

    def _insert_event(self, type: str, data: dict) -> None:
        """One INSERT. Assumes a transaction is already open — see both callers.

        `data` is stored RAW. The private table is the full-fidelity record phase 6's
        drill-down reads; `project()` is the transform on the way out to the public
        feed. Redacting here instead would leave nothing to drill into.

        `elapsed_ms` is stamped HERE and nowhere else, so both callers are covered by
        construction rather than by remembering. For a write tool the row is inserted
        after `execute_bound` returns, inside the tool's own transaction, so
        `elapsed_ms(tool_result) - elapsed_ms(tool_use)` is that tool's wall time —
        which is exactly what phase 6's drill-down projector subtracts.
        """
        self._seq += 1
        self.conn.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            # default=str so a stray datetime or Decimal in an event cannot take down
            # the run it was only meant to describe.
            (
                self.run_uid,
                self.ticket_id,
                self._seq,
                type,
                json.dumps(data, default=str),
                int((time.monotonic() - self._t0) * 1000),
            ),
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
    ) -> tuple[str, bool, bool]:
        """Run one WRITE-tier tool and record it in the same transaction (D-04).

        Returns (result_json, is_error, recorded). `recorded` is False on the one path
        that deliberately writes no row — see below — and the caller must then persist
        the tool_result itself, at its own yield site.

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
            payload = json.loads(result)
            if is_error and payload.get("denied_by"):
                # A guardrail denial: every `denied_by` in _execute_guarded (policy,
                # ticket_binding, citation) returns BEFORE spec.execute, so nothing was
                # written and there is nothing for this row to be atomic with. Recording
                # it here anyway is what inverted the record: the row lands inside this
                # offload, taking a lower seq than the `guardrail` event that explains
                # it, which the caller only reaches afterwards — an audit trail showing
                # a denied send_reply's result BEFORE the denial that produced it, with
                # created_at at second resolution and no third signal to recover the
                # true order from. So the row is left to the caller, which writes it
                # after the guardrail event, in causal order. D-04 is untouched: it
                # applies to calls that wrote something, and this one did not.
                return result, is_error, False
            self._insert_event(
                event_type,
                # json.loads to match agent.py's own tool_result event, which carries
                # the parsed payload rather than the wire string.
                {"tool": name, "result": payload, "is_error": is_error},
            )
        return result, is_error, True

"""The agent loop, written by hand on the Claude API.

No framework: the loop is a plain request -> tool-execute -> append cycle so the
control flow, guardrails, and event stream are fully visible and testable.

Guardrails enforced here (phase 2):
- every tool input is validated with Pydantic before execution
- a ToolPolicy gates write-tier tools (dry-run mode)
- a RunBudget tracks token spend and aborts the run at a hard cost ceiling
- API failures end the run with a structured error event, never a stack trace
  (transient 429/5xx are already retried inside the SDK)

Guardrails enforced here (phase 1, remaster):
- the run's ticket_id is bound server-side: a ticket body is untrusted input and
  the model's tool arguments are therefore untrusted output, so a tool call
  naming a different ticket is denied before execution rather than rebound

Guardrails enforced here (phase 3, remaster):
- a reply may only cite source ids search_docs returned during this run: anything
  else is denied with a retry instruction rather than silently stripped, so the
  model's grounding claim is checked against what it was actually handed

Observability (phase 4): every run is an OpenTelemetry `agent.run` span with
one child span per model request and per tool execution, and each step emits
a structured JSON log line.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from opentelemetry import trace

from .config import settings
from .events import RunRecorder
from .guardrails import RunBudget, ToolInputError, ToolPolicy, validate_tool_input
from .models import AgentEvent
from .prompts import SYSTEM_PROMPT, ticket_prompt
from .tools import ToolSpec

TERMINAL_TOOLS = {"send_reply", "create_escalation"}

logger = logging.getLogger("relay.agent")
tracer = trace.get_tracer("relay")

# "There is no current run", as a value that had to be chosen rather than one that
# arrives by accident. None used to mean this, which made None also the value a
# caller lands on by forgetting the keyword — so the same token meant both "the MCP
# server has no ticket to bind to" and "SEC-04 is off because someone typo'd".
UNBOUND = object()


def bind_to_ticket(ticket_id: int, retrieved_ids: set[str] | None = None):
    """Return a tool executor locked to one ticket, for the length of one run.

    The binding is a constructor argument here rather than a per-call keyword because
    a keyword with a default is a control that can be forgotten: omit it and SEC-04
    stops applying with no error, no log and no failing test. A caller of this cannot
    forget it — there is no argument at the call site to leave out, and no executor at
    all without a ticket id. The MCP path has no current run and so calls
    _execute_guarded directly; that is the one legitimate unbound caller.

    `retrieved_ids` is injected the same way and for the same reason: it is the set of
    citation ids search_docs returned during THIS run, held by reference so the loop can
    grow it as the run proceeds. Per-run and never on the shared registry, or one run
    could cite another's docs. None means "no run to check against" (the MCP path) and
    skips the check entirely; an empty set means "this run has retrieved nothing", so
    any citation is denied.
    """
    if not isinstance(ticket_id, int) or isinstance(ticket_id, bool):
        raise TypeError(
            f"a run must be bound to an int ticket id, got {type(ticket_id).__name__}"
        )

    def execute(
        spec: ToolSpec | None, name: str, raw_input: dict[str, Any], policy: ToolPolicy
    ) -> tuple[str, bool]:
        return _execute_guarded(
            spec,
            name,
            raw_input,
            policy,
            bound_ticket_id=ticket_id,
            retrieved_ids=retrieved_ids,
        )

    return execute


def _execute_guarded(
    spec: ToolSpec | None,
    name: str,
    raw_input: dict[str, Any],
    policy: ToolPolicy,
    *,
    bound_ticket_id: int | object = UNBOUND,
    retrieved_ids: set[str] | None = None,
) -> tuple[str, bool]:
    """Run one tool call through the guardrail chain. Returns (result_json, is_error).

    Bound callers go through bind_to_ticket(); the defaults are for the MCP server,
    which has no current run to bind to and nothing retrieved to cite.
    """
    if bound_ticket_id is None:
        # Fails closed rather than degrading to "unbound": None reaching here means a
        # caller had a binding in hand and lost it, which is the fail-open case SEC-04
        # exists to prevent. UNBOUND is how a caller says it never had one.
        raise ValueError(
            "bound_ticket_id=None disables the ticket binding silently;"
            " pass UNBOUND if this call has no current run"
        )
    if spec is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    denial = policy.denial_reason(spec.tier)
    if denial:
        return json.dumps({"error": denial, "denied_by": "policy"}), True
    try:
        validated = validate_tool_input(spec.input_model, raw_input)
    except ToolInputError as exc:
        return json.dumps({"error": str(exc)}), True
    # Server truth beats model output: validation first so the comparison runs on a
    # coerced int, and before execution so this is a choke point, not an audit log.
    # bound_ticket_id is UNBOUND on the MCP path, where there is no "current run".
    supplied_ticket_id = validated.get("ticket_id")
    if (
        bound_ticket_id is not UNBOUND
        and supplied_ticket_id is not None
        and supplied_ticket_id != bound_ticket_id
    ):
        # Phrased as a retry instruction, not a refusal: a refusal makes the model
        # give up, the run ends without a terminal action, and the eval gate regresses.
        return json.dumps({
            "error": (
                f"ticket_id {supplied_ticket_id} is not this run's ticket."
                f" This run may only act on ticket {bound_ticket_id}."
                f" Retry with ticket_id={bound_ticket_id}."
            ),
            "denied_by": "ticket_binding",
            "expected_ticket_id": bound_ticket_id,
            "supplied_ticket_id": supplied_ticket_id,
        }), True
    # The model's grounding claim, checked against what this run was actually handed
    # (RAG-04). Subset, not "at least one": no citations is the pre-phase-3 call and
    # passes (D-12), a fabricated source does not. retrieved_ids is None only where
    # there is no run — the MCP path — the same way bound_ticket_id is UNBOUND there.
    if name == "send_reply" and retrieved_ids is not None:
        # Compared case- and whitespace-insensitively: every id in the set is already
        # lowercase (a filename plus a slug), so drift in how the model retypes one it
        # was shown is a formatting difference, not a fabricated source.
        allowed = {i.strip().lower() for i in retrieved_ids}
        missing = [
            c for c in (validated.get("citations") or []) if c.strip().lower() not in allowed
        ]
        if missing:
            # A retry instruction that names the valid ids, not a refusal. A refusal
            # leaves resolved_via None, the run ends "ended_without_action", and the
            # eval gate regresses — so the denial has to be recoverable in-run.
            available = ", ".join(sorted(retrieved_ids)) or "nothing yet"
            return json.dumps({
                "error": (
                    f"citation(s) {missing} were not retrieved in this run and cannot"
                    f" be cited. Retrieved so far: {available}."
                    " Retry send_reply citing only those ids, or search_docs first."
                ),
                "denied_by": "citation",
                "missing_citations": missing,
                "retrieved_ids": sorted(retrieved_ids),
            }), True
    try:
        return spec.execute(**validated), False
    except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
        return json.dumps({"error": str(exc)}), True


async def run_ticket(
    client: AsyncAnthropic,
    registry: dict[str, ToolSpec],
    ticket: dict[str, Any],
    policy: ToolPolicy | None = None,
    budget: RunBudget | None = None,
    recorder: RunRecorder | None = None,
    *,
    seed_citation_denial: bool = False,
) -> AsyncIterator[AgentEvent]:
    """Run the agent on one ticket, yielding an AgentEvent per step.

    `seed_citation_denial` is an eval-only probe (D-08, closing 03-REVIEW WR-10): it
    drops one genuinely-retrieved id from this run's accept-set so the model's own,
    natural citation is denied exactly once, and whether it recovers to a terminal
    action becomes observable. Keyword-only and off by default on purpose — the same
    aversion to forgettable per-call controls as `bind_to_ticket` above, in reverse:
    arming it narrows a live run's accept-set, so nothing but the eval harness may.

    `recorder` is an optional collaborator (phase 5, D-04), defaulted the same way as
    `policy` and `budget`: given one, every event this generator yields is first
    written to `run_events`; given None, this loop is byte-for-byte what it was, which
    is what keeps `evals.py` and `mcp_server.py` — both of which call this with neither
    — unchanged. It is injected HERE rather than at the SSE layer because that is the
    only place D-04's atomicity is reachable: a write-tier tool's own transaction is
    opened and committed inside the `to_thread` worker below, so the only way its event
    row can share that transaction is to write it on that same thread, before the
    result ever comes back to the loop. By the time an event surfaces to `event_stream`
    it is already committed — which is also what makes publishing from there
    post-commit for free (D-06).

    Publishing is deliberately NOT done here. The broker is a `main.py` concern; this
    loop's job is to make the event durable, and nothing more.
    """
    policy = policy or ToolPolicy()
    budget = budget or RunBudget(
        settings.max_run_cost_usd, settings.price_in_per_mtok, settings.price_out_per_mtok
    )
    tools = [spec.schema for spec in registry.values()]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": ticket_prompt(
                ticket["id"], ticket["customer_email"], ticket["subject"], ticket["body"]
            ),
        }
    ]

    async def _persisted(event: AgentEvent) -> AgentEvent:
        """Make one event durable, then hand it back to be yielded.

        Written as `yield await _persisted(AgentEvent(...))` at every yield site so
        persistence is impossible to reorder after the yield: the caller cannot see an
        event the database has not already committed, which is the whole of D-06 and
        the reason `event_stream` can publish on arrival with no ordering logic of its
        own. With no recorder this is a plain pass-through.

        Offloaded like every other DB touch in this codebase — `record` opens and
        closes its own transaction synchronously, and Database holds its lock for the
        length of that transaction, so running it on the loop would stall every other
        run on the process for the write. Nothing is held across the `yield` that
        follows: the transaction is already closed when this returns.
        """
        if recorder is not None:
            await asyncio.to_thread(recorder.record, event)
        return event

    resolved_via: str | None = None
    last_stop_reason: str | None = None

    # Every citation id search_docs hands this run, and only this run: a set shared
    # across runs would let one run cite another's docs. Grown in place below, and the
    # executor holds the same object, so a cite is valid the moment it is retrieved.
    retrieved_ids: set[str] = set()

    # The ids the D-08 probe has withheld from this run's accept-set. Filled at most
    # once (the first search that returns anything) but enforced for the LIFE of the
    # run, because a one-shot discard is not a withholding: retrieval.anchors() hands
    # back every heading of a returned doc, so any later search_docs call touching the
    # same file re-adds the dropped id verbatim and silently disarms the probe.
    seeded_drops: set[str] = set()

    # Built once per run, from this run's own ticket — never stored on the registry,
    # which is built once and shared by every live run. Every tool call below goes
    # through this, so the binding cannot be dropped for one call and not another.
    execute_bound = bind_to_ticket(ticket["id"], retrieved_ids)

    # The run span is parented explicitly (not made "current") because this is
    # a generator: execution suspends at every yield, and a current-span
    # context manager would leak across whatever runs in between.
    run_span = tracer.start_span(
        "agent.run",
        attributes={
            "relay.ticket_id": ticket["id"],
            "relay.model": settings.model,
            "relay.allow_writes": policy.allow_writes,
        },
    )
    run_ctx = trace.set_span_in_context(run_span)
    logger.info("run.start", extra={"ctx": {"ticket_id": ticket["id"], "model": settings.model,
                                            "allow_writes": policy.allow_writes}})
    try:
        for _ in range(settings.max_agent_steps):
            try:
                started = time.perf_counter()
                with tracer.start_as_current_span("claude.request", context=run_ctx) as span:
                    response = await client.messages.create(
                        model=settings.model,
                        max_tokens=settings.max_tokens,
                        system=[
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        tools=tools,
                        messages=messages,
                    )
                    span.set_attributes({
                        "relay.stop_reason": str(response.stop_reason),
                        "relay.input_tokens": response.usage.input_tokens,
                        "relay.output_tokens": response.usage.output_tokens,
                    })
                logger.info("model.response", extra={"ctx": {
                    "ticket_id": ticket["id"],
                    "stop_reason": response.stop_reason,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }})
            except anthropic.APIConnectionError:
                logger.error("model.connection_error", extra={"ctx": {"ticket_id": ticket["id"]}})
                yield await _persisted(
                    AgentEvent(type="error", data={"reason": "api_connection_error"})
                )
                return
            except anthropic.APIStatusError as exc:
                logger.error("model.api_error", extra={"ctx": {
                    "ticket_id": ticket["id"], "status": exc.status_code, "type": exc.type,
                }})
                yield await _persisted(AgentEvent(
                    type="error",
                    data={"reason": "api_error", "status": exc.status_code, "type": exc.type},
                ))
                return

            budget.add(response.usage)
            yield await _persisted(AgentEvent(type="usage", data=budget.snapshot()))

            if response.stop_reason == "refusal":
                yield await _persisted(
                    AgentEvent(type="error", data={"reason": "model_refusal"})
                )
                return

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    yield await _persisted(
                        AgentEvent(type="text", data={"text": block.text})
                    )
                elif block.type == "tool_use":
                    yield await _persisted(AgentEvent(
                        type="tool_use", data={"tool": block.name, "input": block.input}
                    ))
                    spec = registry.get(block.name)
                    with tracer.start_as_current_span(
                        f"tool.{block.name}", context=run_ctx
                    ) as span:
                        # Offloaded because tool execution is blocking SQLite and file I/O,
                        # and every other run on this process waits behind it otherwise.
                        # to_thread copies the current context, so this span is still the
                        # parent inside the worker. What it does not buy: cancelling the run
                        # returns from this await at once but the thread runs to completion,
                        # so a disconnect is not "no side effect" — the write is inside a
                        # transaction and either commits or rolls back, never lands halfway.
                        #
                        # Two offloads, one seam. A write-tier tool goes through the
                        # recorder so its `run_events` row is inserted INSIDE the
                        # transaction the tool itself opens on this worker thread — the
                        # reply and the record of the reply commit or roll back together
                        # (D-04). Splitting them would mean a customer emailed with
                        # nothing in the feed to show it happened, or the reverse.
                        # Read tools and everything else have no sibling write to nest
                        # into and are persisted by _persisted() at their yield.
                        #
                        # `tool_result_persisted` comes back False when the call was
                        # denied by a guardrail: nothing was written, so there is
                        # nothing to be atomic with, and the row is written below —
                        # AFTER the guardrail event — so the durable record reads in
                        # causal order rather than inverted (05-REVIEW CR-02).
                        if recorder is not None and spec is not None and spec.tier == "write":
                            result, is_error, tool_result_persisted = await asyncio.to_thread(
                                recorder.execute_and_record,
                                execute_bound, spec, block.name, block.input, policy,
                                event_type="tool_result",
                            )
                        else:
                            result, is_error = await asyncio.to_thread(
                                execute_bound, spec, block.name, block.input, policy
                            )
                            tool_result_persisted = False
                        payload = json.loads(result)
                        binding_violation = (
                            is_error and payload.get("denied_by") == "ticket_binding"
                        )
                        citation_violation = (
                            is_error and payload.get("denied_by") == "citation"
                        )
                        if block.name == "search_docs" and not is_error:
                            # Written here, on the event loop after the offloaded call
                            # has returned, so the set the executor closes over is never
                            # mutated from the worker thread.
                            #
                            # Every id a retrieved doc licenses, not just the
                            # query-derived `id`: the model was handed the whole file,
                            # so its bare name and any of its headings are all correct
                            # grounding (see retrieval.anchors). Narrowing this to `id`
                            # denies the accurate anchor and accepts the locator's guess.
                            for hit in payload.get("results", []):
                                if hit.get("doc"):
                                    retrieved_ids.add(hit["doc"])
                                if hit.get("id"):
                                    retrieved_ids.add(hit["id"])
                                retrieved_ids.update(a for a in hit.get("anchors") or () if a)
                            # Eval-only probe (D-08). Arms on the first search that
                            # returns anything, withholding the top hit's located id —
                            # the one the model is most likely to cite — so the very
                            # next send_reply cites something real that this run's
                            # accept-set does not contain, and the guard denies it.
                            #
                            # Nothing fabricated is added to keep the set non-empty. A
                            # placeholder id flows straight into the denial's
                            # `available` list and its `retrieved_ids` — the guard
                            # would be telling the model it may cite a source that was
                            # never retrieved, and then accepting it. That inverts
                            # RAG-04's property on the one path built to test it, and
                            # writes a citation into the artifact that the running
                            # system never served. Instead the probe declines to arm
                            # when withholding would empty the set: an empty accept-set
                            # denies everything with nothing to retry with, which is a
                            # different (unrecoverable) experiment. Only a single hit
                            # whose doc has no headings can reach that branch.
                            hits = payload.get("results") or []
                            if seed_citation_denial and not seeded_drops and hits:
                                dropped = hits[0].get("id")
                                if dropped and retrieved_ids - {dropped}:
                                    seeded_drops.add(dropped)
                                    logger.warning("guardrail.citation_denial_seeded",
                                                   extra={"ctx": {"ticket_id": ticket["id"],
                                                                  "dropped_id": dropped}})
                            # Re-applied after EVERY grow, not just the arming one. A
                            # second search returning the same file re-adds the dropped
                            # id as one of its anchors, and a probe that disarms itself
                            # produces an armed hook, zero denials and a clean terminal
                            # action — byte-identical in the artifact to a genuine
                            # recovery, which is the unfalsifiability D-08 exists to
                            # close. In place (the executor holds this exact set by
                            # reference); rebinding it would unbind the guard.
                            retrieved_ids.difference_update(seeded_drops)
                        span.set_attributes({
                            "relay.tool.tier": spec.tier if spec else "unknown",
                            "relay.tool.is_error": is_error,
                            "relay.tool.binding_violation": binding_violation,
                            "relay.tool.citation_violation": citation_violation,
                        })
                    logger.info("tool.executed", extra={"ctx": {
                        "ticket_id": ticket["id"], "tool": block.name, "is_error": is_error,
                    }})
                    if binding_violation:
                        logger.warning("guardrail.ticket_id_mismatch", extra={"ctx": {
                            "ticket_id": ticket["id"],
                            "tool": block.name,
                            "supplied_ticket_id": payload["supplied_ticket_id"],
                        }})
                        # Cause before effect, in the stream AND in `run_events`: a
                        # denied write tool skips the recorder's in-transaction insert
                        # (nothing was written to be atomic with), so this row takes the
                        # lower seq and the tool_result below follows it.
                        yield await _persisted(AgentEvent(
                            type="guardrail",
                            data={
                                "guard": "ticket_binding",
                                "tool": block.name,
                                "expected_ticket_id": payload["expected_ticket_id"],
                                "supplied_ticket_id": payload["supplied_ticket_id"],
                                "action": "denied",
                            },
                        ))
                    if block.name == "search_docs" and not is_error and payload.get("degraded"):
                        # A notice, not a guardrail: nothing was denied, the run just
                        # got weaker results than it asked for. Never ends the run —
                        # keyword hits are still hits, and silence here would hide a
                        # Voyage outage behind results that look normal (RAG-05, D-14).
                        logger.warning("retrieval.degraded", extra={"ctx": {
                            "ticket_id": ticket["id"],
                            "tool": block.name,
                            "retrieval_mode": payload.get("retrieval_mode"),
                            "cause": payload.get("degraded_cause"),
                            "results": len(payload.get("results", [])),
                        }})
                        yield await _persisted(AgentEvent(
                            type="notice",
                            data={
                                "kind": "retrieval_degraded",
                                "tool": block.name,
                                "retrieval_mode": payload.get("retrieval_mode"),
                                # "index_unavailable" (rebuild/commit kb/index.json)
                                # vs "voyage_failed" (check the key and upstream).
                                "cause": payload.get("degraded_cause"),
                                "results": len(payload.get("results", [])),
                            },
                        ))
                    if citation_violation:
                        logger.warning("guardrail.citation_unretrieved", extra={"ctx": {
                            "ticket_id": ticket["id"],
                            "tool": block.name,
                            "missing_citations": payload["missing_citations"],
                        }})
                        yield await _persisted(AgentEvent(
                            type="guardrail",
                            data={
                                "guard": "citation",
                                "tool": block.name,
                                "missing_citations": payload["missing_citations"],
                                "retrieved_ids": payload["retrieved_ids"],
                                "action": "denied",
                            },
                        ))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        }
                    )
                    tool_result_event = AgentEvent(
                        type="tool_result",
                        data={
                            "tool": block.name,
                            "result": payload,
                            "is_error": is_error,
                        },
                    )
                    # Not _persisted() when the recorder already wrote this row inside
                    # the tool's own transaction above — recording it again here would
                    # be a second, non-atomic copy of the same step.
                    if not tool_result_persisted:
                        tool_result_event = await _persisted(tool_result_event)
                    yield tool_result_event
                    if not is_error and block.name in TERMINAL_TOOLS:
                        resolved_via = block.name

            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            last_stop_reason = response.stop_reason
            if response.stop_reason != "tool_use":
                break
            if budget.exceeded:
                logger.warning("run.budget_exceeded", extra={"ctx": {
                    "ticket_id": ticket["id"], **budget.snapshot(),
                }})
                yield await _persisted(AgentEvent(
                    type="error", data={"reason": "budget_exceeded", **budget.snapshot()}
                ))
                return

        if resolved_via:
            yield await _persisted(
                AgentEvent(type="resolution", data={"via": resolved_via, **budget.snapshot()})
            )
        elif last_stop_reason == "end_turn" and not policy.allow_writes:
            # A dry run can never take a terminal action; a clean finish is success.
            yield await _persisted(
                AgentEvent(type="resolution", data={"via": None, **budget.snapshot()})
            )
        elif last_stop_reason == "end_turn":
            yield await _persisted(
                AgentEvent(type="error", data={"reason": "ended_without_action"})
            )
        else:
            yield await _persisted(AgentEvent(
                type="error",
                data={"reason": "step_limit_reached", "max_steps": settings.max_agent_steps},
            ))
    finally:
        run_span.set_attributes({
            "relay.outcome": resolved_via or "unresolved",
            "relay.cost_usd": budget.cost_usd,
            "relay.steps": budget.steps,
        })
        run_span.end()
        logger.info("run.end", extra={"ctx": {
            "ticket_id": ticket["id"], "outcome": resolved_via, **budget.snapshot(),
        }})

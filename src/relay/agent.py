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

Observability (phase 4): every run is an OpenTelemetry `agent.run` span with
one child span per model request and per tool execution, and each step emits
a structured JSON log line.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from opentelemetry import trace

from .config import settings
from .guardrails import RunBudget, ToolInputError, ToolPolicy, validate_tool_input
from .models import AgentEvent
from .prompts import SYSTEM_PROMPT, ticket_prompt
from .tools import ToolSpec

TERMINAL_TOOLS = {"send_reply", "create_escalation"}

logger = logging.getLogger("relay.agent")
tracer = trace.get_tracer("relay")


def _execute_guarded(
    spec: ToolSpec | None,
    name: str,
    raw_input: dict[str, Any],
    policy: ToolPolicy,
    *,
    bound_ticket_id: int | None = None,
) -> tuple[str, bool]:
    """Run one tool call through the guardrail chain. Returns (result_json, is_error)."""
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
    # bound_ticket_id is None on the MCP path, where there is no "current run".
    supplied_ticket_id = validated.get("ticket_id")
    if (
        bound_ticket_id is not None
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
) -> AsyncIterator[AgentEvent]:
    """Run the agent on one ticket, yielding an AgentEvent per step."""
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

    resolved_via: str | None = None
    last_stop_reason: str | None = None

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
                yield AgentEvent(type="error", data={"reason": "api_connection_error"})
                return
            except anthropic.APIStatusError as exc:
                logger.error("model.api_error", extra={"ctx": {
                    "ticket_id": ticket["id"], "status": exc.status_code, "type": exc.type,
                }})
                yield AgentEvent(
                    type="error",
                    data={"reason": "api_error", "status": exc.status_code, "type": exc.type},
                )
                return

            budget.add(response.usage)
            yield AgentEvent(type="usage", data=budget.snapshot())

            if response.stop_reason == "refusal":
                yield AgentEvent(type="error", data={"reason": "model_refusal"})
                return

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    yield AgentEvent(type="text", data={"text": block.text})
                elif block.type == "tool_use":
                    yield AgentEvent(
                        type="tool_use", data={"tool": block.name, "input": block.input}
                    )
                    spec = registry.get(block.name)
                    with tracer.start_as_current_span(
                        f"tool.{block.name}", context=run_ctx
                    ) as span:
                        result, is_error = _execute_guarded(spec, block.name, block.input, policy)
                        span.set_attributes({
                            "relay.tool.tier": spec.tier if spec else "unknown",
                            "relay.tool.is_error": is_error,
                        })
                    logger.info("tool.executed", extra={"ctx": {
                        "ticket_id": ticket["id"], "tool": block.name, "is_error": is_error,
                    }})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        }
                    )
                    yield AgentEvent(
                        type="tool_result",
                        data={
                            "tool": block.name,
                            "result": json.loads(result),
                            "is_error": is_error,
                        },
                    )
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
                yield AgentEvent(
                    type="error", data={"reason": "budget_exceeded", **budget.snapshot()}
                )
                return

        if resolved_via:
            yield AgentEvent(type="resolution", data={"via": resolved_via, **budget.snapshot()})
        elif last_stop_reason == "end_turn" and not policy.allow_writes:
            # A dry run can never take a terminal action; a clean finish is success.
            yield AgentEvent(type="resolution", data={"via": None, **budget.snapshot()})
        elif last_stop_reason == "end_turn":
            yield AgentEvent(type="error", data={"reason": "ended_without_action"})
        else:
            yield AgentEvent(
                type="error",
                data={"reason": "step_limit_reached", "max_steps": settings.max_agent_steps},
            )
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

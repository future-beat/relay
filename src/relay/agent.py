"""The agent loop, written by hand on the Claude API.

No framework: the loop is a plain request -> tool-execute -> append cycle so the
control flow, guardrails, and event stream are fully visible and testable.

Guardrails enforced here (phase 2):
- every tool input is validated with Pydantic before execution
- a ToolPolicy gates write-tier tools (dry-run mode)
- a RunBudget tracks token spend and aborts the run at a hard cost ceiling
- API failures end the run with a structured error event, never a stack trace
  (transient 429/5xx are already retried inside the SDK)
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from .config import settings
from .guardrails import RunBudget, ToolInputError, ToolPolicy, validate_tool_input
from .models import AgentEvent
from .prompts import SYSTEM_PROMPT, ticket_prompt
from .tools import ToolSpec

TERMINAL_TOOLS = {"send_reply", "create_escalation"}


def _execute_guarded(spec: ToolSpec | None, name: str, raw_input: dict[str, Any],
                     policy: ToolPolicy) -> tuple[str, bool]:
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
    try:
        return spec.execute(**validated), False
    except Exception as exc:  # surfaced to the model, not swallowed
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

    for _ in range(settings.max_agent_steps):
        try:
            response = await client.messages.create(
                model=settings.model,
                max_tokens=settings.max_tokens,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                tools=tools,
                messages=messages,
            )
        except anthropic.APIConnectionError:
            yield AgentEvent(type="error", data={"reason": "api_connection_error"})
            return
        except anthropic.APIStatusError as exc:
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
                yield AgentEvent(type="tool_use", data={"tool": block.name, "input": block.input})
                spec = registry.get(block.name)
                result, is_error = _execute_guarded(spec, block.name, block.input, policy)
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
                    data={"tool": block.name, "result": json.loads(result), "is_error": is_error},
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

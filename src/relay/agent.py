"""The agent loop, written by hand on the Claude API.

No framework: the loop is a plain request -> tool-execute -> append cycle so the
control flow, step caps, and event stream are fully visible and testable.
"""

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from .config import settings
from .models import AgentEvent
from .prompts import SYSTEM_PROMPT, ticket_prompt
from .tools import ToolSpec

TERMINAL_TOOLS = {"send_reply", "create_escalation"}


async def run_ticket(
    client: AsyncAnthropic,
    registry: dict[str, ToolSpec],
    ticket: dict[str, Any],
) -> AsyncIterator[AgentEvent]:
    """Run the agent on one ticket, yielding an AgentEvent per step."""
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

    for _ in range(settings.max_agent_steps):
        response = await client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )

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
                if spec is None:
                    result, is_error = json.dumps({"error": f"unknown tool {block.name}"}), True
                else:
                    try:
                        result, is_error = spec.execute(**block.input), False
                    except Exception as exc:  # surfaced to the model, not swallowed
                        result, is_error = json.dumps({"error": str(exc)}), True
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

        if response.stop_reason != "tool_use":
            break

    if resolved_via:
        yield AgentEvent(type="resolution", data={"via": resolved_via})
    else:
        yield AgentEvent(
            type="error",
            data={"reason": "step_limit_reached", "max_steps": settings.max_agent_steps},
        )

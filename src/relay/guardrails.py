"""Phase 2 guardrails: validated tool I/O, cost budgets, and write-tool policy.

Everything here is enforced in the agent loop — not just requested in the
prompt — so a misbehaving model run is contained by code.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .models import TicketCategory


class LookupCustomerInput(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class SearchDocsInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class SetCategoryInput(BaseModel):
    ticket_id: int = Field(gt=0)
    category: TicketCategory


class SendReplyInput(BaseModel):
    ticket_id: int = Field(gt=0)
    body: str = Field(min_length=20, max_length=10_000)
    # Optional on purpose (D-12): the phase-3 guard rejects a reply citing a source
    # that was never retrieved — subset validation, not "must cite at least one".
    # An empty list is always a subset, so a citation-less reply stays legal and the
    # model is never pushed into ending a run without a terminal action.
    citations: list[str] = Field(default_factory=list, max_length=20)


class CreateEscalationInput(BaseModel):
    ticket_id: int = Field(gt=0)
    reason: str = Field(min_length=20, max_length=10_000)
    priority: str = Field(pattern="^(low|medium|high)$")


def validate_tool_input(model: type[BaseModel], raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a model-produced tool input. Raises ToolInputError with a
    message the model can act on."""
    try:
        return model.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        raise ToolInputError(f"invalid tool input — {problems}") from exc


class ToolInputError(Exception):
    pass


@dataclass
class ToolPolicy:
    """Decides whether a tool may run. Write-tier tools can be disabled per
    run (dry-run), in which case the model gets an explanatory error result
    instead of a side effect."""

    allow_writes: bool = True

    def denial_reason(self, tier: str) -> str | None:
        if tier == "write" and not self.allow_writes:
            return (
                "This run is in dry-run mode: write actions are disabled by policy."
                " Summarise what you would have done instead."
            )
        return None


class RunBudget:
    """Accumulates token usage across a run and enforces a hard cost ceiling.

    Cache reads/writes are priced at their multipliers so long cached system
    prompts don't inflate the measured cost.
    """

    def __init__(self, max_cost_usd: float, price_in_per_mtok: float, price_out_per_mtok: float):
        self.max_cost_usd = max_cost_usd
        self._in = price_in_per_mtok / 1_000_000
        self._out = price_out_per_mtok / 1_000_000
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.steps = 0

    def add(self, usage: Any) -> None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.input_tokens += usage.input_tokens + cache_read + cache_write
        self.output_tokens += usage.output_tokens
        self.cost_usd += (
            usage.input_tokens * self._in
            + cache_read * self._in * 0.1
            + cache_write * self._in * 1.25
            + usage.output_tokens * self._out
        )
        self.steps += 1

    @property
    def exceeded(self) -> bool:
        return self.cost_usd >= self.max_cost_usd

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "max_cost_usd": self.max_cost_usd,
        }

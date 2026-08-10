from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class TicketCategory(str, Enum):
    billing = "billing"
    technical = "technical"
    account = "account"
    feature_request = "feature_request"
    other = "other"


class TicketStatus(str, Enum):
    open = "open"
    resolved = "resolved"
    escalated = "escalated"


class TicketCreate(BaseModel):
    customer_email: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


class Ticket(BaseModel):
    id: int
    customer_email: str
    subject: str
    body: str
    status: TicketStatus
    category: TicketCategory | None = None
    created_at: datetime


class AgentEvent(BaseModel):
    """One step in an agent run, streamed to the client as SSE."""

    # "text" | "tool_use" | "tool_result" | "guardrail" | "notice" | "usage"
    # | "resolution" | "error"
    # "notice" is degraded-but-continuing news for the viewer (retrieval fell back to
    # keyword search); "guardrail" is a control that fired and denied something.
    type: str
    data: dict[str, Any]

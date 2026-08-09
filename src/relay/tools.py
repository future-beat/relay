"""Agent tools: Claude-facing schemas plus their local executors.

Each tool carries a permission tier ("read" or "write") so phase 2 can gate
write actions behind confirmation or policy without touching the loop.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .db import Database
from .guardrails import (
    CreateEscalationInput,
    LookupCustomerInput,
    SearchDocsInput,
    SendReplyInput,
    SetCategoryInput,
)


@dataclass(frozen=True)
class ToolSpec:
    schema: dict[str, Any]
    tier: str  # "read" | "write"
    input_model: type[BaseModel]
    execute: Callable[..., str]


def lookup_customer(db: Database, email: str) -> str:
    row = db.execute("SELECT * FROM customers WHERE email = ?", (email,)).fetchone()
    if row is None:
        return json.dumps({"found": False})
    tickets = db.execute(
        "SELECT id, subject, status, created_at FROM tickets"
        " WHERE customer_email = ? ORDER BY created_at DESC LIMIT 10",
        (email,),
    ).fetchall()
    return json.dumps(
        {"found": True, "customer": dict(row), "recent_tickets": [dict(t) for t in tickets]}
    )


def search_docs(kb_dir: Path, query: str, max_results: int = 3) -> str:
    """Keyword search over the markdown knowledge base.

    Phase 1 keeps this dependency-free; the embeddings-based retriever
    replaces the scoring here without changing the tool contract.
    """
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    scored = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        score = sum(lower.count(term) for term in terms)
        if score > 0:
            scored.append((score, path.name, text))
    scored.sort(reverse=True)
    results = [{"doc": name, "content": text} for _, name, text in scored[:max_results]]
    return json.dumps({"results": results})


def create_escalation(db: Database, ticket_id: int, reason: str, priority: str) -> str:
    with db.transaction():
        cur = db.execute(
            "INSERT INTO escalations (ticket_id, reason, priority) VALUES (?, ?, ?)",
            (ticket_id, reason, priority),
        )
        db.execute("UPDATE tickets SET status = 'escalated' WHERE id = ?", (ticket_id,))
        # Read inside the block: once the lock drops another thread's insert has moved it.
        escalation_id = cur.lastrowid
    return json.dumps({"escalation_id": escalation_id, "status": "escalated"})


def send_reply(db: Database, ticket_id: int, body: str) -> str:
    # Email delivery is mocked: the reply is persisted, nothing leaves the system.
    with db.transaction():
        cur = db.execute(
            "INSERT INTO replies (ticket_id, body) VALUES (?, ?)", (ticket_id, body)
        )
        db.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        reply_id = cur.lastrowid
    return json.dumps({"reply_id": reply_id, "status": "resolved"})


def set_category(db: Database, ticket_id: int, category: str) -> str:
    with db.transaction():
        db.execute("UPDATE tickets SET category = ? WHERE id = ?", (category, ticket_id))
    return json.dumps({"ticket_id": ticket_id, "category": category})


def build_registry(conn: Database, kb_dir: Path) -> dict[str, ToolSpec]:
    return {
        "lookup_customer": ToolSpec(
            schema={
                "name": "lookup_customer",
                "description": (
                    "Look up a customer by email. Returns their profile (name, plan,"
                    " signup date) and their 10 most recent tickets. Call this first"
                    " for every ticket so your reply reflects who the customer is."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Customer email address"}
                    },
                    "required": ["email"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="read",
            input_model=LookupCustomerInput,
            execute=lambda email: lookup_customer(conn, email),
        ),
        "search_docs": ToolSpec(
            schema={
                "name": "search_docs",
                "description": (
                    "Search the product documentation. Call this before answering any"
                    " product or policy question — ground every claim in a returned doc"
                    " rather than answering from memory."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords"}
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="read",
            input_model=SearchDocsInput,
            execute=lambda query: search_docs(kb_dir, query),
        ),
        "set_category": ToolSpec(
            schema={
                "name": "set_category",
                "description": "Record the ticket's category after classifying it.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "integer"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "billing",
                                "technical",
                                "account",
                                "feature_request",
                                "other",
                            ],
                        },
                    },
                    "required": ["ticket_id", "category"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="write",
            input_model=SetCategoryInput,
            execute=lambda ticket_id, category: set_category(conn, ticket_id, category),
        ),
        "send_reply": ToolSpec(
            schema={
                "name": "send_reply",
                "description": (
                    "Send the final reply to the customer and mark the ticket resolved."
                    " Call this only when the answer is fully grounded in documentation"
                    " and customer data. This ends the ticket."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "integer"},
                        "body": {"type": "string", "description": "The reply text"},
                    },
                    "required": ["ticket_id", "body"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="write",
            input_model=SendReplyInput,
            execute=lambda ticket_id, body: send_reply(conn, ticket_id, body),
        ),
        "create_escalation": ToolSpec(
            schema={
                "name": "create_escalation",
                "description": (
                    "Escalate the ticket to a human agent when you cannot resolve it:"
                    " the docs don't cover it, it needs account changes you can't make,"
                    " or the customer is at risk of churning. This ends the ticket."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "integer"},
                        "reason": {"type": "string", "description": "Structured handover summary"},
                        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["ticket_id", "reason", "priority"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="write",
            input_model=CreateEscalationInput,
            execute=lambda ticket_id, reason, priority: create_escalation(
                conn, ticket_id, reason, priority
            ),
        ),
    }

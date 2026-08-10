"""Agent tools: Claude-facing schemas plus their local executors.

Each tool carries a permission tier ("read" or "write") so phase 2 can gate
write actions behind confirmation or policy without touching the loop.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import retrieval
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


def search_docs(index: retrieval.Index, query: str, max_results: int = 3) -> str:
    """Hybrid semantic + keyword search over the markdown knowledge base.

    Phase 3 swapped the phase-1 scorer for `retrieval.retrieve` and changed nothing
    else: the envelope is still `{"results": [...]}` carrying whole files (D-01), so
    retrieval quality is the only variable that moved. Each result gained the
    `{doc, heading, id, text, score}` citation shape (D-06), and `retrieval_mode` /
    `degraded` were added alongside `results` so a run can show which path served it.

    `index` is loaded once by `build_registry` and captured by the closure. Sync on
    purpose — this runs inside the phase-2 `asyncio.to_thread` seam.
    """
    # key/floor are left to retrieve()'s settings sentinel, so 03-06's calibrated
    # floor lands with no change here.
    results, mode, degraded, cause = retrieval.retrieve(index, query, max_results=max_results)
    return json.dumps({
        "results": results,
        "retrieval_mode": mode,
        "degraded": degraded,
        # Which degradation, so the notice is actionable rather than just alarming:
        # a stale/missing index is a deploy fix, a Voyage failure is a runtime one.
        "degraded_cause": cause,
    })


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


def send_reply(
    db: Database, ticket_id: int, body: str, citations: Sequence[str] = ()
) -> str:
    # citations is accepted and validated but not persisted this phase: it exists so the
    # model declares its grounding, which agent.py checks against the ids search_docs
    # actually returned. Defaulted so every pre-phase-3 call site still works (D-12).
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
    # Read the embedding index once at startup and let the search_docs closure capture
    # it, the same way the other closures capture `conn`. A per-call load would put a
    # disk read and a full re-parse of every vector inside every tool call.
    index = retrieval.load_index(kb_dir)
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
            execute=lambda query: search_docs(index, query),
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
                        "citations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The search_docs result ids this reply is grounded in,"
                                ' e.g. "billing.md#refunds". Cite only ids that were'
                                " returned to you — a made-up id is rejected."
                            ),
                        },
                    },
                    "required": ["ticket_id", "body"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            tier="write",
            input_model=SendReplyInput,
            execute=lambda ticket_id, body, citations=(): send_reply(
                conn, ticket_id, body, citations
            ),
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

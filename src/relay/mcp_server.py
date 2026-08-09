"""Phase 5: expose Relay's tools over the Model Context Protocol.

Any MCP client (Claude Desktop, Claude Code, an IDE) can drive the same
tool registry the agent loop uses — through the same guardrail chain:
Pydantic input validation and the write-tool policy. Two MCP-only tools
(create_ticket, list_open_tickets) are added so a client can work the
system end to end.

Run over stdio:
    python -m relay.mcp_server

Writes are disabled by default — the tool surface is read-only until an
operator sets RELAY_MCP_ALLOW_WRITES=true to opt in.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, EmailStr, Field

from .agent import _execute_guarded
from .config import settings
from .db import connect, init_db
from .guardrails import ToolPolicy
from .tools import ToolSpec, build_registry


class CreateTicketInput(BaseModel):
    customer_email: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


class NoInput(BaseModel):
    pass


def _create_ticket(conn: sqlite3.Connection, customer_email: str, subject: str, body: str) -> str:
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        (customer_email, subject, body),
    )
    conn.commit()
    return json.dumps({"ticket_id": cur.lastrowid, "status": "open"})


def _list_open_tickets(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT id, customer_email, subject, status, created_at FROM tickets"
        " WHERE status = 'open' ORDER BY created_at"
    ).fetchall()
    return json.dumps({"open_tickets": [dict(r) for r in rows]})


def build_mcp_registry(conn: sqlite3.Connection, kb_dir: Path) -> dict[str, ToolSpec]:
    """The agent's registry plus the ticket-lifecycle tools an MCP client needs."""
    registry = dict(build_registry(conn, kb_dir))
    registry["create_ticket"] = ToolSpec(
        schema={
            "name": "create_ticket",
            "description": "File a new support ticket for a customer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_email": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["customer_email", "subject", "body"],
                "additionalProperties": False,
            },
        },
        tier="write",
        input_model=CreateTicketInput,
        execute=lambda customer_email, subject, body: _create_ticket(
            conn, customer_email, subject, body
        ),
    )
    registry["list_open_tickets"] = ToolSpec(
        schema={
            "name": "list_open_tickets",
            "description": "List all currently open support tickets.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        tier="read",
        input_model=NoInput,
        execute=lambda: _list_open_tickets(conn),
    )
    return registry


def list_mcp_tools(registry: dict[str, ToolSpec]) -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=spec.schema["description"],
            input_schema=spec.schema["input_schema"],
        )
        for name, spec in registry.items()
    ]


def call_mcp_tool(
    registry: dict[str, ToolSpec], policy: ToolPolicy, name: str, arguments: dict
) -> str:
    """Run one MCP tool call through the same guardrail chain as the agent loop.

    Raises RuntimeError on any guarded failure so the MCP server reports it
    as a tool error to the client.
    """
    result, is_error = _execute_guarded(registry.get(name), name, arguments or {}, policy)
    if is_error:
        raise RuntimeError(json.loads(result)["error"])
    return result


def create_server(conn: sqlite3.Connection) -> Server:
    registry = build_mcp_registry(conn, settings.kb_dir)
    policy = ToolPolicy(allow_writes=settings.mcp_allow_writes)

    async def _on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list_mcp_tools(registry))

    async def _on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            text = call_mcp_tool(registry, policy, params.name, params.arguments or {})
        except RuntimeError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], is_error=True
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return Server("relay", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def amain() -> None:
    conn = connect(settings.db_path)
    init_db(conn)
    server = create_server(conn)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

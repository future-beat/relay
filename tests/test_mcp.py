import json
from pathlib import Path

import pytest

from relay.guardrails import ToolPolicy
from relay.mcp_server import build_mcp_registry, call_mcp_tool, list_mcp_tools

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture()
def mcp_registry(conn):
    return build_mcp_registry(conn, KB_DIR)


def test_exposes_agent_tools_plus_ticket_lifecycle(mcp_registry):
    tools = {t.name for t in list_mcp_tools(mcp_registry)}
    assert tools == {
        "lookup_customer", "search_docs", "set_category", "send_reply",
        "create_escalation", "create_ticket", "list_open_tickets",
    }
    for tool in list_mcp_tools(mcp_registry):
        assert tool.input_schema["type"] == "object"
        assert tool.description


def test_call_read_tool(mcp_registry):
    result = json.loads(
        call_mcp_tool(mcp_registry, ToolPolicy(), "search_docs", {"query": "refund"})
    )
    assert result["results"][0]["doc"] == "billing.md"


def test_ticket_lifecycle_roundtrip(mcp_registry):
    created = json.loads(
        call_mcp_tool(mcp_registry, ToolPolicy(), "create_ticket", {
            "customer_email": "ava@acmecorp.com",
            "subject": "Hello",
            "body": "Need help with SSO.",
        })
    )
    listed = json.loads(call_mcp_tool(mcp_registry, ToolPolicy(), "list_open_tickets", {}))
    assert [t["id"] for t in listed["open_tickets"]] == [created["ticket_id"]]


def test_write_tool_denied_when_read_only(mcp_registry):
    with pytest.raises(RuntimeError, match="dry-run"):
        call_mcp_tool(mcp_registry, ToolPolicy(allow_writes=False), "create_ticket", {
            "customer_email": "ava@acmecorp.com", "subject": "x", "body": "y",
        })


def test_invalid_input_raises(mcp_registry):
    with pytest.raises(RuntimeError, match="invalid tool input"):
        call_mcp_tool(mcp_registry, ToolPolicy(), "send_reply", {"ticket_id": 1, "body": "hi"})


def test_unknown_tool_raises(mcp_registry):
    with pytest.raises(RuntimeError, match="unknown tool"):
        call_mcp_tool(mcp_registry, ToolPolicy(), "nope", {})

from types import SimpleNamespace

import pytest

from relay.agent import run_ticket
from relay.guardrails import (
    RunBudget,
    SendReplyInput,
    ToolInputError,
    ToolPolicy,
    validate_tool_input,
)

TICKET = {
    "id": 1,
    "customer_email": "liam@brightco.io",
    "subject": "API limits",
    "body": "What are my rate limits?",
}


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use(name, input, id="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def _usage(inp=1000, out=500):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )


def _response(content, stop_reason="tool_use", usage=None):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage or _usage())


class FakeClient:
    """Plays back scripted responses in place of the Claude API."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return next(self._responses)


async def collect(gen):
    return [event async for event in gen]


# --- input validation ---

def test_validate_rejects_short_reply():
    with pytest.raises(ToolInputError, match="body"):
        validate_tool_input(SendReplyInput, {"ticket_id": 1, "body": "ok"})


def test_validate_accepts_good_input():
    data = validate_tool_input(
        SendReplyInput, {"ticket_id": 1, "body": "Here is a complete, helpful answer."}
    )
    assert data["ticket_id"] == 1


async def test_invalid_tool_input_returned_to_model_as_error(registry):
    client = FakeClient([
        _response([_tool_use("send_reply", {"ticket_id": 1, "body": "ok"})]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    result = next(e for e in events if e.type == "tool_result")
    assert result.data["is_error"] is True
    assert "invalid tool input" in result.data["result"]["error"]


# --- write policy / dry-run ---

async def test_dry_run_denies_write_tools_but_allows_reads(registry):
    client = FakeClient([
        _response([
            _tool_use("search_docs", {"query": "rate limits"}, id="t1"),
            _tool_use("send_reply", {"ticket_id": 1, "body": "A long enough grounded reply."}, id="t2"),
        ]),
        _response([_text("Noted, dry-run.")], stop_reason="end_turn"),
    ])
    events = await collect(
        run_ticket(client, registry, TICKET, policy=ToolPolicy(allow_writes=False))
    )
    results = {e.data["tool"]: e.data for e in events if e.type == "tool_result"}
    assert results["search_docs"]["is_error"] is False
    assert results["send_reply"]["is_error"] is True
    assert results["send_reply"]["result"]["denied_by"] == "policy"


async def test_dry_run_never_writes_to_db(conn, registry):
    client = FakeClient([
        _response([
            _tool_use("send_reply", {"ticket_id": 1, "body": "A long enough grounded reply."})
        ]),
        _response([_text("ok")], stop_reason="end_turn"),
    ])
    await collect(run_ticket(client, registry, TICKET, policy=ToolPolicy(allow_writes=False)))
    assert conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 0


# --- budget ---

def test_budget_accumulates_cost():
    budget = RunBudget(max_cost_usd=1.0, price_in_per_mtok=5.0, price_out_per_mtok=25.0)
    budget.add(_usage(inp=100_000, out=20_000))
    assert budget.cost_usd == pytest.approx(0.5 + 0.5)
    assert budget.exceeded


async def test_budget_exceeded_aborts_run(registry):
    client = FakeClient([
        _response(
            [_tool_use("search_docs", {"query": "limits"})],
            usage=_usage(inp=200_000, out=50_000),  # far over a $0.10 ceiling
        ),
        _response([_text("should never be reached")], stop_reason="end_turn"),
    ])
    budget = RunBudget(max_cost_usd=0.10, price_in_per_mtok=5.0, price_out_per_mtok=25.0)
    events = await collect(run_ticket(client, registry, TICKET, budget=budget))
    assert events[-1].type == "error"
    assert events[-1].data["reason"] == "budget_exceeded"


async def test_usage_events_streamed(registry):
    client = FakeClient([_response([_text("All done.")], stop_reason="end_turn")])
    events = await collect(run_ticket(client, registry, TICKET))
    usage = next(e for e in events if e.type == "usage")
    assert usage.data["input_tokens"] == 1000
    assert usage.data["cost_usd"] > 0


# --- API failure ---

async def test_api_error_yields_structured_event(registry):
    import anthropic
    import httpx

    class ErrorClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))

    events = await collect(run_ticket(ErrorClient(), registry, TICKET))
    assert events == [events[0]]
    assert events[0].type == "error"
    assert events[0].data["reason"] == "api_connection_error"


async def test_dry_run_clean_finish_is_success_not_error(registry):
    client = FakeClient([
        _response([_text("Here is what I would have done.")], stop_reason="end_turn"),
    ])
    events = await collect(
        run_ticket(client, registry, TICKET, policy=ToolPolicy(allow_writes=False))
    )
    assert events[-1].type == "resolution"
    assert events[-1].data["via"] is None


async def test_normal_run_ending_without_action_is_error(registry):
    client = FakeClient([
        _response([_text("I think that answers it.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    assert events[-1].type == "error"
    assert events[-1].data["reason"] == "ended_without_action"

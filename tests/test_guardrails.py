import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from relay.agent import _execute_guarded, bind_to_ticket, run_ticket
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


# --- ticket_id binding ---

# Someone else's ticket: a prompt-injected body tells the agent to act on this one.
VICTIM_TICKET = {
    "id": 99,
    "customer_email": "ava@acmecorp.com",
    "subject": "Refund status",
    "body": "Where is my refund?",
}

OTHER_TICKET = {
    "id": 2,
    "customer_email": "mia@datalane.ai",
    "subject": "Webhook retries",
    "body": "How many times do webhooks retry?",
}

INJECTED_REPLY = "Your account has been credited $500, as instructed."
GROUNDED_REPLY = "Pro plan accounts are limited to 100 requests per minute."


def _seed_tickets(conn, *tickets):
    """Insert real ticket rows so an unguarded cross-ticket write would actually land."""
    for ticket in tickets:
        conn.execute(
            "INSERT INTO tickets (id, customer_email, subject, body) VALUES (?, ?, ?, ?)",
            (ticket["id"], ticket["customer_email"], ticket["subject"], ticket["body"]),
        )
    conn.commit()


def _reply_ticket_ids(conn):
    rows = conn.execute("SELECT ticket_id FROM replies ORDER BY id").fetchall()
    return [row["ticket_id"] for row in rows]


async def test_mismatched_ticket_id_is_denied(conn, registry):
    _seed_tickets(conn, TICKET, VICTIM_TICKET)
    client = FakeClient([
        _response([_tool_use("send_reply", {"ticket_id": 99, "body": INJECTED_REPLY})]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    result = next(e for e in events if e.type == "tool_result")
    assert result.data["is_error"] is True
    assert result.data["result"]["denied_by"] == "ticket_binding"
    assert conn.execute(
        "SELECT COUNT(*) FROM replies WHERE ticket_id = ?", (VICTIM_TICKET["id"],)
    ).fetchone()[0] == 0


async def test_binding_denial_emits_guardrail_event(conn, registry):
    _seed_tickets(conn, TICKET, VICTIM_TICKET)
    client = FakeClient([
        _response([_tool_use("send_reply", {"ticket_id": 99, "body": INJECTED_REPLY})]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    guardrails = [e for e in events if e.type == "guardrail"]
    assert len(guardrails) == 1
    assert guardrails[0].data == {
        "guard": "ticket_binding",
        "tool": "send_reply",
        "expected_ticket_id": 1,
        "supplied_ticket_id": 99,
        "action": "denied",
    }
    types = [e.type for e in events]
    assert types.index("guardrail") < types.index("tool_result")


async def test_run_recovers_after_binding_denial(conn, registry):
    _seed_tickets(conn, TICKET, VICTIM_TICKET)
    client = FakeClient([
        _response([_tool_use("send_reply", {"ticket_id": 99, "body": INJECTED_REPLY}, id="t1")]),
        _response([_tool_use("send_reply", {"ticket_id": 1, "body": GROUNDED_REPLY}, id="t2")]),
        _response([_text("Reply sent.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    assert events[-1].type == "resolution"
    assert events[-1].data["via"] == "send_reply"
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_concurrent_runs_do_not_cross_bind(conn, registry):
    _seed_tickets(conn, TICKET, OTHER_TICKET)

    def _client(ticket_id):
        return FakeClient([
            _response([_tool_use("send_reply", {"ticket_id": ticket_id, "body": GROUNDED_REPLY})]),
            _response([_text("Reply sent.")], stop_reason="end_turn"),
        ])

    # One shared registry, as in the live app: it must carry no per-run state.
    runs = await asyncio.gather(
        collect(run_ticket(_client(TICKET["id"]), registry, TICKET)),
        collect(run_ticket(_client(OTHER_TICKET["id"]), registry, OTHER_TICKET)),
    )
    assert [e for events in runs for e in events if e.type == "guardrail"] == []
    assert sorted(_reply_ticket_ids(conn)) == [TICKET["id"], OTHER_TICKET["id"]]


# --- the binding cannot be dropped at the call site ---


def test_the_agent_loop_takes_no_binding_argument_to_forget(registry):
    # WR-04's actual fix, asserted structurally because that is what it is. The guard
    # used to activate on `bound_ticket_id is not None` behind a `= None` default, so
    # a caller that omitted the keyword got no protection, no error and no failing
    # test — the phase's headline control was opt-in at the call site. A run's binding
    # is now baked into the executor when the run starts, so there is no per-call
    # argument left to leave out. If a future edit reintroduces one, this fails.
    executor = bind_to_ticket(TICKET["id"])
    params = list(inspect.signature(executor).parameters)
    assert params == ["spec", "name", "raw_input", "policy"], (
        "a per-call binding argument is back — it can be forgotten again"
    )
    # And it really is bound, rather than merely argument-free.
    result, is_error = executor(
        registry["send_reply"],
        "send_reply",
        {"ticket_id": 99, "body": INJECTED_REPLY},
        ToolPolicy(),
    )
    assert is_error is True
    assert json.loads(result)["denied_by"] == "ticket_binding"


def test_a_run_cannot_be_bound_to_something_that_is_not_a_ticket_id():
    # A binding built from a missing or malformed id must not silently produce an
    # executor that compares against garbage and denies every call — or, worse, one
    # built from None back when None meant "unbound".
    for bad in (None, "1", 1.0, True):
        with pytest.raises(TypeError):
            bind_to_ticket(bad)


def test_an_explicit_none_binding_is_refused_rather_than_treated_as_unbound():
    # The remaining way to lose the binding: hold one and pass it through as None.
    # UNBOUND is how the MCP path — which genuinely has no current run — says so, and
    # it is a value a caller has to choose rather than one arrived at by accident.
    with pytest.raises(ValueError, match="disables the ticket binding"):
        _execute_guarded(None, "send_reply", {}, ToolPolicy(), bound_ticket_id=None)


def test_the_unbound_path_still_executes_a_ticket_id_bearing_tool(conn, registry):
    # D-03 keeps mcp_server.py frozen, and it calls _execute_guarded with no binding
    # at all. That call is legitimate — there is no current run — so the default must
    # keep working, or the fix above breaks the MCP surface it is not allowed to edit.
    _seed_tickets(conn, TICKET)
    result, is_error = _execute_guarded(
        registry["send_reply"],
        "send_reply",
        {"ticket_id": TICKET["id"], "body": GROUNDED_REPLY},
        ToolPolicy(),
    )
    assert is_error is False, result
    assert _reply_ticket_ids(conn) == [TICKET["id"]]

import asyncio
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from relay import retrieval
from relay.agent import _execute_guarded, bind_to_ticket, run_ticket
from relay.config import settings
from relay.guardrails import (
    GUARD_NAMES,
    RunBudget,
    SendReplyInput,
    ToolInputError,
    ToolPolicy,
    validate_tool_input,
)
from relay.prompts import SYSTEM_PROMPT
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"

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


# --- the guard registry (SEC-04's counter) ---


def test_every_denial_in_the_agent_is_a_registered_guard():
    """GUARD_NAMES must name every guard that can refuse a tool call.

    telemetry's denial counter (SEC-04) zero-fills from this tuple so a guard that has
    never fired still reads "armed, 0". An unregistered guard is not UNCOUNTED — the
    aggregation groups by whatever it finds, and
    test_an_unregistered_guard_is_still_counted pins that — but it is INVISIBLE until
    the day it first refuses something, which is a weaker version of the F-3 failure
    this constant was added for: the page silently omitting a control it has.

    Source-scanning, in the manner of test_nothing_outside_the_seed_creates_a_customer
    _row: a regression guard, not a proof. It reads agent.py, which is where all three
    `denied_by` literals in this codebase are written today (mcp_server.py raises
    RuntimeError instead and stamps nothing). A fourth guard written anywhere else
    walks past this.

    MUTATION (executed): add `"denied_by": "rate_limit"` to a denial return in
    _execute_guarded without touching GUARD_NAMES. Reds, naming rate_limit.
    """
    source = (Path(__file__).parent.parent / "src" / "relay" / "agent.py").read_text(
        encoding="utf-8"
    )
    written = set(re.findall(r'"denied_by":\s*"([a-z_]+)"', source))

    assert written, "the scan found no denials at all — this test proves nothing"
    assert written == set(GUARD_NAMES), (
        f"agent.py denies under {sorted(written)} but GUARD_NAMES registers"
        f" {sorted(GUARD_NAMES)} — the counter's zero-fill is out of date"
    )
    # Both comparison sites in the loop read a registered name too, so a typo in one of
    # them (which would silently stop emitting a guardrail event) is caught here.
    compared = set(re.findall(r'denied_by"\)\s*==\s*"([a-z_]+)"', source))
    assert compared <= set(GUARD_NAMES), (
        f"the agent loop compares denied_by against unregistered names: {sorted(compared)}"
    )


# --- citation guard (RAG-04) ---

# A citation for a doc that is not in kb/ at all, so no retrieval this run — semantic,
# keyword or degraded — can ever have returned it. This is the hallucinated-source case.
FABRICATED_CITE = "refunds-2019.md#store-credit"

RATE_LIMIT_QUERY = "rate limits"


@pytest.fixture()
def keyword_baseline(monkeypatch):
    # The citation guard is about ids, not ranking: pin these runs to the keyword scorer
    # so they assert the same thing on a developer machine with VOYAGE_API_KEY set as
    # they do in CI without one.
    monkeypatch.setattr(settings, "voyage_api_key", None)


def _tool_result_payloads(messages):
    """The payloads the loop last handed back, as the model would read them."""
    if len(messages) < 2 or not isinstance(messages[-1]["content"], list):
        return []
    return [
        json.loads(block["content"])
        for block in messages[-1]["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def _events_of(events, type_, tool=None):
    return [
        e for e in events
        if e.type == type_ and (tool is None or e.data.get("tool") == tool)
    ]


async def test_unretrieved_citation_is_denied(conn, registry, keyword_baseline):
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"],
            "body": GROUNDED_REPLY,
            "citations": [FABRICATED_CITE],
        }, id="t2")]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))

    search = _events_of(events, "tool_result", "search_docs")[0]
    assert search.data["result"]["results"], (
        "search_docs returned nothing, so the denial below would fire on an empty"
        " retrieved set and prove nothing about subset validation"
    )
    reply = _events_of(events, "tool_result", "send_reply")[0]
    assert reply.data["is_error"] is True
    assert reply.data["result"]["denied_by"] == "citation"
    assert reply.data["result"]["missing_citations"] == [FABRICATED_CITE]
    # Real ids were on offer and the fabricated one still did not pass.
    assert reply.data["result"]["retrieved_ids"]
    assert FABRICATED_CITE not in reply.data["result"]["retrieved_ids"]
    assert _reply_ticket_ids(conn) == []


async def test_citation_denial_emits_guardrail_event(conn, registry, keyword_baseline):
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"],
            "body": GROUNDED_REPLY,
            "citations": [FABRICATED_CITE],
        }, id="t2")]),
        _response([_text("Understood.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))

    guardrails = _events_of(events, "guardrail")
    assert len(guardrails) == 1
    assert guardrails[0].data["guard"] == "citation"
    assert guardrails[0].data["tool"] == "send_reply"
    assert guardrails[0].data["missing_citations"] == [FABRICATED_CITE]
    assert guardrails[0].data["action"] == "denied"
    # Cause before effect: the denial, then the result it produced.
    guard_at = events.index(guardrails[0])
    reply_at = events.index(_events_of(events, "tool_result", "send_reply")[0])
    assert guard_at < reply_at


class RecoveringFakeClient:
    """Cites a fabricated id once, then cites what the denial handed back.

    The recovery step deliberately reads `retrieved_ids` out of the denial payload
    rather than being scripted with the right answer: if the denial stops naming the
    valid ids, this client has nothing to retry with and the run dies without a
    terminal action — which is exactly the regression the wording exists to prevent.
    """

    def __init__(self, ticket_id):
        self.ticket_id = ticket_id
        self.recovered_with = None
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, *, messages, **kwargs):
        payloads = _tool_result_payloads(messages)
        if not payloads:
            return _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")])
        last = payloads[-1]
        if "results" in last:
            return _response([_tool_use("send_reply", {
                "ticket_id": self.ticket_id,
                "body": GROUNDED_REPLY,
                "citations": [FABRICATED_CITE],
            }, id="t2")])
        if last.get("denied_by") == "citation":
            self.recovered_with = list(last.get("retrieved_ids") or [])
            return _response([_tool_use("send_reply", {
                "ticket_id": self.ticket_id,
                "body": GROUNDED_REPLY,
                "citations": self.recovered_with,
            }, id="t3")])
        return _response([_text("Reply sent.")], stop_reason="end_turn")


async def test_run_recovers_after_citation_denial(conn, registry, keyword_baseline):
    # Pitfall 1: a citation denial is is_error=True, so resolved_via stays None. If the
    # model treats it as final the run ends `ended_without_action` and the eval gate
    # regresses. The denial has to be a retry instruction, and this is what proves it.
    _seed_tickets(conn, TICKET)
    client = RecoveringFakeClient(TICKET["id"])
    events = await collect(run_ticket(client, registry, TICKET))

    assert [e.data["guard"] for e in _events_of(events, "guardrail")] == ["citation"]
    assert client.recovered_with, (
        "the denial named no retrieved ids, so the model had nothing to retry with"
    )
    assert events[-1].type == "resolution"
    assert events[-1].data["via"] == "send_reply"
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


def test_the_citation_set_is_also_not_a_per_call_argument_to_forget(conn, registry):
    # Same argument as the ticket binding: the run's retrieved ids are baked into the
    # executor when the run starts, so there is no per-call keyword to omit. An empty
    # set is a real state — "this run has retrieved nothing yet" — and must deny, not
    # wave everything through the way an unbound MCP call does.
    executor = bind_to_ticket(TICKET["id"], set())
    assert list(inspect.signature(executor).parameters) == ["spec", "name", "raw_input", "policy"]
    result, is_error = executor(
        registry["send_reply"],
        "send_reply",
        {"ticket_id": TICKET["id"], "body": GROUNDED_REPLY, "citations": [FABRICATED_CITE]},
        ToolPolicy(),
    )
    assert is_error is True
    assert json.loads(result)["denied_by"] == "citation"
    assert _reply_ticket_ids(conn) == []


def test_the_unbound_path_does_not_enforce_citations(conn, registry):
    # mcp_server.py is frozen (D-03) and has no run, so it has no retrieved ids to check
    # against. Enforcing there would deny every cited reply on the MCP surface.
    _seed_tickets(conn, TICKET)
    result, is_error = _execute_guarded(
        registry["send_reply"],
        "send_reply",
        {"ticket_id": TICKET["id"], "body": GROUNDED_REPLY, "citations": [FABRICATED_CITE]},
        ToolPolicy(),
    )
    assert is_error is False, result
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


# One query, one retrieved doc: "refund policy" hits billing.md and nothing else, so
# api.md is a real KB doc this run demonstrably never saw.
BILLING_QUERY = "refund policy"
LOCATED_CITE = "billing.md#refunds"  # the id _locate_heading picks for that query
OTHER_SECTION_CITE = "billing.md#upgrades-and-downgrades"  # a heading further down
UNRETRIEVED_DOC_CITE = "api.md#rate-limits"  # a real doc, not retrieved this run


async def _cite(conn, registry, citations):
    """One run: search billing docs, then reply citing `citations`. Returns the events."""
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": BILLING_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"], "body": GROUNDED_REPLY, "citations": citations,
        }, id="t2")]),
        _response([_text("Reply sent.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))
    search = _events_of(events, "tool_result", "search_docs")[0]
    assert [r["doc"] for r in search.data["result"]["results"]] == ["billing.md"], (
        "this run must retrieve billing.md and only billing.md, or the assertions below"
        " prove nothing about which ids the guard accepts"
    )
    return events


async def test_any_heading_of_a_retrieved_doc_is_a_valid_citation(
    conn, registry, keyword_baseline
):
    # The model is handed the WHOLE file (D-01), so a heading it read out of the
    # returned text is correct grounding — better grounding, in fact, than the
    # query-derived `id`. Denying it costs a round trip and pushes the run toward
    # `ended_without_action` for behaving exactly as the prompt asks.
    events = await _cite(conn, registry, [OTHER_SECTION_CITE])
    reply = _events_of(events, "tool_result", "send_reply")[0]
    assert reply.data["is_error"] is False, reply.data["result"]
    assert _events_of(events, "guardrail") == []
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_the_bare_doc_name_is_a_valid_citation(conn, registry, keyword_baseline):
    events = await _cite(conn, registry, ["billing.md", LOCATED_CITE])
    reply = _events_of(events, "tool_result", "send_reply")[0]
    assert reply.data["is_error"] is False, reply.data["result"]
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_a_citation_differing_only_in_case_is_not_a_fabricated_source(
    conn, registry, keyword_baseline
):
    events = await _cite(conn, registry, ["  BILLING.MD#REFUNDS  "])
    reply = _events_of(events, "tool_result", "send_reply")[0]
    assert reply.data["is_error"] is False, reply.data["result"]
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_a_citation_to_a_doc_this_run_never_retrieved_is_still_denied(
    conn, registry, keyword_baseline
):
    # The other direction, and the whole point of RAG-04: widening the accept-set to
    # every anchor of a RETRIEVED doc must not turn the guard into a rubber stamp.
    # api.md exists in kb/ and has a real `rate limits` heading — it simply was not
    # returned to this run, so citing it is a hallucinated source.
    events = await _cite(conn, registry, [UNRETRIEVED_DOC_CITE])
    reply = _events_of(events, "tool_result", "send_reply")[0]
    assert reply.data["is_error"] is True
    assert reply.data["result"]["denied_by"] == "citation"
    assert reply.data["result"]["missing_citations"] == [UNRETRIEVED_DOC_CITE]
    # The denial still names something usable, or the run cannot recover.
    assert OTHER_SECTION_CITE in reply.data["result"]["retrieved_ids"]
    assert not any(i.startswith("api.md") for i in reply.data["result"]["retrieved_ids"])
    assert _reply_ticket_ids(conn) == []


def test_the_system_prompt_tells_the_model_to_cite_retrieved_ids(registry):
    # The guard above only ever fires on a model that cites at all. If the instruction
    # is dropped the guard goes quiet and looks healthy while grounding is unchecked.
    assert "citations" in SYSTEM_PROMPT
    assert "search_docs result" in SYSTEM_PROMPT
    assert "citations" in registry["send_reply"].schema["input_schema"]["properties"]


# --- retrieval degradation (RAG-05) ---


def _registry_with(conn, monkeypatch, *, matrix: bool, reason: str | None = None):
    """A registry whose index state is fixed by the test, not by `kb/index.json`.

    The committed artifact is a build output: it can legitimately be stale between a
    kb edit and a rebuild. A degradation test that reads it therefore passes for
    whichever cause happens to be true today — exactly the kind of accidental pass
    this phase's review was about. Pin the state instead.
    """
    real = retrieval.load_index

    def _fake(kb_dir):
        index = real(kb_dir)
        vectors = None
        if matrix:
            vectors = np.eye(len(index.docs), settings.voyage_dim, dtype=np.float32)
        return retrieval.Index(
            docs=index.docs,
            matrix=vectors,
            model=settings.voyage_model,
            dim=settings.voyage_dim,
            unavailable_reason=reason,
        )

    monkeypatch.setattr(retrieval, "load_index", _fake)
    return build_registry(conn, KB_DIR)


async def test_retrieval_degraded_emits_notice(conn, monkeypatch):
    # Voyage configured and failing is the case worth surfacing: the results still look
    # like results, so without this event the run silently serves keyword-quality hits.
    monkeypatch.setattr(settings, "voyage_api_key", "test-key-never-sent-anywhere")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("down"))
    )
    registry = _registry_with(conn, monkeypatch, matrix=True)
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"], "body": GROUNDED_REPLY,
        }, id="t2")]),
        _response([_text("Reply sent.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))

    search = _events_of(events, "tool_result", "search_docs")[0]
    assert search.data["result"]["degraded"] is True, "Voyage did not actually fail here"
    notices = _events_of(events, "notice")
    assert [n.data["kind"] for n in notices] == ["retrieval_degraded"]
    assert notices[0].data["tool"] == "search_docs"
    assert notices[0].data["retrieval_mode"] == "keyword"
    assert notices[0].data["cause"] == "voyage_failed"
    assert events.index(notices[0]) < events.index(search)
    # A notice, not an ending: the run still resolves on the fallback results.
    assert events[-1].type == "resolution"
    assert events[-1].data["via"] == "send_reply"
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_a_key_with_an_unusable_index_emits_the_notice_too(conn, monkeypatch):
    """CR-03 end to end: the run — not just a boot log — has to say this.

    A stale or missing `kb/index.json` on a key-configured deployment used to reach
    the stream as `degraded: false, mode: keyword`: indistinguishable from the
    deliberate keyless baseline. The cause is carried through so the notice says
    which fix applies (rebuild the artifact, not "check Voyage").
    """
    monkeypatch.setattr(settings, "voyage_api_key", "test-key-never-sent-anywhere")
    registry = _registry_with(
        conn, monkeypatch, matrix=False,
        reason="kb/*.md changed without rebuilding the index",
    )
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"], "body": GROUNDED_REPLY,
        }, id="t2")]),
        _response([_text("Reply sent.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))

    search = _events_of(events, "tool_result", "search_docs")[0]
    assert search.data["result"]["degraded"] is True
    assert search.data["result"]["degraded_cause"] == "index_unavailable"
    notices = _events_of(events, "notice")
    assert [n.data["kind"] for n in notices] == ["retrieval_degraded"]
    assert notices[0].data["cause"] == "index_unavailable"
    # Still degraded, not dead: keyword hits are real hits and the run resolves.
    assert events[-1].type == "resolution"
    assert _reply_ticket_ids(conn) == [TICKET["id"]]


async def test_keyword_baseline_emits_no_notice(conn, registry, keyword_baseline):
    # No key is the CI and local default, not a degradation — a notice on every run
    # would make the real one unreadable.
    _seed_tickets(conn, TICKET)
    client = FakeClient([
        _response([_tool_use("search_docs", {"query": RATE_LIMIT_QUERY}, id="t1")]),
        _response([_tool_use("send_reply", {
            "ticket_id": TICKET["id"], "body": GROUNDED_REPLY,
        }, id="t2")]),
        _response([_text("Reply sent.")], stop_reason="end_turn"),
    ])
    events = await collect(run_ticket(client, registry, TICKET))

    search = _events_of(events, "tool_result", "search_docs")[0]
    assert search.data["result"]["degraded"] is False
    assert _events_of(events, "notice") == []
    assert events[-1].type == "resolution"


def test_a_reply_with_no_citations_is_not_denied(conn, registry):
    # D-12: citations are optional and validation is subset — [] ⊆ retrieved always
    # passes, which is what keeps every pre-phase-3 scripted send_reply green.
    _seed_tickets(conn, TICKET)
    executor = bind_to_ticket(TICKET["id"], set())
    result, is_error = executor(
        registry["send_reply"],
        "send_reply",
        {"ticket_id": TICKET["id"], "body": GROUNDED_REPLY},
        ToolPolicy(),
    )
    assert is_error is False, result
    assert _reply_ticket_ids(conn) == [TICKET["id"]]

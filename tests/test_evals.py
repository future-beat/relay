from relay.db import SEED_CUSTOMERS
from relay.evals import extract_outcome, load_golden
from relay.models import AgentEvent, TicketCategory

SEED_EMAILS = {c[0] for c in SEED_CUSTOMERS}
VALID_ACTIONS = {"send_reply", "create_escalation"}
VALID_CATEGORIES = {c.value for c in TicketCategory}


def test_golden_dataset_is_well_formed():
    cases = load_golden()
    assert len(cases) >= 10
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for case in cases:
        assert case["customer_email"] in SEED_EMAILS, f"{case['id']}: unknown customer"
        assert case["expected_action"] in VALID_ACTIONS, f"{case['id']}: bad action"
        assert case["expected_categories"], f"{case['id']}: no acceptable categories"
        assert set(case["expected_categories"]) <= VALID_CATEGORIES, f"{case['id']}: bad category"


def test_dataset_covers_both_terminal_actions():
    actions = {c["expected_action"] for c in load_golden()}
    assert actions == VALID_ACTIONS


def test_extract_outcome_from_events():
    events = [
        AgentEvent(type="usage", data={"cost_usd": 0.01, "steps": 1, "input_tokens": 1,
                                       "output_tokens": 1, "max_cost_usd": 0.5}),
        AgentEvent(type="tool_use", data={"tool": "set_category",
                                          "input": {"ticket_id": 1, "category": "billing"}}),
        AgentEvent(type="tool_use", data={"tool": "send_reply",
                                          "input": {"ticket_id": 1, "body": "Hello Mia, ..."}}),
        AgentEvent(type="usage", data={"cost_usd": 0.03, "steps": 2, "input_tokens": 2,
                                       "output_tokens": 2, "max_cost_usd": 0.5}),
        AgentEvent(type="resolution", data={"via": "send_reply"}),
    ]
    outcome = extract_outcome(events)
    assert outcome["action"] == "send_reply"
    assert outcome["category"] == "billing"
    assert outcome["final_text"] == "Hello Mia, ..."
    assert outcome["cost_usd"] == 0.03
    assert outcome["error"] is None


def test_extract_outcome_records_error():
    events = [AgentEvent(type="error", data={"reason": "budget_exceeded"})]
    outcome = extract_outcome(events)
    assert outcome["error"] == "budget_exceeded"
    assert outcome["action"] is None

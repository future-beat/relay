import json
from dataclasses import asdict
from pathlib import Path

import pytest

from helpers import FakeClient, response, text_block, tool_use_block
from relay.config import settings
from relay.db import SEED_CUSTOMERS
from relay.evals import extract_outcome, load_golden
from relay.models import AgentEvent, TicketCategory
from relay.retrieval import load_index, slug
from relay.retrieval_eval import load_labels, mrr, recall_at_k, scored_labels

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


# --- WR-10: is the citation guard load-bearing, or has it simply never been asked? --
#
# The report used to record action/category/grounded/quality/cost/error and nothing
# about retrieval. "Zero citation denials across 12 cases" was therefore consistent
# with the model never emitting `citations` at all, in which case the guard is inert
# in production while every test passes. These record the fact that decides it.


def _search_result(payload, *, is_error=False):
    return AgentEvent(
        type="tool_result",
        data={"tool": "search_docs", "result": payload, "is_error": is_error},
    )


def _hit(doc, heading_slug, *anchors):
    return {"doc": doc, "id": f"{doc}#{heading_slug}", "anchors": [doc, *anchors]}


def test_extract_outcome_records_what_the_reply_cited():
    events = [
        _search_result({
            "results": [_hit("billing.md", "refunds", "billing.md#refunds")],
            "retrieval_mode": "semantic", "degraded": False, "degraded_cause": None,
        }),
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {
            "ticket_id": 1, "body": "Hello Mia, ...", "citations": ["billing.md#refunds"],
        }}),
        AgentEvent(type="resolution", data={"via": "send_reply"}),
    ]
    outcome = extract_outcome(events)
    assert outcome["citations"] == ["billing.md#refunds"]
    assert outcome["retrieval"]["mode"] == "semantic"
    assert outcome["retrieval"]["degraded"] is False
    # The report's whole point: this comparison is now possible from the artifact.
    assert set(outcome["citations"]) <= set(outcome["retrieval"]["retrieved_ids"])


def test_a_reply_that_cited_nothing_is_distinguishable_from_one_that_cited_well():
    """The exact ambiguity WR-10 names. Both runs produce zero guardrail events."""
    searched = _search_result({
        "results": [_hit("billing.md", "refunds", "billing.md#refunds")],
        "retrieval_mode": "keyword", "degraded": False, "degraded_cause": None,
    })
    silent = extract_outcome([
        searched,
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {
            "ticket_id": 1, "body": "Hello Mia, ...",
        }}),
    ])
    cited = extract_outcome([
        searched,
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {
            "ticket_id": 1, "body": "Hello Mia, ...", "citations": ["billing.md"],
        }}),
    ])
    # None, not [] — "never passed the argument" is the finding, and D-12 makes `[]`
    # a legitimate pass, so collapsing the two would hide exactly the case in question.
    assert silent["citations"] is None
    assert cited["citations"] == ["billing.md"]


def test_retrieved_ids_include_every_anchor_the_guard_would_accept():
    """agent.py accepts doc name, located id, and every heading of a returned file.

    A report that recorded only the located `id` would show a legitimately cited
    heading as unretrieved — inventing a violation the running system never saw.
    """
    events = [
        _search_result({
            "results": [_hit("api.md", "rate-limits", "api.md#rate-limits", "api.md#webhooks")],
            "retrieval_mode": "semantic", "degraded": False, "degraded_cause": None,
        }),
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {
            "ticket_id": 1, "body": "b" * 40, "citations": ["api.md#webhooks"],
        }}),
    ]
    outcome = extract_outcome(events)
    assert outcome["retrieval"]["retrieved_ids"] == [
        "api.md", "api.md#rate-limits", "api.md#webhooks",
    ]
    assert set(outcome["citations"]) <= set(outcome["retrieval"]["retrieved_ids"])


def test_a_fabricated_citation_is_visible_in_the_report():
    events = [
        _search_result({
            "results": [_hit("billing.md", "refunds", "billing.md#refunds")],
            "retrieval_mode": "semantic", "degraded": False, "degraded_cause": None,
        }),
        AgentEvent(type="tool_use", data={"tool": "send_reply", "input": {
            "ticket_id": 1, "body": "b" * 40, "citations": ["pricing.md#enterprise"],
        }}),
    ]
    outcome = extract_outcome(events)
    assert not set(outcome["citations"]) <= set(outcome["retrieval"]["retrieved_ids"])


def test_one_degraded_search_is_not_erased_by_a_later_healthy_one():
    events = [
        _search_result({
            "results": [], "retrieval_mode": "keyword",
            "degraded": True, "degraded_cause": "voyage_failed",
        }),
        _search_result({
            "results": [_hit("billing.md", "refunds")], "retrieval_mode": "semantic",
            "degraded": False, "degraded_cause": None,
        }),
    ]
    outcome = extract_outcome(events)
    assert outcome["retrieval"]["degraded"] is True
    assert outcome["retrieval"]["mode"] == "semantic"


def test_a_failed_search_contributes_no_retrieved_ids():
    events = [_search_result({"results": [_hit("billing.md", "refunds")]}, is_error=True)]
    assert extract_outcome(events)["retrieval"]["retrieved_ids"] == []


def test_a_run_that_never_searched_reports_that_plainly():
    outcome = extract_outcome([AgentEvent(type="resolution", data={"via": "send_reply"})])
    assert outcome["retrieval"] == {"mode": None, "degraded": False, "retrieved_ids": []}


async def test_the_report_artifact_carries_citations_and_retrieval(monkeypatch):
    """End-to-end through run_case: a field extract_outcome computes but the artifact
    drops is worth nothing, and asdict() only serialises declared CaseResult fields."""
    from relay import evals
    from relay.config import settings

    monkeypatch.setattr(settings, "voyage_api_key", None)  # keyword mode, no network

    async def _fake_judge(*args, **kwargs):
        return {"grounded": True, "invented_claims": [], "quality": 5, "reasoning": "ok"}

    monkeypatch.setattr(evals, "judge_grounding", _fake_judge)
    client = FakeClient([
        response([tool_use_block("search_docs", {"query": "refund policy"})]),
        response([tool_use_block(
            "send_reply",
            {"ticket_id": 1, "body": "Here is what our refund policy says. " * 2,
             "citations": ["billing.md"]},
            id="toolu_2",
        )]),
        response([text_block("done")], stop_reason="end_turn"),
    ])
    case = {
        "id": "refund-window", "customer_email": next(iter(SEED_EMAILS)),
        "subject": "Refund", "body": "Can I get a refund?",
        "expected_action": "send_reply", "expected_categories": ["billing"],
    }
    result = asdict(await evals.run_case(client, case, kb_text="(kb)"))
    assert result["citations"] == ["billing.md"]
    assert "billing.md" in result["retrieval"]["retrieved_ids"]
    assert result["retrieval"]["mode"] == "keyword"


# --- EVAL-01: labeled retrieval set + recall/MRR (report-only, D-03) -------------
# All three pin keyword mode: with no key retrieve() cannot reach Voyage, so the
# free suite bills nothing and conftest's _no_outbound_http guard never fires.


@pytest.fixture()
def keyword_baseline(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", None)


def _index_ids() -> set[str]:
    """Every id kb/index.json actually licenses — doc names and doc#slug anchors."""
    raw = json.loads(Path("kb/index.json").read_text())
    ids: set[str] = set()
    for doc in raw["docs"]:
        ids.add(doc["doc"])
        ids.update(f"{doc['doc']}#{slug(h)}" for h in doc["headings"])
    return ids


def test_retrieval_labels_well_formed():
    # mutation: point any `relevant` id at a doc/anchor absent from kb/index.json
    # (e.g. "billing.md#store-credit"), or delete the `rid in known` assertion.
    labels = load_labels()
    known = _index_ids()
    assert len(labels) >= 12
    ids = [row["id"] for row in labels]
    assert len(ids) == len(set(ids)), "duplicate label ids"
    assert any(row["relevant"] == [] for row in labels), "missing the empty-relevant negative"
    for row in labels:
        assert row["query"].strip(), f"{row['id']}: empty query"
        assert isinstance(row["relevant"], list), f"{row['id']}: relevant is not a list"
        for rid in row["relevant"]:
            assert rid in known, f"{row['id']}: {rid} is not in kb/index.json"


def test_recall_and_mrr_over_labeled_set(keyword_baseline):
    # mutation: stub relay.retrieval_eval.retrieve to return ([], "keyword", False, None)
    # — recall@1 over the exact-match row drops to 0.0 and this fails.
    index = load_index(Path("kb"))
    labels = load_labels()
    r1 = recall_at_k(index, labels, 1)
    r3 = recall_at_k(index, labels, 3)
    reciprocal = mrr(index, labels)
    assert 0.0 <= r1 <= 1.0
    assert 0.0 <= r3 <= 1.0
    assert 0.0 <= reciprocal <= 1.0
    assert r3 >= r1, "recall@3 cannot be below recall@1"

    # A query whose relevant doc keyword-matches must rank first. This is the
    # assertion that breaks if the metric stops consulting the shipped retriever.
    exact = [row for row in labels if row["id"] == "password-reset"]
    assert len(exact) == 1
    assert recall_at_k(index, exact, 1) == 1.0

    # The `relevant: []` negative is excluded from the denominator — N-1, not N.
    # One guaranteed hit plus one negative must read 1.0; counting the negative
    # would read 0.5 and silently understate every reported number.
    negative = [row for row in labels if row["relevant"] == []]
    assert len(negative) == 1
    mixed = exact + negative
    assert len(scored_labels(mixed)) == len(mixed) - 1
    assert recall_at_k(index, mixed, 3) == 1.0
    assert mrr(index, mixed) == 1.0


def test_soft_floor_recall3_positive(keyword_baseline):
    # mutation: make recall_at_k return 0.0 (dead retrieval) — this fails.
    # Soft floor only (D-03): a wiring tripwire, never a numeric quality gate.
    index = load_index(Path("kb"))
    assert recall_at_k(index, load_labels(), 3) > 0

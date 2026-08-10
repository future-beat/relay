import inspect
import json
from pathlib import Path

import pytest

from relay import retrieval
from relay.config import settings
from relay.guardrails import SendReplyInput, ToolInputError, validate_tool_input
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture(autouse=True)
def _keyword_baseline(monkeypatch):
    # No key => retrieve() stays on the keyword scorer. These tests assert the tool's
    # envelope and the phase-1 ranking baseline, not Voyage's ordering, and must never
    # reach the network just because a developer happens to have VOYAGE_API_KEY set.
    monkeypatch.setattr(settings, "voyage_api_key", None)


def test_lookup_customer_found(registry):
    result = json.loads(registry["lookup_customer"].execute(email="ava@acmecorp.com"))
    assert result["found"] is True
    assert result["customer"]["plan"] == "enterprise"


def test_lookup_customer_missing(registry):
    result = json.loads(registry["lookup_customer"].execute(email="nobody@nowhere.com"))
    assert result["found"] is False


def test_search_docs_grounds_billing_questions(registry):
    result = json.loads(registry["search_docs"].execute(query="refund policy"))
    docs = [r["doc"] for r in result["results"]]
    assert "billing.md" in docs


def test_search_docs_results_carry_the_citation_shape(registry):
    result = json.loads(registry["search_docs"].execute(query="refund policy"))
    assert result["results"], "no hit to inspect — the assertions below would be vacuous"
    for hit in result["results"]:
        assert set(hit) == {"doc", "heading", "id", "anchors", "text", "score"}
        assert hit["id"].startswith(hit["doc"])
        # Every heading of the whole file the model was just handed, plus the bare
        # doc name — the ids a citation of this result may legitimately use.
        assert hit["anchors"][0] == hit["doc"]
        assert hit["id"] in hit["anchors"]
    assert result["retrieval_mode"] == "keyword"
    assert result["degraded"] is False


def test_search_docs_returns_whole_files_never_chunks(registry):
    # D-01/D-02: the envelope stays byte-compatible with phase 1 — only ranking changed.
    result = json.loads(registry["search_docs"].execute(query="refund policy"))
    hit = next(r for r in result["results"] if r["doc"] == "billing.md")
    assert hit["text"] == (KB_DIR / "billing.md").read_text(encoding="utf-8")


def test_search_docs_no_match(registry):
    result = json.loads(registry["search_docs"].execute(query="zzzzz qqqqq"))
    # Empty results are the escalation signal (D-03) — never a fabricated best guess.
    assert result["results"] == []
    assert result["retrieval_mode"] == "keyword"


def test_search_docs_reads_the_index_once_not_per_call(conn, monkeypatch):
    # The closure captures the loaded matrix; a per-call load_index would put a disk
    # read (and a re-parse of every embedding) inside every tool call.
    calls: list[Path] = []
    real_load_index = retrieval.load_index

    def counting_load_index(kb_dir):
        calls.append(kb_dir)
        return real_load_index(kb_dir)

    monkeypatch.setattr(retrieval, "load_index", counting_load_index)
    built = build_registry(conn, KB_DIR)
    for _ in range(3):
        built["search_docs"].execute(query="refund policy")

    assert len(calls) == 1


def test_search_docs_stays_synchronous(registry):
    # It runs inside phase 2's asyncio.to_thread seam: a coroutine function here would
    # hand the model an un-awaited coroutine object.
    assert not inspect.iscoroutinefunction(registry["search_docs"].execute)


def test_escalation_marks_ticket(conn, registry):
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", "Refund", "Please refund me"),
    )
    ticket_id = cur.lastrowid
    result = json.loads(
        registry["create_escalation"].execute(
            ticket_id=ticket_id, reason="Refund requires human billing agent", priority="high"
        )
    )
    assert result["status"] == "escalated"
    status = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()[0]
    assert status == "escalated"


def test_send_reply_resolves_ticket(conn, registry):
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("liam@brightco.io", "API limits", "What are the rate limits?"),
    )
    ticket_id = cur.lastrowid
    result = json.loads(
        registry["send_reply"].execute(ticket_id=ticket_id, body="Pro allows 600 req/min.")
    )
    assert result["status"] == "resolved"


def test_send_reply_still_resolves_without_citations(conn, registry):
    # D-12: citations is optional, so every pre-phase-3 scripted call keeps working.
    # This is the back-compat contract the other six test files depend on implicitly.
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("liam@brightco.io", "API limits", "What are the rate limits?"),
    )
    ticket_id = cur.lastrowid
    validated = validate_tool_input(
        SendReplyInput, {"ticket_id": ticket_id, "body": "Pro allows 600 requests a minute."}
    )
    assert validated["citations"] == []

    result = json.loads(registry["send_reply"].execute(**validated))
    assert result["status"] == "resolved"
    status = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()[0]
    assert status == "resolved"


def test_send_reply_accepts_citations(conn, registry):
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", "Refund", "Can I get a refund?"),
    )
    ticket_id = cur.lastrowid
    validated = validate_tool_input(
        SendReplyInput,
        {
            "ticket_id": ticket_id,
            "body": "Refunds are available within 30 days of purchase.",
            "citations": ["billing.md#refunds"],
        },
    )
    assert validated["citations"] == ["billing.md#refunds"]

    result = json.loads(registry["send_reply"].execute(**validated))
    assert result["status"] == "resolved"


def test_send_reply_rejects_non_string_citations():
    with pytest.raises(ToolInputError, match="citations"):
        validate_tool_input(
            SendReplyInput,
            {"ticket_id": 1, "body": "A long enough grounded reply.", "citations": "billing.md"},
        )


def test_send_reply_schema_declares_citations_optional(registry):
    schema = registry["send_reply"].schema["input_schema"]
    assert schema["properties"]["citations"]["type"] == "array"
    assert schema["properties"]["citations"]["items"]["type"] == "string"
    # Not required: forcing a citation would break every citation-less call site and
    # sharpen the ended_without_action eval trap (D-12).
    assert schema["required"] == ["ticket_id", "body"]


def test_all_tools_declare_permission_tier(registry):
    assert {spec.tier for spec in registry.values()} == {"read", "write"}
    assert registry["send_reply"].tier == "write"
    assert registry["lookup_customer"].tier == "read"

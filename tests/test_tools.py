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


# Someone else's typed words, filed against the same address — the shape the Try-it form
# actually produces, where every visitor picks a seeded customer and edits the subject.
# Improbable on purpose: an absence assertion over a plausible string says nothing.
OTHER_VISITORS_SUBJECT = "SOMEONE-ELSES-TYPED-SUBJECT-8c41"


def test_lookup_customer_returns_no_free_text(conn, registry):
    """The payload carries no field a stranger can write into (the structural fix).

    This is the whole reason the tool changed: `recent_tickets[].subject` was the only
    free-text field `lookup_customer` returned, it is visitor-typed, and everything this
    tool returns reaches the model's context — hence the model's prose, hence the keyless
    /runs/{uid}. The mask downstream filters LITERALS; it cannot see a paraphrase. So the
    property has to be that the words are never in the context, not that they are masked
    on the way out.

    Asserted two ways, because they fail differently: by VALUE (the seeded subject is
    absent from the serialised payload) and by SHAPE (the exact key set of each record,
    so a future migration that adds a `body` or a `notes` column to `tickets` reds here
    rather than shipping a new disclosure with no test failure). The value assertion
    alone would pass against a payload that grew a different free-text column; the shape
    assertion alone would pass against a `subject` renamed to `title`.

    MUTATION: restore `subject` to the SELECT in `lookup_customer`
    (`SELECT id, subject, status, created_at FROM tickets ...`). Both halves red.

    Anti-vacuity: the ticket really is in the address's history — `recent_tickets` is
    non-empty and its id is the row just inserted — so "no subject in the payload" is a
    claim about what the tool returns, not about a lookup that found nothing.
    """
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("ava@acmecorp.com", OTHER_VISITORS_SUBJECT, "filed by a previous demo visitor"),
    )
    conn.commit()

    raw = registry["lookup_customer"].execute(email="ava@acmecorp.com")
    result = json.loads(raw)

    assert result["found"] is True
    assert result["recent_tickets"], "the address has no history — the assertions are vacuous"
    assert cur.lastrowid in [t["id"] for t in result["recent_tickets"]], (
        "the ticket carrying the subject is not in the returned history — vacuous"
    )

    assert OTHER_VISITORS_SUBJECT not in raw, (
        "lookup_customer hands another visitor's typed words to the model"
    )
    for ticket in result["recent_tickets"]:
        assert set(ticket) == {"id", "status", "created_at"}, ticket
    # The customer row too: named columns, none of them free text (four fictional
    # personas hardcoded in a public repo — enumerable constants, not secrets).
    assert set(result["customer"]) == {"email", "name", "plan", "signed_up"}


def test_lookup_customer_keeps_the_escalation_signal(conn, registry):
    """...and dropping the text kept what the prompt actually reads history FOR.

    prompts.py uses history for one decision — escalate when the customer is frustrated
    or at risk of leaving — and that signal is how many tickets, how recent, how many
    unresolved. If the payload had been reduced to a bare count, or the statuses dropped
    along with the subjects, the tool would still pass the leak test above while the
    escalation cases quietly lost their input. So this pins the signal, not the shape.

    MUTATION: drop `status` (or `created_at`) from the SELECT — reds here while
    test_lookup_customer_returns_no_free_text stays green, which is the point of having
    both.
    """
    for subject, status in (
        ("first complaint", "escalated"),
        ("second complaint", "open"),
        ("third complaint", "open"),
    ):
        conn.execute(
            "INSERT INTO tickets (customer_email, subject, body, status)"
            " VALUES (?, ?, ?, ?)",
            ("ava@acmecorp.com", subject, "body", status),
        )
    conn.commit()

    result = json.loads(registry["lookup_customer"].execute(email="ava@acmecorp.com"))
    tickets = result["recent_tickets"]

    assert len(tickets) == 3, "the count — how many times they have written in"
    assert sum(t["status"] == "open" for t in tickets) == 2, "how many are unresolved"
    assert all(t["created_at"] for t in tickets), "how recent each one is"


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
    # A question a real model would actually emit, not `zzzzz qqqqq` — gibberish
    # passed this for the wrong reason while every natural phrasing of the same
    # uncovered ask returned the whole KB (CR-04).
    result = json.loads(registry["search_docs"].execute(query="Do you integrate with Salesforce?"))
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

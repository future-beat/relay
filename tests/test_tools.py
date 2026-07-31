import json


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


def test_search_docs_no_match(registry):
    result = json.loads(registry["search_docs"].execute(query="zzzzz qqqqq"))
    assert result["results"] == []


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


def test_all_tools_declare_permission_tier(registry):
    assert {spec.tier for spec in registry.values()} == {"read", "write"}
    assert registry["send_reply"].tier == "write"
    assert registry["lookup_customer"].tier == "read"

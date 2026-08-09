def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_create_and_fetch_ticket(client):
    created = client.post(
        "/tickets",
        json={
            "customer_email": "ava@acmecorp.com",
            "subject": "Cannot log in",
            "body": "SSO redirect loops forever.",
        },
    )
    assert created.status_code == 201
    ticket = created.json()
    assert ticket["status"] == "open"

    fetched = client.get(f"/tickets/{ticket['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["subject"] == "Cannot log in"


def test_get_missing_ticket_404(client):
    assert client.get("/tickets/9999").status_code == 404

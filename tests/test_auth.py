import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from relay.auth import require_tier, resolve_tier
from relay.config import PUBLISHED_DEMO_KEY, Settings, settings

_REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-owner-key")
    monkeypatch.setattr(settings, "demo_key", "test-demo-key")


@pytest.fixture()
def no_keys(monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "demo_key", None)


# --- tier resolution ---


def test_owner_key_resolves_to_owner_tier(keys):
    assert resolve_tier("test-owner-key") == "owner"


def test_demo_key_resolves_to_demo_tier(keys):
    assert resolve_tier("test-demo-key") == "demo"


def test_unknown_key_resolves_to_none(keys):
    assert resolve_tier("nope") is None


def test_missing_and_empty_keys_resolve_to_none(keys):
    assert resolve_tier(None) is None
    assert resolve_tier("") is None


def test_non_ascii_key_is_rejected_cleanly(keys):
    # secrets.compare_digest raises TypeError on non-ASCII str — comparing bytes must not.
    assert resolve_tier("kéy") is None


def test_unconfigured_tier_never_matches(monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "demo_key", "test-demo-key")
    assert resolve_tier("") is None
    assert resolve_tier("test-demo-key") == "demo"


# --- dependency gating ---


def test_valid_key_returns_its_tier(keys):
    assert require_tier("owner", "demo")(presented="test-owner-key") == "owner"
    assert require_tier("owner", "demo")(presented="test-demo-key") == "demo"


def test_missing_key_is_401_with_challenge_header(keys):
    with pytest.raises(HTTPException) as exc:
        require_tier("owner", "demo")(presented=None)
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == "APIKey"


def test_unknown_key_is_401(keys):
    with pytest.raises(HTTPException) as exc:
        require_tier("owner", "demo")(presented="nope")
    assert exc.value.status_code == 401


def test_non_ascii_key_is_401_not_500(keys):
    with pytest.raises(HTTPException) as exc:
        require_tier("owner", "demo")(presented="kéy")
    assert exc.value.status_code == 401


def test_wrong_tier_is_403(keys):
    with pytest.raises(HTTPException) as exc:
        require_tier("owner")(presented="test-demo-key")
    assert exc.value.status_code == 403


def test_unconfigured_deployment_fails_closed_with_503(no_keys):
    for presented in (None, "anything", "test-owner-key"):
        with pytest.raises(HTTPException) as exc:
            require_tier("owner", "demo")(presented=presented)
        assert exc.value.status_code == 503


# --- routes ---


TICKET = {
    "customer_email": "ava@acmecorp.com",
    "subject": "Cannot log in",
    "body": "SSO redirect loops forever.",
}


@contextmanager
def without_key(client):
    """Drop the shared fixture's credential for one block.

    The fixture puts X-API-Key on the client's default headers, so an
    unauthenticated request has to remove it, not merely omit it.
    """
    saved = client.headers.pop("X-API-Key")
    try:
        yield client
    finally:
        client.headers["X-API-Key"] = saved


def test_missing_key_returns_401_with_challenge(client):
    with without_key(client) as anon:
        resp = anon.post("/tickets", json=TICKET)
    assert resp.status_code == 401
    # SEC-01 names the header explicitly: a 401 without a challenge is the exact
    # RFC violation the requirement exists to prevent.
    assert resp.headers["WWW-Authenticate"] == "APIKey"


def test_invalid_key_401(client):
    resp = client.post("/tickets", json=TICKET, headers={"X-API-Key": "not-a-real-key"})
    assert resp.status_code == 401


def test_repeated_wrong_keys_are_rate_limited(client, monkeypatch):
    # SEC-01/SEC-02: a 401 raised from a sub-dependency used to short-circuit the
    # limiter, so key guessing cost the attacker nothing. The anon bucket is charged
    # before the credential resolves, which is the only thing that bounds the rate.
    monkeypatch.setattr(settings, "anon_auth_limit", "3/minute")
    bad = {"X-API-Key": "not-a-real-key"}
    for _ in range(3):
        assert client.post("/tickets", json=TICKET, headers=bad).status_code == 401
    resp = client.post("/tickets", json=TICKET, headers=bad)
    assert resp.status_code == 429
    assert resp.json()["detail"]["tier"] == "anon"


def test_the_anon_meter_is_charged_across_every_protected_route(client, monkeypatch):
    # One bucket per IP, not per route: otherwise an attacker just rotates endpoints.
    monkeypatch.setattr(settings, "anon_auth_limit", "2/minute")
    bad = {"X-API-Key": "not-a-real-key"}
    assert client.post("/tickets", json=TICKET, headers=bad).status_code == 401
    assert client.get("/tickets/1", headers=bad).status_code == 401
    assert client.post("/tickets/1/process", headers=bad).status_code == 429


def test_public_routes_are_not_charged_the_anon_meter(client, monkeypatch):
    # /health backs the container HEALTHCHECK and the CI smoke job — it must never
    # be throttleable by anyone hammering the protected surface.
    monkeypatch.setattr(settings, "anon_auth_limit", "1/minute")
    with without_key(client) as anon:
        assert anon.post("/tickets", json=TICKET).status_code == 401
        for _ in range(5):
            assert anon.get("/health").status_code == 200
        assert anon.get("/metrics").status_code == 200


def test_valid_key_allows(client):
    assert client.post("/tickets", json=TICKET).status_code == 201


def test_public_routes_need_no_key(client):
    with without_key(client) as anon:
        assert anon.get("/health").status_code == 200
        assert anon.get("/metrics").status_code == 200
        assert anon.get("/dashboard").status_code == 200
        assert anon.get("/", follow_redirects=False).status_code in (302, 307)


def test_unauthenticated_get_ticket_401(client):
    # Complement to test_api.py::test_get_missing_ticket_404 — the dependency
    # resolves before the handler, so a missing ticket is 401 before it is 404.
    with without_key(client) as anon:
        assert anon.get("/tickets/9999").status_code == 401


def test_process_requires_key(client):
    ticket_id = client.post("/tickets", json=TICKET).json()["id"]
    with without_key(client) as anon:
        # Rejected by the gate, so this never reaches the agent or the model.
        assert anon.post(f"/tickets/{ticket_id}/process").status_code == 401


def test_demo_key_is_accepted(client):
    resp = client.post("/tickets", json=TICKET, headers={"X-API-Key": "test-demo-key"})
    assert resp.status_code == 201


def test_auth_not_configured_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "demo_key", None)
    assert client.post("/tickets", json=TICKET).status_code == 503
    # The pairing is the point: failing closed must not take /health down with it,
    # or the container HEALTHCHECK and the CI smoke job die alongside auth.
    assert client.get("/health").status_code == 200


# --- published demo key (D-02 / SEC-06) ---


def test_dashboard_publishes_the_demo_key(client):
    with without_key(client) as anon:
        resp = anon.get("/dashboard")
    assert resp.status_code == 200
    assert settings.demo_key in resp.text


def test_dashboard_demo_key_is_sourced_not_hardcoded(client, monkeypatch):
    # The point of D-02 is one source of truth. If the dashboard rendered a
    # literal, it would eventually advertise a key the service rejects — so the
    # test moves the setting and requires the page to follow it.
    monkeypatch.setattr(settings, "demo_key", "sentinel-key-4f21b9")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "sentinel-key-4f21b9" in resp.text
    assert "test-demo-key" not in resp.text


def test_dashboard_without_a_demo_key_does_not_render_none(client, monkeypatch):
    # /dashboard is the public landing surface: an unconfigured deployment must
    # still serve it, and must not print the string "None" as if it were a key.
    monkeypatch.setattr(settings, "demo_key", None)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert ">None<" not in resp.text
    assert "None" not in resp.text


def test_the_published_demo_key_agrees_across_every_file_that_names_it():
    # WR-06. D-02 specifies one published key with one source of truth, but the value
    # lived as a bare literal in the README that nothing looked at, while demo.sh and
    # .env.example carried a different one. The concrete failure: a visitor runs
    # ./scripts/demo.sh against the hosted demo, gets 401 on both calls, and
    # `set -euo pipefail` lands it as a JSON parse crash rather than a readable error
    # — the "try it yourself" moment that is the entire reason for publishing a key.
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    demo_sh = (_REPO_ROOT / "scripts" / "demo.sh").read_text(encoding="utf-8")

    assert f'X-API-Key: {PUBLISHED_DEMO_KEY}"' in readme, (
        "README's curl example does not use the published demo key"
    )
    assert f"RELAY_DEMO_KEY={PUBLISHED_DEMO_KEY}" in demo_sh, (
        "scripts/demo.sh suggests a different key than the README publishes"
    )
    # No second literal anywhere: a stale one is exactly how these drifted apart, and
    # it reads as authoritative to whoever finds it first.
    stale = re.findall(r"(?:X-API-Key: |RELAY_DEMO_KEY=)([A-Za-z0-9._-]{4,})", readme + demo_sh)
    assert set(stale) <= {PUBLISHED_DEMO_KEY}, f"a demo key literal disagrees: {set(stale)}"


def test_the_published_key_is_not_a_default_the_service_would_accept(monkeypatch):
    # Publishing the value must not mean an unconfigured deployment honours it. Auth
    # fails closed on purpose; a default on the setting would quietly reopen it on
    # every machine that boots without `fly secrets set RELAY_DEMO_KEY=...`.
    assert Settings.model_fields["demo_key"].default is None

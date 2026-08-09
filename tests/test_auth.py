import pytest
from fastapi import HTTPException

from relay.auth import require_tier, resolve_tier
from relay.config import settings


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

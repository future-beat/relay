"""Credential fields must not render through the settings object's repr.

The concrete failure this covers: pydantic renders field values in __repr__/__str__,
so any incidental repr of `settings` — a pytest assertion failure message being the
one actually observed — printed ANTHROPIC_API_KEY and VOYAGE_API_KEY in plaintext.
CI runs the suite with ANTHROPIC_API_KEY in the environment and keeps build logs, so
that is a credential in an archived artifact.

Every value here is a fake sentinel set on the object by the test. A real key is never
read from .env and never printed into test output — reproducing the leak to prove it
is closed would be the same disclosure as the bug.
"""

import pytest

from relay.config import Settings, settings

# Distinct per field so a failure names which one leaked, and shaped like nothing that
# could match another substring of the repr.
FAKE = {
    "anthropic_api_key": "fake-anthropic-4f21b9",
    "api_key": "fake-owner-91c4de",
    "demo_key": "fake-demo-7ab30c",
    "voyage_api_key": "fake-voyage-2d68e1",
}


@pytest.fixture()
def fake_credentials(monkeypatch):
    """Fakes on the live singleton — the object whose repr leaked — then restored."""
    for field, value in FAKE.items():
        monkeypatch.setattr(settings, field, value)


@pytest.mark.parametrize("field", sorted(FAKE))
def test_repr_of_settings_does_not_render_a_credential(fake_credentials, field):
    assert FAKE[field] not in repr(settings), f"{field} leaks through repr(settings)"


@pytest.mark.parametrize("field", sorted(FAKE))
def test_str_of_settings_does_not_render_a_credential(fake_credentials, field):
    # Pydantic builds __str__ from the same __repr_args__, so this is not a duplicate
    # of the above by construction — it is the f-string/print path, which is how a
    # value most plausibly reaches a log line rather than an assertion message.
    assert FAKE[field] not in str(settings), f"{field} leaks through str(settings)"


def test_a_repr_of_settings_still_carries_the_non_secret_configuration(fake_credentials):
    # The point is to hide four fields, not to blind the object: a repr that showed
    # nothing would push whoever is debugging config back to reading .env by hand.
    rendered = repr(settings)
    assert "model=" in rendered
    assert "retrieval_floor=" in rendered


@pytest.mark.parametrize("field", sorted(FAKE))
def test_every_credential_field_is_declared_repr_false(field):
    # The behavioural assertions above are the contract; this one names the mechanism,
    # so a field re-declared as a bare `str | None = None` fails as itself rather than
    # as a mystery substring match.
    assert Settings.model_fields[field].repr is False, (
        f"{field} is a credential and must be declared Field(..., repr=False)"
    )


def test_a_new_credential_shaped_field_has_to_opt_in_or_opt_out_here():
    # Guards the list above from silently going stale: a fifth `*_key` field added
    # later is either a credential (add it to FAKE) or is not (name it otherwise).
    key_fields = {name for name in Settings.model_fields if name.endswith("_key")}
    assert key_fields == set(FAKE), (
        f"credential-shaped fields changed: {key_fields ^ set(FAKE)}"
    )

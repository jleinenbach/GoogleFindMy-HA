# tests/test_diagnostics_secret_bundle.py
"""The credential bundle must never reach a diagnostics download.

`entry.data` carries the whole `secrets.json` bundle between the config flow and
the migration in `async_setup_entry`. If setup fails in between — which is
exactly when a user downloads diagnostics and pastes them into a public issue —
the bundle is still there, and `async_get_config_entry_diagnostics` copies
`dict(entry.data)` into its payload.
"""

from __future__ import annotations

from custom_components.googlefindmy.const import DATA_SECRET_BUNDLE
from custom_components.googlefindmy.diagnostics import (
    TO_REDACT,
    TO_REDACT_PREFIXES,
    async_redact_data,
)
from custom_components.googlefindmy.redaction import REDACTED

_SECRET = "0123456789abcdef" * 4


def _redact(payload: dict[str, object]) -> dict[str, object]:
    return async_redact_data(payload, TO_REDACT, TO_REDACT_PREFIXES)


def test_the_bundle_is_redacted_as_a_mapping() -> None:
    payload = {DATA_SECRET_BUNDLE: {"aas_token": _SECRET, "shared_key": _SECRET}}

    assert _redact(payload)[DATA_SECRET_BUNDLE] == REDACTED


def test_the_bundle_is_redacted_as_a_json_string() -> None:
    """A legacy bundle arrives as a string, where key redaction cannot reach in."""

    payload = {DATA_SECRET_BUNDLE: '{"aas_token": "' + _SECRET + '"}'}

    redacted = _redact(payload)

    assert redacted[DATA_SECRET_BUNDLE] == REDACTED
    assert _SECRET not in str(redacted)


def test_the_two_keys_that_decrypt_locations_are_redacted() -> None:
    payload = {"shared_key": _SECRET, "owner_key": _SECRET, "scanned_data": _SECRET}

    redacted = _redact(payload)

    assert all(value == REDACTED for value in redacted.values())


def test_the_account_address_does_not_survive_in_the_key_name() -> None:
    """Redacting the value leaves the address standing in the property name.

    These names are built as `<what>_<e-mail>`, and the diagnostics file is the
    one people attach to public issues, so the address has to go from the name
    as well. The numbering is shared across nesting levels, so a reader can
    still tell which entries belong to the same account.
    """

    payload = {
        # The address is in the payload, as it is in a real diagnostics file
        # (`entry.data` carries the account e-mail), so the key name can be
        # cleaned exactly instead of by guessing.
        "username": "user@example.com",
        "adm_token_user@example.com": _SECRET,
        "aas_token_issued_at_user@example.com": 1,
        "nested": {"spot_token_other@example.org": _SECRET},
    }

    redacted = _redact(payload)

    assert "example.com" not in str(redacted)
    assert "example.org" not in str(redacted)
    assert "adm_token_<account-1>" in redacted
    # The same account keeps the same number, and `issued_at` survives.
    assert "aas_token_issued_at_<account-1>" in redacted
    # `other@example.org` appears nowhere as a value, so the greedy fallback
    # takes the whole key: unreadable, but nothing leaks.
    assert "<account-2>" in redacted["nested"]


def test_an_underscored_local_part_leaves_no_fragment() -> None:
    """`john_doe@example.com` must not become `adm_token_john_<account-1>`.

    Guessing where the name ends and the address begins is what produced that
    fragment. The address is taken from the payload's own values instead, and
    anything still address-shaped afterwards is removed greedily, even at the
    cost of an unreadable key.
    """

    payload = {
        "username": "john_doe@example.com",
        "adm_token_john_doe@example.com": _SECRET,
        "aas_token_issued_at_john_doe@example.com": 1,
        # No matching value anywhere: the greedy fallback has to take it.
        "spot_token_jane_roe@example.org": _SECRET,
    }

    redacted = _redact(payload)

    text = str(redacted)
    for fragment in ("john", "doe", "jane", "roe", "example.com", "example.org"):
        assert fragment not in text, fragment
    # The readable part survives where the address was known.
    assert "aas_token_issued_at_<account-1>" in redacted


def test_run_time_key_names_are_redacted_by_prefix() -> None:
    """These names are built from the account e-mail and cannot be listed."""

    payload = {
        "adm_token_user@example.com": _SECRET,
        "spot_token_user@example.com": _SECRET,
        "android_id_user@example.com": _SECRET,
        "aas_token_issued_at_user@example.com": 1,
        "owner_key_user@example.com": _SECRET,
        "shared_key_user@example.com": _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_harmless_keys_survive() -> None:
    """A diagnostics file that redacts everything is useless."""

    payload = {"poll_interval": 300, "enable_stats_entities": True, "nested": {"a": 1}}

    assert _redact(payload) == payload


def test_fcm_credential_material_is_redacted_by_name() -> None:
    """These are fixed key names, so they are matched exactly, not by prefix."""

    payload = {
        "fcm_credentials": {"gcm": {"security_token": _SECRET}},
        "fcm_creds": {"gcm": {"security_token": _SECRET}},
        "fcm_installation": _SECRET,
        "fcm_registration": _SECRET,
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_fcm_health_fields_survive() -> None:
    """The push diagnostics are the reason people download this file.

    A blanket ``fcm_`` prefix rule would blank the receiver state, the status
    snapshot and the counters this module builds itself, which is precisely the
    information needed to explain a push failure.
    """

    payload = {
        "fcm_receiver_state": "connected",
        "fcm_status": {"connected": True, "last_error": None},
        "fcm_lock_contention_count": 3,
        "fcm_acquisition_duration_seconds": 1.25,
        "fcm_push_enabled": True,
    }

    assert _redact(payload) == payload

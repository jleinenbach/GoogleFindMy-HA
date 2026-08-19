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


def test_run_time_key_names_are_redacted_by_prefix() -> None:
    """These names are built from the account e-mail and cannot be listed."""

    payload = {
        "adm_token_user@example.com": _SECRET,
        "spot_token_user@example.com": _SECRET,
        "android_id_user@example.com": _SECRET,
        "aas_token_issued_at_user@example.com": 1,
        "owner_key_user@example.com": _SECRET,
        "shared_key_user@example.com": _SECRET,
        "fcm_credentials": {"gcm": {"security_token": _SECRET}},
    }

    redacted = _redact(payload)

    assert _SECRET not in str(redacted)
    assert all(value == REDACTED for value in redacted.values())


def test_harmless_keys_survive() -> None:
    """A diagnostics file that redacts everything is useless."""

    payload = {"poll_interval": 300, "enable_stats_entities": True, "nested": {"a": 1}}

    assert _redact(payload) == payload

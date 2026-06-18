# tests/test_config_flow_secrets_normalization.py
"""Tests for whitespace normalization of pasted secrets.json bundles.

Copy/paste of a ``secrets.json`` into the config flow can inject stray
whitespace into credential values. A single space inside ``owner_key`` breaks
AES-GCM owner-key decryption ("Owner key decryption failed"), and the user had
to delete that space manually. ``normalize_secrets_bundle`` removes whitespace
from credential values while preserving interior spaces in free-text fields.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
)
from custom_components.googlefindmy.shared_helpers import normalize_secrets_bundle
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AABBCC", "AABBCC"),  # already clean -> unchanged
        (" AABBCC", "AABBCC"),  # leading space
        ("AABBCC ", "AABBCC"),  # trailing space
        ("AA BB CC", "AABBCC"),  # interior spaces (wrapped paste)
        ("AA\nBB\tCC", "AABBCC"),  # interior newline/tab
        ("  AA BB  ", "AABB"),  # combined leading/trailing/interior
    ],
)
def test_credential_value_all_whitespace_removed(raw: str, expected: str) -> None:
    """Whitelisted credential keys lose ALL whitespace, including interior."""
    assert normalize_secrets_bundle({"owner_key": raw})["owner_key"] == expected
    assert normalize_secrets_bundle({"shared_key": raw})["shared_key"] == expected


def test_non_credential_value_only_edge_trimmed() -> None:
    """Free-text fields keep interior spaces and are only edge-trimmed."""
    result = normalize_secrets_bundle(
        {"username": "  Jane Doe  ", "Email": " a b@x.io "}
    )
    assert result["username"] == "Jane Doe"
    assert result["Email"] == "a b@x.io"


def test_nested_fcm_token_normalized() -> None:
    """Whitelisted keys nested under fcm_credentials are normalized too."""
    bundle = {
        "fcm_credentials": {
            "installation": {"token": " inst alled "},
            "fcm": {"registration": {"token": "reg\nistration"}},
        }
    }
    result = normalize_secrets_bundle(bundle)
    assert result["fcm_credentials"]["installation"]["token"] == "installed"
    assert result["fcm_credentials"]["fcm"]["registration"]["token"] == "registration"


def test_idempotent() -> None:
    """Applying the normalization twice yields the same result."""
    bundle = {
        "owner_key": " AA BB ",
        "username": "  Jane Doe  ",
        "fcm_credentials": {"installation": {"token": " t ok "}},
    }
    once = normalize_secrets_bundle(bundle)
    assert normalize_secrets_bundle(once) == once


def test_input_not_mutated() -> None:
    """The input mapping is never mutated (safe for MappingProxyType)."""
    bundle = {"owner_key": " AA BB ", "nested": {"aas_token": " x y "}}
    normalize_secrets_bundle(bundle)
    assert bundle["owner_key"] == " AA BB "
    assert bundle["nested"]["aas_token"] == " x y "


def test_list_values_recursed() -> None:
    """Lists are walked element-wise, dicts inside them are normalized."""
    bundle = {"items": [{"token": " a b "}, {"username": "  keep me  "}]}
    result = normalize_secrets_bundle(bundle)
    assert result["items"][0]["token"] == "ab"
    assert result["items"][1]["username"] == "keep me"


def test_non_string_scalars_unchanged() -> None:
    """Non-string scalar values pass through unchanged."""
    bundle = {"count": 3, "enabled": True, "missing": None, "owner_key": " A B "}
    result = normalize_secrets_bundle(bundle)
    assert result["count"] == 3
    assert result["enabled"] is True
    assert result["missing"] is None
    assert result["owner_key"] == "AB"


def test_non_mapping_input_returned_unchanged() -> None:
    """A non-mapping top-level value is returned unchanged."""
    assert normalize_secrets_bundle("plain") == "plain"
    assert normalize_secrets_bundle(None) is None


@pytest.mark.asyncio
async def test_config_flow_round_trip_strips_owner_key_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a pasted owner_key with a stray space is persisted clean.

    This is the regression guard for the reported bug: the user pasted a valid
    secrets.json but a copy/paste space inside owner_key broke decryption. The
    persisted secrets bundle must contain the whitespace-free owner_key.
    """
    secrets_payload = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "owner_key": "AA BB\nCC ",
        "shared_key": " DD EE ",
        "username": "  Keep Me  ",
    }

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        # The bundle handed downstream is already normalized.
        assert secrets_bundle is not None
        assert secrets_bundle["owner_key"] == "AABBCC"
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    captured: dict[str, Any] = {}

    async def _create_entry(
        *,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["result"] = {"title": title, "data": data, "options": options}
        return {"type": "create_entry", "title": title, "data": data}

    hass = _FlowHass()
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    flow.async_create_entry = _create_entry  # type: ignore[assignment]

    first = await flow.async_step_secrets_json(
        {"secrets_json": json.dumps(secrets_payload)}
    )
    if inspect.isawaitable(first):
        first = await first
    assert isinstance(first, dict)
    assert first.get("type") == "form"

    final = await flow.async_step_device_selection({})
    if inspect.isawaitable(final):
        final = await final
    assert isinstance(final, dict)
    assert final.get("type") == "create_entry"

    payload = captured.get("result")
    assert payload, "Expected config entry payload to be captured"
    data = payload["data"]
    bundle = data[DATA_SECRET_BUNDLE]
    assert bundle["owner_key"] == "AABBCC"
    assert bundle["shared_key"] == "DDEE"
    # Free-text field keeps interior spaces, only edge-trimmed.
    assert bundle["username"] == "Keep Me"
    # Token persisted from the (clean) aas_token.
    assert data[CONF_OAUTH_TOKEN] == "aas_et/FROM_SECRETS"
    assert data[CONF_GOOGLE_EMAIL] == "user@example.com"

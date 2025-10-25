# tests/test_config_flow_discovery.py
"""Tests covering discovery-specific config flow helpers."""

from __future__ import annotations

import asyncio
from typing import Any
import inspect

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
)


def test_normalize_and_validate_discovery_payload() -> None:
    """Secrets-first discovery payloads should normalize email and tokens."""

    payload = {
        "secrets_json": {
            "google_email": "DiscoveryUser@example.com",
            "aas_token": "aas_et/DISCOVERY",
            "oauth_token": "manually/PERSIST",
        }
    }

    result = config_flow._normalize_and_validate_discovery_payload(payload)

    assert result.email == "DiscoveryUser@example.com"
    assert result.unique_id == "discoveryuser@example.com"
    tokens = {token for _label, token in result.candidates}
    assert "aas_et/DISCOVERY" in tokens
    assert "manually/PERSIST" in tokens
    assert result.secrets_bundle is not None


def test_async_step_discovery_new_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery for a new account should prepare auth data and show confirm."""

    async def _fake_pick(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert email == "new.user@example.com"
        assert secrets_bundle == {"aas_token": "aas_et/VALID_TOKEN_VALUE"}
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> dict[str, Any]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        payload = {
            CONF_GOOGLE_EMAIL: "new.user@example.com",
            "secrets_json": {"aas_token": "aas_et/VALID_TOKEN_VALUE"},
        }

        result = await flow.async_step_discovery(payload)
        if inspect.isawaitable(result):
            result = await result
        assert flow._auth_data.get(CONF_GOOGLE_EMAIL) == "new.user@example.com"  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_AUTH_METHOD) == config_flow._AUTH_METHOD_SECRETS  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_AAS_TOKEN) == "aas_et/VALID_TOKEN_VALUE"  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_SECRET_BUNDLE) == {  # type: ignore[attr-defined]
            "aas_token": "aas_et/VALID_TOKEN_VALUE"
        }
        assert flow.context.get("confirm_only") is True
        placeholders = flow.context.get("title_placeholders", {})
        assert placeholders.get("email") == "new.user@example.com"
        return result

    result = asyncio.run(_exercise())
    assert result["type"] == "form"


def test_async_step_discovery_existing_entry_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery for an existing entry should update data via abort helper."""

    async def _fake_pick(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _Entry:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old",
            }

    entry = _Entry()

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> dict[str, Any]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        payload = {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            "candidate_tokens": ["aas_et/NEW_TOKEN_VALUE"],
        }

        normalized = config_flow._normalize_and_validate_discovery_payload(payload)
        _, updates = await config_flow._ingest_discovery_credentials(
            flow,
            normalized,
            existing_entry=entry,
        )
        assert updates is not None
        assert updates["data"][CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
        assert updates["data"].get(DATA_SECRET_BUNDLE) is None

        flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
        result = await flow.async_step_discovery(payload)
        if inspect.isawaitable(result):
            result = await result
        assert flow._auth_data.get(CONF_OAUTH_TOKEN) == "aas_et/NEW_TOKEN_VALUE"  # type: ignore[attr-defined]
        return result

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


def test_async_step_discovery_invalid_payload() -> None:
    """Invalid payloads should abort with the documented reason."""

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> dict[str, Any]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        return await flow.async_step_discovery({})

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"

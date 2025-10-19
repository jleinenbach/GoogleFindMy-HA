# tests/test_config_flow.py
"""Config flow regression tests for Google Find My Device."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.Auth import token_retrieval
from custom_components.googlefindmy.const import (
    CONF_ACCOUNT_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
)
from homeassistant.data_entry_flow import FlowResultType


class _DummyConfigEntries:
    """Minimal stub for Home Assistant's ConfigEntries manager."""

    def async_entries(self, domain: str) -> list[Any]:  # pragma: no cover - shape compat
        assert domain == config_flow.DOMAIN
        return []


class _DummyHass:
    """Minimal Home Assistant stub used by the config flow tests."""

    def __init__(self) -> None:
        self.config_entries = _DummyConfigEntries()
        self.data: dict[str, Any] = {config_flow.DOMAIN: {}}


def test_secrets_bundle_with_aas_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets that only include an aas_token should complete the flow."""

    perform_calls: list[tuple[str, str, str]] = []
    token_probe_called = False

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        service: str,
        app: str,
        client_sig: str,
    ) -> dict[str, str]:  # noqa: D401 - signature mirrors gpsoauth
        perform_calls.append((username, aas_token, service))
        return {"Auth": "service-token"}

    def fail_exchange(*_: Any, **__: Any) -> None:  # noqa: D401 - ensures we never call exchange
        raise AssertionError("gpsoauth.exchange_token must not be invoked for pre-seeded AAS tokens")

    async def fake_pick_working_token(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str:
        nonlocal token_probe_called
        token_probe_called = True
        del secrets_bundle
        token = candidates[0][1]
        # Issue a service token to mirror the real validation behaviour.
        token_retrieval._perform_oauth_sync(email, token, "android_device_manager", False)
        return token

    async def fake_build(self: config_flow.ConfigFlow):
        token = self._auth_data.get(CONF_OAUTH_TOKEN)
        email = self._auth_data.get(CONF_GOOGLE_EMAIL)
        async def _async_get_basic_device_list(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        dummy_api = SimpleNamespace(async_get_basic_device_list=_async_get_basic_device_list)
        return dummy_api, email, token

    captured_entry: dict[str, Any] = {}

    def fake_async_create_entry(self: config_flow.ConfigFlow, **kwargs: Any) -> dict[str, Any]:
        captured_entry.update(kwargs)
        return {"type": FlowResultType.CREATE_ENTRY, **kwargs}

    async def _run_flow() -> None:
        monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)
        monkeypatch.setattr(token_retrieval.gpsoauth, "exchange_token", fail_exchange)
        monkeypatch.setattr(config_flow, "async_pick_working_token", fake_pick_working_token)
        monkeypatch.setattr(config_flow.ConfigFlow, "_async_build_api_and_username", fake_build)
        monkeypatch.setattr(config_flow.ConfigFlow, "async_create_entry", fake_async_create_entry)
        original_init = config_flow.ConfigFlow.__init__

        def wrapped_init(self: config_flow.ConfigFlow) -> None:
            original_init(self)
            self.unique_id = None

        monkeypatch.setattr(config_flow.ConfigFlow, "__init__", wrapped_init)

        flow = config_flow.ConfigFlow()
        flow.hass = _DummyHass()

        secrets_payload = json.dumps({"email": "user@example.com", "aas_token": "aas_et/test-token-value-123"})

        result = await flow.async_step_secrets_json({"secrets_json": secrets_payload})
        assert result["type"] == FlowResultType.FORM
        assert result.get("errors") in ({}, None)

        assert flow._auth_data.get(CONF_ACCOUNT_OAUTH_TOKEN) == "aas_et/test-token-value-123"
        result2 = await flow.async_step_device_selection({})
        assert result2["type"] == FlowResultType.CREATE_ENTRY

    asyncio.run(_run_flow())

    assert captured_entry, captured_entry
    assert CONF_ACCOUNT_OAUTH_TOKEN in captured_entry["data"], captured_entry["data"]
    assert captured_entry["data"][CONF_ACCOUNT_OAUTH_TOKEN] == "aas_et/test-token-value-123"
    assert CONF_OAUTH_TOKEN not in captured_entry["data"], captured_entry["data"]
    assert token_probe_called

    assert perform_calls == [
        (
            "user@example.com",
            "aas_et/test-token-value-123",
            "oauth2:https://www.googleapis.com/auth/android_device_manager",
        )
    ]

    token_retrieval._perform_oauth_sync(
        "user@example.com",
        captured_entry["data"][CONF_ACCOUNT_OAUTH_TOKEN],
        "android_device_manager",
        False,
    )
    assert len(perform_calls) == 2

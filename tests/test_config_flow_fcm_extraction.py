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
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
)
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)


@pytest.mark.asyncio
async def test_secrets_json_extracts_fcm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config flow should persist fcm_credentials extracted from secrets.json."""

    secrets_payload = {
        "google_email": "fcm-user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "fcm_credentials": {"installation": {"token": "install-token"}},
    }

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert secrets_bundle == secrets_payload
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
                lambda: _ConfigEntries(),
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
        return {"type": "create_entry", "title": title, "data": data, "options": options}

    hass = _FlowHass()
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        assert raise_on_progress is False
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
    assert data[CONF_GOOGLE_EMAIL] == "fcm-user@example.com"
    assert data[CONF_OAUTH_TOKEN] == "aas_et/FROM_SECRETS"
    assert data[DATA_AUTH_METHOD] == config_flow._AUTH_METHOD_SECRETS
    assert data[DATA_SECRET_BUNDLE] == secrets_payload
    assert data["fcm_credentials"] == secrets_payload["fcm_credentials"]

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.network import NoURLAvailableError

import custom_components.googlefindmy as integration
from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    map_token_hex_digest,
    map_token_secret_seed,
)
from custom_components.googlefindmy.entity import GoogleFindMyDeviceEntity


@pytest.mark.asyncio
async def test_async_normalize_device_names_strips_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Device names are normalized while user overrides remain untouched."""

    hass = SimpleNamespace()
    hass.data = {}

    registry = dr.async_get(hass)
    prefixed = registry.async_get_or_create(
        config_entry_id="entry-id",
        identifiers={(DOMAIN, "device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Find My - Alpha",
    )
    user_named = registry.async_get_or_create(
        config_entry_id="entry-id",
        identifiers={(DOMAIN, "device-beta")},
        manufacturer="Google",
        model="Nest",
        name="Find My - Beta",
    )
    user_named.name_by_user = "My Beta"

    await integration._async_normalize_device_names(hass)

    assert prefixed.name == "Alpha"
    assert any(update["name"] == "Alpha" for update in registry.updated)
    assert user_named.name == "Find My - Beta"
    assert not any(update["device_id"] == user_named.id for update in registry.updated)


@pytest.mark.asyncio
async def test_async_refresh_device_urls_updates_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refresh helper rebuilds configuration URLs for integration devices."""

    fake_now = 1_209_600  # deterministic week bucket
    base_url = "https://example.test"

    entry = SimpleNamespace(
        entry_id="entry-1",
        options={OPT_MAP_VIEW_TOKEN_EXPIRATION: True},
    )
    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid"}
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])

    monkeypatch.setattr(integration, "get_url", lambda _hass, **kwargs: base_url)
    monkeypatch.setattr(integration.time, "time", lambda: fake_now)

    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}:device-alpha")},
        manufacturer="Google",
        model="Nest",
        name="Alpha",
    )

    await integration._async_refresh_device_urls(hass)

    expected_token = map_token_hex_digest(
        map_token_secret_seed("ha-uuid", entry.entry_id, True, now=fake_now)
    )
    assert device.configuration_url == (
        f"{base_url}/api/googlefindmy/map/device-alpha?token={expected_token}"
    )
    update = registry.updated[-1]
    assert update["device_id"] == device.id
    assert update["configuration_url"] == device.configuration_url
    assert update["translation_placeholders"] in ({}, None)
    assert update["new_identifiers"] is None


def test_device_configuration_url_warns_when_external_url_missing(
    caplog: pytest.LogCaptureFixture,
    stub_homeassistant_network,
) -> None:
    """Absolute URL resolution warns once and returns ``None`` when unavailable."""

    stub_homeassistant_network(error=NoURLAvailableError("no external URL"))

    hass = SimpleNamespace(data={"core.uuid": "ha-uuid"})
    entity = SimpleNamespace(hass=hass, _base_url_warning_emitted=False)

    caplog.set_level(logging.WARNING)

    first_result = GoogleFindMyDeviceEntity._resolve_absolute_base_url(entity)
    second_result = GoogleFindMyDeviceEntity._resolve_absolute_base_url(entity)

    assert first_result is None
    assert second_result is None

    warnings = [
        record
        for record in caplog.records
        if "Unable to resolve external URL" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert entity._base_url_warning_emitted is True

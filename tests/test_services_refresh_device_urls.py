# tests/test_services_refresh_device_urls.py
"""Validate refresh_device_urls service token scoping and canonical identifiers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from homeassistant.core import ServiceCall

from custom_components.googlefindmy import const
from custom_components.googlefindmy import services


class _StubServices:
    """Capture service registrations for inspection in tests."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], object] = {}

    def async_register(self, domain: str, service: str, handler: object) -> None:
        self.registered[(domain, service)] = handler


class _StubConfigEntries:
    """Config entry manager stub providing domain-filtered lookups."""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[SimpleNamespace]:  # noqa: D401 - simple passthrough
        return list(self._entries) if domain == const.DOMAIN else []

    def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        return None


class _StubDeviceRegistry:
    """Minimal device registry capturing configuration URL updates."""

    def __init__(self, devices: dict[str, SimpleNamespace]) -> None:
        self.devices = devices
        self.updated: dict[str, str] = {}

    def async_update_device(self, *, device_id: str, configuration_url: str) -> None:
        self.updated[device_id] = configuration_url
        self.devices[device_id].configuration_url = configuration_url


def test_refresh_device_urls_uses_entry_scoped_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each device URL uses the owning entry's token policy and canonical identifier."""

    fake_now = 1_209_600  # Aligns with a deterministic week bucket.
    base_url = "https://example.test"

    entry_one = SimpleNamespace(
        entry_id="entry-1",
        options={},
        data={},
    )
    entry_two = SimpleNamespace(
        entry_id="entry-2",
        options={const.OPT_MAP_VIEW_TOKEN_EXPIRATION: True},
        data={},
    )
    config_entries = _StubConfigEntries([entry_one, entry_two])

    hass = SimpleNamespace()
    hass.data = {"core.uuid": "ha-uuid", const.DOMAIN: {"entries": {}}}
    hass.services = _StubServices()
    hass.config_entries = config_entries

    ctx = {
        "domain": const.DOMAIN,
        "resolve_canonical": lambda hass, device_id: (device_id, device_id),
        "is_active_entry": lambda entry: True,
        "primary_active_entry": lambda entries: entries[0] if entries else None,
        "opt": lambda entry, key, default: entry.options.get(key, default),
        "default_map_view_token_expiration": const.DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
        "opt_map_view_token_expiration_key": const.OPT_MAP_VIEW_TOKEN_EXPIRATION,
        "redact_url_token": lambda url: url,
        "soft_migrate_entry": lambda hass, entry: None,
    }

    devices = {
        "ha-dev-1": SimpleNamespace(
            id="ha-dev-1",
            identifiers={(const.DOMAIN, "entry-1:device-alpha")},
            config_entries={"entry-1"},
            serial_number=None,
            name="Alpha",
            name_by_user=None,
        ),
        "ha-dev-2": SimpleNamespace(
            id="ha-dev-2",
            identifiers={(const.DOMAIN, "entry-2:device-beta")},
            config_entries={"entry-2"},
            serial_number="beta-serial",
            name="Beta",
            name_by_user="Backpack",
        ),
        "ha-service": SimpleNamespace(
            id="ha-service",
            identifiers={
                (const.DOMAIN, f"{const.SERVICE_DEVICE_IDENTIFIER_PREFIX}entry-1")
            },
            config_entries={"entry-1"},
            serial_number=None,
            name="Service",
            name_by_user=None,
        ),
    }
    device_registry = _StubDeviceRegistry(devices)

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services, "get_url", lambda hass, **kwargs: base_url)
    monkeypatch.setattr(services.time, "time", lambda: fake_now)

    async def _run_refresh() -> None:
        await services.async_register_services(hass, ctx)
        handler = hass.services.registered[
            (const.DOMAIN, const.SERVICE_REFRESH_DEVICE_URLS)
        ]
        await handler(ServiceCall({}))

    asyncio.run(_run_refresh())

    expected_entry_one_token = const.map_token_hex_digest(
        const.map_token_secret_seed("ha-uuid", "entry-1", False)
    )
    expected_entry_two_token = const.map_token_hex_digest(
        const.map_token_secret_seed("ha-uuid", "entry-2", True, now=fake_now)
    )

    assert device_registry.updated == {
        "ha-dev-1": f"{base_url}/api/googlefindmy/map/device-alpha?token={expected_entry_one_token}",
        "ha-dev-2": f"{base_url}/api/googlefindmy/map/beta-serial?token={expected_entry_two_token}",
    }
    assert "ha-service" not in device_registry.updated

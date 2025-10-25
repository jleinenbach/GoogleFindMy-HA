# tests/test_config_entry_migration.py
"""Regression tests for config entry data migration logic."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    OPT_DEVICE_POLL_DELAY,
)


class _StubConfigEntry:
    """Minimal ConfigEntry stand-in for migration tests."""

    def __init__(self) -> None:
        self.entry_id = "test-entry"
        self.version = 1
        self.title = ""
        self.data: dict[str, Any] = {
            DATA_SECRET_BUNDLE: {"username": "User@Example.com"},
            OPT_DEVICE_POLL_DELAY: 7,
        }
        self.options: dict[str, Any] = {}
        self.unique_id: str | None = None
        self.subentries: dict[str, Any] = {}


class _StubConfigEntries:
    """Capture config entry updates performed by the migration."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self._entry = entry
        self.updated: list[dict[str, Any]] = []

    def async_update_entry(self, entry: _StubConfigEntry, **kwargs: Any) -> None:
        assert entry is self._entry
        self.updated.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "title" in kwargs:
            entry.title = kwargs["title"]
        if "unique_id" in kwargs:
            entry.unique_id = kwargs["unique_id"]


class _FakeRegistryEntry:
    """Minimal entity registry entry used for unique_id migration tests."""

    def __init__(
        self,
        *,
        entity_id: str,
        domain: str,
        platform: str,
        unique_id: str,
        config_entry_id: str,
    ) -> None:
        self.entity_id = entity_id
        self.domain = domain
        self.platform = platform
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class _FakeEntityRegistry:
    """Test double for the Home Assistant entity registry."""

    def __init__(self) -> None:
        self.entities: dict[str, _FakeRegistryEntry] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self.updated: list[str] = []

    def add(
        self,
        *,
        entity_id: str,
        domain: str,
        platform: str,
        unique_id: str,
        config_entry_id: str,
    ) -> None:
        entry = _FakeRegistryEntry(
            entity_id=entity_id,
            domain=domain,
            platform=platform,
            unique_id=unique_id,
            config_entry_id=config_entry_id,
        )
        self.entities[entity_id] = entry
        self._by_key[(domain, platform, unique_id)] = entity_id

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return self._by_key.get((domain, platform, unique_id))

    def async_update_entity(self, entity_id: str, *, new_unique_id: str) -> None:
        entry = self.entities[entity_id]
        self._by_key.pop((entry.domain, entry.platform, entry.unique_id), None)
        entry.unique_id = new_unique_id
        self._by_key[(entry.domain, entry.platform, new_unique_id)] = entity_id
        self.updated.append(entity_id)


class _FakeDeviceRegistry:
    """Minimal device registry stub used to satisfy migration helpers."""

    def __init__(self) -> None:
        self.devices: dict[str, Any] = {}

    def async_update_device(self, **_kwargs: Any) -> None:  # pragma: no cover - stub
        return None


class _StubHass:
    """Namespace providing the config_entries helper."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self.config_entries = _StubConfigEntries(entry)


def test_async_migrate_entry_populates_email_and_options() -> None:
    """Legacy entries should gain email metadata and soft-migrate options."""

    integration = import_module("custom_components.googlefindmy.__init__")

    entry = _StubConfigEntry()
    hass = _StubHass(entry)

    result = asyncio.run(integration.async_migrate_entry(hass, entry))
    assert result is True
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.data[CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert entry.title == "User@Example.com"
    assert entry.unique_id == "user@example.com"
    assert entry.options[OPT_DEVICE_POLL_DELAY] == 7

    hass_second = _StubHass(entry)
    second_result = asyncio.run(integration.async_migrate_entry(hass_second, entry))
    assert second_result is True
    assert hass_second.config_entries.updated == []


def test_unique_id_subentry_migration_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing unique_ids should be upgraded to include the subentry identifier."""

    integration = import_module("custom_components.googlefindmy.__init__")

    entry = _StubConfigEntry()
    entry.entry_id = "entry-1"
    entry.options = MappingProxyType({"unique_id_migrated": True})
    subentry = SimpleNamespace(
        subentry_id="sub-1",
        data={
            "group_key": "core_tracking",
            "features": (
                "binary_sensor",
                "button",
                "device_tracker",
                "sensor",
            ),
        },
        title="Core Tracking",
    )
    entry.subentries = {subentry.subentry_id: subentry}

    hass = _StubHass(entry)

    entity_registry = _FakeEntityRegistry()
    entity_registry.add(
        entity_id="device_tracker.googlefindmy_device_1",
        domain="device_tracker",
        platform=integration.DOMAIN,
        unique_id="entry-1:device-1",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="sensor.googlefindmy_device_1_last_seen",
        domain="sensor",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_device-1_last_seen",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="sensor.googlefindmy_api_updates",
        domain="sensor",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_api_updates_total",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="button.googlefindmy_device_1_play_sound",
        domain="button",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_device-1_play_sound",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="binary_sensor.googlefindmy_polling",
        domain="binary_sensor",
        platform=integration.DOMAIN,
        unique_id="entry-1:polling",
        config_entry_id="entry-1",
    )

    monkeypatch.setattr(integration.er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(
        integration.dr, "async_get", lambda _hass: _FakeDeviceRegistry()
    )

    asyncio.run(integration._async_migrate_unique_ids(hass, entry))

    tracker_entry = entity_registry.entities["device_tracker.googlefindmy_device_1"]
    assert tracker_entry.unique_id == "entry-1:sub-1:device-1"
    last_seen_entry = entity_registry.entities["sensor.googlefindmy_device_1_last_seen"]
    assert last_seen_entry.unique_id == "googlefindmy_entry-1_sub-1_device-1_last_seen"
    stats_entry = entity_registry.entities["sensor.googlefindmy_api_updates"]
    assert stats_entry.unique_id == "googlefindmy_entry-1_sub-1_api_updates_total"
    button_entry = entity_registry.entities["button.googlefindmy_device_1_play_sound"]
    assert button_entry.unique_id == "googlefindmy_entry-1_sub-1_device-1_play_sound"
    binary_entry = entity_registry.entities["binary_sensor.googlefindmy_polling"]
    assert binary_entry.unique_id == "entry-1:sub-1:polling"

    assert entry.options["unique_id_migrated"] is True
    assert entry.options["unique_id_subentry_migrated"] is True
    assert len(entity_registry.updated) >= 5
    assert hass.config_entries.updated

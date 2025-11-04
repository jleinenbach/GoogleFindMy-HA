# tests/test_services_rebuild_registry.py
"""Regression tests for the googlefindmy.rebuild_registry service."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import (
    ATTR_MODE,
    DOMAIN,
    MODE_MIGRATE,
    MODE_REBUILD,
    SERVICE_REBUILD_REGISTRY,
    service_device_identifier,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall

from tests.helpers import (
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeDeviceRegistry,
    FakeEntityRegistry,
    FakeHass,
)


@pytest.mark.asyncio
async def test_rebuild_registry_handles_migration_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Service must migrate and reload entries that recover from MIGRATION_ERROR."""

    entry = FakeConfigEntry(entry_id="entry-1", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    migration_calls: list[tuple[str, str]] = []

    async def _soft_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("soft", cfg_entry.entry_id))

    async def _unique_id_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("unique", cfg_entry.entry_id))

    ctx = {
        "soft_migrate_entry": _soft_migrate,
        "migrate_unique_ids": _unique_id_migrate,
    }

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_REBUILD}))

    assert entry_manager.reload_calls == [entry.entry_id]
    assert entry_manager.migrate_calls == [entry.entry_id]
    assert entry.state == ConfigEntryState.NOT_LOADED
    assert ("soft", entry.entry_id) in migration_calls
    assert ("unique", entry.entry_id) in migration_calls
    assert any("migration error state" in record.message for record in caplog.records)
    assert any("queued for reload" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_rebuild_registry_skips_reload_when_migration_still_required(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Entries that remain in MIGRATION_ERROR must not be reloaded automatically."""

    entry = FakeConfigEntry(entry_id="entry-2", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager([entry], migration_success=False)
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    ctx: dict[str, Any] = {}

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_REBUILD}))

    assert entry_manager.migrate_calls == [entry.entry_id]
    assert entry_manager.reload_calls == []
    assert entry.state == ConfigEntryState.MIGRATION_ERROR
    assert any("manual migration" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_rebuild_registry_skips_reload_when_migration_api_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Entries stay untouched when Home Assistant lacks migration helpers."""

    entry = FakeConfigEntry(entry_id="entry-2b", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager(
        [entry], supports_migrate=False
    )
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    soft_calls: list[str] = []

    async def _soft_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        soft_calls.append(cfg_entry.entry_id)

    ctx = {"soft_migrate_entry": _soft_migrate}

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_REBUILD}))

    assert entry_manager.migrate_calls == []
    assert entry_manager.reload_calls == []
    assert soft_calls == [entry.entry_id]
    assert entry.state == ConfigEntryState.MIGRATION_ERROR
    assert any(
        "cannot retry migration automatically" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_migrate_mode_recovers_migration_error_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MODE_MIGRATE must repair entries stuck in MIGRATION_ERROR before reload."""

    entry = FakeConfigEntry(entry_id="entry-3", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    migration_calls: list[tuple[str, str]] = []

    async def _soft_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("soft", cfg_entry.entry_id))

    async def _unique_id_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("unique", cfg_entry.entry_id))

    ctx = {
        "soft_migrate_entry": _soft_migrate,
        "migrate_unique_ids": _unique_id_migrate,
    }

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_MIGRATE}))

    assert entry_manager.migrate_calls == [entry.entry_id]
    assert entry_manager.reload_calls == [entry.entry_id]
    assert entry.state == ConfigEntryState.NOT_LOADED
    assert migration_calls.count(("soft", entry.entry_id)) >= 2
    assert migration_calls.count(("unique", entry.entry_id)) >= 2
    assert any("queued for reload" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_migrate_mode_skips_reload_when_migration_error_persists(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MODE_MIGRATE must avoid reload attempts if MIGRATION_ERROR persists."""

    entry = FakeConfigEntry(entry_id="entry-4", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager([entry], migration_success=False)
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    migration_calls: list[tuple[str, str]] = []

    async def _soft_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("soft", cfg_entry.entry_id))

    async def _unique_id_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("unique", cfg_entry.entry_id))

    ctx = {
        "soft_migrate_entry": _soft_migrate,
        "migrate_unique_ids": _unique_id_migrate,
    }

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_MIGRATE}))

    assert entry_manager.migrate_calls == [entry.entry_id]
    assert entry_manager.reload_calls == []
    assert entry.state == ConfigEntryState.MIGRATION_ERROR
    assert migration_calls.count(("soft", entry.entry_id)) >= 2
    assert migration_calls.count(("unique", entry.entry_id)) >= 2
    assert any("manual migration" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_migrate_mode_skips_reload_without_migration_api(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MODE_MIGRATE must not reload entries when HA cannot retry migration."""

    entry = FakeConfigEntry(entry_id="entry-4b", state=ConfigEntryState.MIGRATION_ERROR)
    entry_manager = FakeConfigEntriesManager(
        [entry], supports_migrate=False
    )
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass: entity_registry)

    migration_calls: list[tuple[str, str]] = []

    async def _soft_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("soft", cfg_entry.entry_id))

    async def _unique_id_migrate(hass_obj: Any, cfg_entry: FakeConfigEntry) -> None:
        migration_calls.append(("unique", cfg_entry.entry_id))

    ctx = {
        "soft_migrate_entry": _soft_migrate,
        "migrate_unique_ids": _unique_id_migrate,
    }

    await services.async_register_services(hass, ctx)
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    caplog.set_level(logging.INFO)

    await handler(ServiceCall({ATTR_MODE: MODE_MIGRATE}))

    assert entry_manager.migrate_calls == []
    assert entry_manager.reload_calls == []
    assert migration_calls.count(("soft", entry.entry_id)) >= 2
    assert migration_calls.count(("unique", entry.entry_id)) >= 2
    assert entry.state == ConfigEntryState.MIGRATION_ERROR
    assert any("manual migration" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_rebuild_registry_preserves_service_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphan cleanup should keep the service device while dropping trackers."""

    entry = FakeConfigEntry(entry_id="entry-service")
    entry_manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(entry_manager)

    device_registry = FakeDeviceRegistry()
    entity_registry = FakeEntityRegistry()

    service_id = "device-service"
    tracker_id = "device-tracker"

    device_registry.devices[service_id] = SimpleNamespace(
        id=service_id,
        identifiers={service_device_identifier(entry.entry_id)},
        config_entries={entry.entry_id},
    )
    device_registry.devices[tracker_id] = SimpleNamespace(
        id=tracker_id,
        identifiers={(DOMAIN, "tracker-device")},
        config_entries={entry.entry_id},
    )

    removed_devices: list[str] = []

    def _remove_device(device_id: str) -> None:
        removed_devices.append(device_id)
        device_registry.devices.pop(device_id, None)

    device_registry.async_remove_device = _remove_device  # type: ignore[assignment]

    monkeypatch.setattr(services.dr, "async_get", lambda hass_obj: device_registry)
    monkeypatch.setattr(services.er, "async_get", lambda hass_obj: entity_registry)

    await services.async_register_services(hass, {})
    handler = hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]

    await handler(ServiceCall({ATTR_MODE: MODE_REBUILD}))

    assert removed_devices == [tracker_id]
    assert service_id in device_registry.devices

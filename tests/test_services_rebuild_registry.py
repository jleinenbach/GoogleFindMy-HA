# tests/test_services_rebuild_registry.py
from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import (
    ATTR_MODE,
    DOMAIN,
    MODE_REBUILD,
    SERVICE_REBUILD_REGISTRY,
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
    """Service must skip reload and invoke migration helpers for MIGRATION_ERROR entries."""

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

    caplog.set_level(logging.WARNING)

    await handler(ServiceCall({ATTR_MODE: MODE_REBUILD}))

    assert entry_manager.reload_calls == []
    assert ("soft", entry.entry_id) in migration_calls
    assert ("unique", entry.entry_id) in migration_calls
    assert any("migration error state" in record.message for record in caplog.records)

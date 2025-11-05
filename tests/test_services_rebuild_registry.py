# tests/test_services_rebuild_registry.py
"""Regression tests for the googlefindmy.rebuild_registry service."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import DOMAIN, SERVICE_REBUILD_REGISTRY
from homeassistant.core import ServiceCall

from tests.helpers import FakeConfigEntriesManager, FakeConfigEntry, FakeHass


async def _register_rebuild_service(hass: FakeHass, ctx: dict[str, Any]) -> Any:
    """Helper to register the rebuild service and return its handler."""

    await services.async_register_services(hass, ctx)
    return hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]


@pytest.mark.asyncio
async def test_rebuild_registry_reloads_primary_entry(caplog: pytest.LogCaptureFixture) -> None:
    """When no entry IDs are provided, reload the first config entry."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({}))

    assert manager.reload_calls == ["primary"]
    assert any(
        "Reloading config entry: primary" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_reloads_specific_ids(caplog: pytest.LogCaptureFixture) -> None:
    """Reload only the config entries explicitly requested by ID."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
            FakeConfigEntry(entry_id="tertiary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(
        ServiceCall({services.ATTR_ENTRY_ID: ["secondary", "missing", "primary"]})
    )

    assert manager.reload_calls == ["secondary", "primary"]
    assert any(
        "Reloading config entries: ['secondary', 'primary']" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_accepts_single_entry_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Treat a lone entry ID string as a single-item reload request."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({services.ATTR_ENTRY_ID: "primary"}))

    assert manager.reload_calls == ["primary"]
    assert any(
        "Reloading config entries: ['primary']" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_logs_warning_for_invalid_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn and exit when none of the provided entry IDs are valid."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(
        ServiceCall({services.ATTR_ENTRY_ID: ["missing-1", "missing-2"]})
    )

    assert manager.reload_calls == []
    assert any(
        "No valid config entries found for IDs" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_handles_missing_entries(caplog: pytest.LogCaptureFixture) -> None:
    """Gracefully warn when the integration has no config entries to reload."""

    manager = FakeConfigEntriesManager([])
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({}))

    assert manager.reload_calls == []
    assert any(
        "No config entries available to reload." in record.message
        for record in caplog.records
    )

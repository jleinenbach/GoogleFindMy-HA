# tests/test_entry_self_healing_cleanup.py
"""Regression coverage for the device-registry self-healing helper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import _self_heal_device_registry
from custom_components.googlefindmy.const import DOMAIN, service_device_identifier
from tests.helpers import (
    FakeConfigEntry,
    FakeDeviceEntry,
    FakeDeviceRegistry,
    device_registry_async_entries_for_config_entry,
)


def test_self_heal_removes_only_incorrect_tracker_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper must strip ghost ``via_device_id`` links without side effects."""

    entry = FakeConfigEntry(entry_id="entry-1")
    service_identifier = service_device_identifier(entry.entry_id)
    service_device = FakeDeviceEntry(
        id="service-device",
        identifiers={service_identifier},
        config_entries={entry.entry_id},
    )
    ghost_tracker = FakeDeviceEntry(
        id="ghost-tracker",
        identifiers={(DOMAIN, "tracker-ghost")},
        config_entries={entry.entry_id},
        via_device_id="service-device",
        name="Ghost Tracker",
    )
    clean_tracker = FakeDeviceEntry(
        id="clean-tracker",
        identifiers={(DOMAIN, "tracker-clean")},
        config_entries={entry.entry_id},
    )
    foreign_tracker = FakeDeviceEntry(
        id="foreign-tracker",
        identifiers={(DOMAIN, "tracker-foreign")},
        config_entries={"entry-2"},
        via_device_id="service-device",
    )
    registry = FakeDeviceRegistry(
        [service_device, ghost_tracker, clean_tracker, foreign_tracker]
    )

    monkeypatch.setattr(
        "custom_components.googlefindmy.dr.async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.dr.async_entries_for_config_entry",
        device_registry_async_entries_for_config_entry,
        raising=False,
    )

    hass = SimpleNamespace()

    _self_heal_device_registry(hass, entry)

    assert ghost_tracker.via_device_id is None
    assert clean_tracker.via_device_id is None
    assert foreign_tracker.via_device_id == "service-device"
    assert [
        (device_id, changes)
        for device_id, changes in registry.updated
    ] == [("ghost-tracker", {"via_device_id": None})]

    # Idempotence: running the helper again should not mutate any devices.
    _self_heal_device_registry(hass, entry)
    assert len(registry.updated) == 1

# tests/test_coordinator_device_registry.py
"""Regression tests for coordinator device registry linkage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from collections.abc import Iterable

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import DOMAIN
from homeassistant.helpers import device_registry as dr


class _FakeDeviceEntry:
    """Minimal stand-in for Home Assistant's DeviceEntry."""

    def __init__(
        self,
        *,
        identifiers: Iterable[tuple[str, str]],
        config_entry_id: str,
        name: str | None,
        via_device: str | None,
    ) -> None:
        self.identifiers: set[tuple[str, str]] = set(identifiers)
        self.config_entries = {config_entry_id}
        self.id = f"device-{config_entry_id}-{len(self.identifiers)}"
        self.name = name
        self.name_by_user = None
        self.disabled_by = None
        self.via_device = via_device


class _FakeDeviceRegistry:
    """Fake device registry capturing `async_get_or_create` calls."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.devices: list[_FakeDeviceEntry] = []

    def async_get_device(
        self, *, identifiers: set[tuple[str, str]]
    ) -> _FakeDeviceEntry | None:
        for device in self.devices:
            if identifiers & device.identifiers:
                return device
        return None

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        manufacturer: str,
        model: str,
        name: str | None,
        via_device: str | None,
    ) -> _FakeDeviceEntry:
        entry = _FakeDeviceEntry(
            identifiers=identifiers,
            config_entry_id=config_entry_id,
            name=name,
            via_device=via_device,
        )
        self.devices.append(entry)
        self.created.append(
            {
                "config_entry_id": config_entry_id,
                "identifiers": identifiers,
                "manufacturer": manufacturer,
                "model": model,
                "name": name,
                "via_device": via_device,
            }
        )
        return entry

    def async_update_device(
        self,
        *,
        device_id: str,
        new_identifiers: Iterable[tuple[str, str]] | None = None,
        via_device: str | None = None,
        name: str | None = None,
    ) -> None:
        for device in self.devices:
            if device.id == device_id:
                if new_identifiers is not None:
                    device.identifiers = set(new_identifiers)
                if via_device is not None:
                    device.via_device = via_device
                if name is not None:
                    device.name = name
                self.updated.append(
                    {
                        "device_id": device_id,
                        "new_identifiers": None
                        if new_identifiers is None
                        else set(new_identifiers),
                        "via_device": via_device,
                        "name": name,
                    }
                )
                return
        raise AssertionError(f"Unknown device_id {device_id}")


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> _FakeDeviceRegistry:
    """Patch Home Assistant's device registry helper with a lightweight stub."""

    registry = _FakeDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)
    return registry


def test_devices_link_to_service_device(fake_registry: _FakeDeviceRegistry) -> None:
    """Newly created devices must reference the service device via `via_device`."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.hass = object()
    coordinator._service_device_id = "svc-device-1"

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "abc123", "name": "Pixel"}],
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.created[0]["identifiers"] == {(DOMAIN, "entry-42:abc123")}
    assert fake_registry.created[0]["via_device"] == "svc-device-1"


def test_legacy_device_migrates_to_service_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Legacy devices gain the service device parent during migration."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.hass = object()
    coordinator._service_device_id = "svc-device-1"

    legacy = _FakeDeviceEntry(
        identifiers={(DOMAIN, "abc123")},
        config_entry_id="entry-42",
        name=None,
        via_device=None,
    )
    fake_registry.devices.append(legacy)

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "abc123", "name": "Pixel"}],
        ignored=set(),
    )

    assert created == 2
    assert legacy.via_device == "svc-device-1"
    assert legacy.identifiers == {(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")}
    assert fake_registry.updated[0]["via_device"] == "svc-device-1"
    assert legacy.name == "Pixel"

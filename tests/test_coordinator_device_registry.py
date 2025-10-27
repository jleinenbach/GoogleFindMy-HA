# tests/test_coordinator_device_registry.py
"""Regression tests for coordinator device registry linkage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from collections.abc import Iterable

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import DOMAIN, service_device_identifier
from homeassistant.helpers import device_registry as dr


class _FakeDeviceEntry:
    """Minimal stand-in for Home Assistant's DeviceEntry."""

    _counter = 0

    def __init__(
        self,
        *,
        identifiers: Iterable[tuple[str, str]],
        config_entry_id: str,
        name: str | None,
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        sw_version: str | None = None,
        entry_type: Any | None = None,
        configuration_url: str | None = None,
    ) -> None:
        self.identifiers: set[tuple[str, str]] = set(identifiers)
        self.config_entries = {config_entry_id}
        type(self)._counter += 1
        self.id = f"device-{config_entry_id}-{type(self)._counter}"
        self.name = name
        self.name_by_user = None
        self.disabled_by = None
        self.via_device_id = via_device_id
        self.via_device = via_device
        self.manufacturer = manufacturer
        self.model = model
        self.sw_version = sw_version
        self.entry_type = entry_type
        self.configuration_url = configuration_url


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
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> _FakeDeviceEntry:
        entry = _FakeDeviceEntry(
            identifiers=identifiers,
            config_entry_id=config_entry_id,
            name=name,
            via_device_id=via_device_id,
            via_device=via_device,
            manufacturer=manufacturer,
            model=model,
            sw_version=kwargs.get("sw_version"),
            entry_type=kwargs.get("entry_type"),
            configuration_url=kwargs.get("configuration_url"),
        )
        self.devices.append(entry)
        self.created.append(
            {
                "config_entry_id": config_entry_id,
                "identifiers": identifiers,
                "manufacturer": manufacturer,
                "model": model,
                "name": name,
                "via_device_id": via_device_id,
                "via_device": via_device,
                "sw_version": kwargs.get("sw_version"),
                "entry_type": kwargs.get("entry_type"),
                "configuration_url": kwargs.get("configuration_url"),
            }
        )
        return entry

    def async_update_device(
        self,
        *,
        device_id: str,
        new_identifiers: Iterable[tuple[str, str]] | None = None,
        via_device_id: str | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        for device in self.devices:
            if device.id == device_id:
                if new_identifiers is not None:
                    device.identifiers = set(new_identifiers)
                if via_device_id is not None:
                    device.via_device_id = via_device_id
                if name is not None:
                    device.name = name
                if "manufacturer" in kwargs:
                    device.manufacturer = kwargs["manufacturer"]
                if "model" in kwargs:
                    device.model = kwargs["model"]
                if "sw_version" in kwargs:
                    device.sw_version = kwargs["sw_version"]
                if "entry_type" in kwargs:
                    device.entry_type = kwargs["entry_type"]
                if "configuration_url" in kwargs:
                    device.configuration_url = kwargs["configuration_url"]
                self.updated.append(
                    {
                        "device_id": device_id,
                        "new_identifiers": None
                        if new_identifiers is None
                        else set(new_identifiers),
                        "via_device_id": via_device_id,
                        "name": name,
                        "manufacturer": kwargs.get("manufacturer"),
                        "model": kwargs.get("model"),
                        "sw_version": kwargs.get("sw_version"),
                        "entry_type": kwargs.get("entry_type"),
                        "configuration_url": kwargs.get("configuration_url"),
                    }
                )
                return
        raise AssertionError(f"Unknown device_id {device_id}")

    def async_get(self, device_id: str) -> _FakeDeviceEntry | None:
        for device in self.devices:
            if device.id == device_id:
                return device
        return None


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> _FakeDeviceRegistry:
    """Patch Home Assistant's device registry helper with a lightweight stub."""

    _FakeDeviceEntry._counter = 0
    if not hasattr(dr, "DeviceEntryType"):
        monkeypatch.setattr(
            dr,
            "DeviceEntryType",
            SimpleNamespace(SERVICE="service"),
            raising=False,
        )
    registry = _FakeDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)
    return registry


def test_devices_link_to_service_device(fake_registry: _FakeDeviceRegistry) -> None:
    """Newly created devices must reference the service device via `via_device_id`."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.hass = object()
    coordinator._service_device_id = "svc-device-1"

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "abc123", "name": "Pixel"}],
        ignored=set(),
    )

    assert created == 1
    service_ident = service_device_identifier("entry-42")
    assert fake_registry.created[0]["identifiers"] == {(DOMAIN, "entry-42:abc123")}
    assert fake_registry.created[0]["via_device"] == service_ident
    assert fake_registry.created[0]["via_device_id"] is None
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"


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
        via_device_id=None,
    )
    fake_registry.devices.append(legacy)

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "abc123", "name": "Pixel"}],
        ignored=set(),
    )

    assert created == 2
    assert legacy.via_device_id == "svc-device-1"
    assert legacy.identifiers == {(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")}
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"
    assert legacy.name == "Pixel"


def test_existing_device_backfills_via_link(fake_registry: _FakeDeviceRegistry) -> None:
    """Existing namespaced devices gain the service device parent if missing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.hass = object()
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Pixel",
        via_device_id=None,
    )
    fake_registry.devices.append(existing)

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "abc123", "name": "Pixel"}],
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"
    assert existing.via_device_id == "svc-device-1"


def test_service_device_backfills_via_links(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Devices created before the service device exists are relinked once available."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.hass = object()
    coordinator._service_device_ready = False
    coordinator._service_device_id = None
    coordinator._pending_via_updates = set()

    devices = [
        {"id": "abc123", "name": "Pixel"},
        {"id": "def456", "name": "Tablet"},
    ]

    created = coordinator._ensure_registry_for_devices(devices, set())

    assert created == 2
    service_ident = service_device_identifier("entry-42")
    assert fake_registry.created[0]["via_device"] == service_ident
    assert fake_registry.created[1]["via_device"] == service_ident
    for entry in fake_registry.devices:
        assert entry.via_device_id is None

    pending = getattr(coordinator, "_pending_via_updates")
    assert len(pending) == 2

    coordinator._ensure_service_device_exists()

    service_id = coordinator._service_device_id
    assert service_id is not None
    service_ident = service_device_identifier("entry-42")
    for entry in fake_registry.devices:
        if service_ident in entry.identifiers:
            continue
        assert entry.via_device_id == service_id

    assert getattr(coordinator, "_pending_via_updates") == set()


def test_rebuild_flow_creates_devices_with_via_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Safe-mode rebuild path should recreate devices using via_device linkage."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = SimpleNamespace(entry_id="entry-77")
    coordinator.hass = object()

    # Simulate a safe-mode rebuild: service device removed, pending queue cleared.
    coordinator._service_device_ready = False
    coordinator._service_device_id = None
    coordinator._pending_via_updates = set()
    coordinator._dr_supports_via_device_kw = True

    created = coordinator._ensure_registry_for_devices(
        devices=[{"id": "ghi789", "name": "Phone"}],
        ignored=set(),
    )

    assert created == 1
    metadata = fake_registry.created[0]
    parent_identifier = service_device_identifier("entry-77")
    assert metadata["via_device"] == parent_identifier
    assert metadata["via_device_id"] is None
    pending = getattr(coordinator, "_pending_via_updates")
    assert pending == {fake_registry.devices[0].id}

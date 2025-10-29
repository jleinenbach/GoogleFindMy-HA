# tests/test_coordinator_device_registry.py
"""Regression tests for coordinator device registry linkage."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from typing import Any
from collections.abc import Iterable

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import (
    DOMAIN,
    INTEGRATION_VERSION,
    SERVICE_DEVICE_MANUFACTURER,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    service_device_identifier,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for tests."""

    return f"{entry_id}-{key}-subentry"


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
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        config_subentry_id: str | None = None,
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
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders
        self.config_subentry_id = config_subentry_id


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
        name: str | None = None,
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
            translation_key=kwargs.get("translation_key"),
            translation_placeholders=kwargs.get("translation_placeholders"),
            config_subentry_id=kwargs.get("config_subentry_id"),
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
                "translation_key": kwargs.get("translation_key"),
                "translation_placeholders": kwargs.get("translation_placeholders"),
                "config_subentry_id": kwargs.get("config_subentry_id"),
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
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        config_subentry_id: str | None = None,
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
                if translation_key is not None:
                    device.translation_key = translation_key
                if translation_placeholders is not None:
                    device.translation_placeholders = translation_placeholders
                if config_subentry_id is not None:
                    device.config_subentry_id = config_subentry_id
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
                        "translation_key": translation_key,
                        "translation_placeholders": translation_placeholders,
                        "config_subentry_id": config_subentry_id,
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


def _build_entry_with_subentries(entry_id: str) -> SimpleNamespace:
    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry_id}-service",
        subentry_id=_stable_subentry_id(entry_id, SERVICE_SUBENTRY_KEY),
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "visible_device_ids": []}),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-tracker",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    return SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={
            service_subentry.subentry_id: service_subentry,
            tracker_subentry.subentry_id: tracker_subentry,
        },
        runtime_data=None,
        service_subentry_id=service_subentry.subentry_id,
        tracker_subentry_id=tracker_subentry.subentry_id,
    )


def _prepare_coordinator_for_registry(
    coordinator: GoogleFindMyCoordinator, entry: SimpleNamespace
) -> None:
    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = []
    coordinator._enabled_poll_device_ids = set()
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 5
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._subentry_manager = None
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )
    coordinator._pending_via_updates = set()


def test_devices_link_to_service_device(fake_registry: _FakeDeviceRegistry) -> None:
    """Newly created devices must reference the service device via `via_device_id`."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    service_ident = service_device_identifier("entry-42")
    assert fake_registry.created[0]["identifiers"] == {(DOMAIN, "entry-42:abc123")}
    assert fake_registry.created[0]["via_device"] == service_ident
    assert fake_registry.created[0]["via_device_id"] is None
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"
    assert (
        fake_registry.created[0]["config_subentry_id"] == entry.tracker_subentry_id
    )
    assert (
        fake_registry.updated[0]["config_subentry_id"] == entry.tracker_subentry_id
    )


def test_legacy_device_migrates_to_service_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Legacy devices gain the service device parent during migration."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    legacy = _FakeDeviceEntry(
        identifiers={(DOMAIN, "abc123")},
        config_entry_id="entry-42",
        name=None,
        via_device_id=None,
    )
    fake_registry.devices.append(legacy)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 2
    assert legacy.via_device_id == "svc-device-1"
    assert legacy.identifiers == {(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")}
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"
    assert fake_registry.updated[0]["config_subentry_id"] == entry.tracker_subentry_id
    assert legacy.name == "Pixel"
    assert legacy.config_subentry_id == entry.tracker_subentry_id


def test_existing_device_backfills_via_link(fake_registry: _FakeDeviceRegistry) -> None:
    """Existing namespaced devices gain the service device parent if missing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Pixel",
        via_device_id=None,
    )
    fake_registry.devices.append(existing)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.updated[0]["via_device_id"] == "svc-device-1"
    assert existing.via_device_id == "svc-device-1"
    assert fake_registry.updated[0]["config_subentry_id"] == entry.tracker_subentry_id
    assert existing.config_subentry_id == entry.tracker_subentry_id


def test_service_device_backfills_via_links(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Devices created before the service device exists are relinked once available."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_ready = False
    coordinator._service_device_id = None

    devices = [
        {"id": "abc123", "name": "Pixel"},
        {"id": "def456", "name": "Tablet"},
    ]

    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(devices, set())

    assert created == 2
    service_ident = service_device_identifier("entry-42")
    assert fake_registry.created[0]["via_device"] == service_ident
    assert fake_registry.created[1]["via_device"] == service_ident
    for device_entry in fake_registry.devices:
        assert device_entry.via_device_id is None

    pending = getattr(coordinator, "_pending_via_updates")
    assert len(pending) == 2

    coordinator._ensure_service_device_exists()

    service_id = coordinator._service_device_id
    assert service_id is not None
    service_ident = service_device_identifier("entry-42")
    service_entry = next(
        entry for entry in fake_registry.devices if service_ident in entry.identifiers
    )
    assert service_entry.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert service_entry.translation_placeholders == {}
    assert service_entry.config_subentry_id == entry.service_subentry_id
    metadata = fake_registry.created[-1]
    assert metadata["identifiers"] == {service_ident}
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id
    for device_entry in fake_registry.devices:
        if service_ident in device_entry.identifiers:
            continue
        assert device_entry.via_device_id == service_id
        assert device_entry.config_subentry_id == entry.tracker_subentry_id

    assert getattr(coordinator, "_pending_via_updates") == set()


def test_service_device_updates_add_translation(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Existing service devices gain translation metadata when missing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)

    service_ident = service_device_identifier("entry-42")
    legacy_service = _FakeDeviceEntry(
        identifiers={service_ident},
        config_entry_id="entry-42",
        name=None,
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        sw_version=INTEGRATION_VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        translation_key=None,
        translation_placeholders=None,
    )
    fake_registry.devices.append(legacy_service)

    coordinator._ensure_service_device_exists()

    assert legacy_service.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert legacy_service.translation_placeholders == {}
    assert legacy_service.config_subentry_id == entry.service_subentry_id
    assert fake_registry.updated
    metadata = fake_registry.updated[0]
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id


def test_service_device_preserves_user_defined_name(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """User-defined service device names should not be cleared during updates."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-99")
    _prepare_coordinator_for_registry(coordinator, entry)

    service_ident = service_device_identifier("entry-99")
    legacy_service = _FakeDeviceEntry(
        identifiers={service_ident},
        config_entry_id="entry-99",
        name="Custom Hub",
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        sw_version=INTEGRATION_VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        translation_key=None,
        translation_placeholders=None,
        config_subentry_id=None,
    )
    legacy_service.name_by_user = "Custom Hub"
    fake_registry.devices.append(legacy_service)

    coordinator._ensure_service_device_exists()

    assert legacy_service.name == "Custom Hub"
    assert legacy_service.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert legacy_service.translation_placeholders == {}
    assert legacy_service.config_subentry_id == entry.service_subentry_id
    assert fake_registry.updated
    metadata = fake_registry.updated[0]
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id


def test_rebuild_flow_creates_devices_with_via_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Safe-mode rebuild path should recreate devices using via_device linkage."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-77")
    _prepare_coordinator_for_registry(coordinator, entry)

    # Simulate a safe-mode rebuild: service device removed, pending queue cleared.
    coordinator._service_device_ready = False
    coordinator._service_device_id = None
    coordinator._dr_supports_via_device_kw = True

    devices = [{"id": "ghi789", "name": "Phone"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    metadata = fake_registry.created[0]
    parent_identifier = service_device_identifier("entry-77")
    assert metadata["via_device"] == parent_identifier
    assert metadata["via_device_id"] is None
    assert metadata["config_subentry_id"] == entry.tracker_subentry_id
    pending = getattr(coordinator, "_pending_via_updates")
    assert pending == {fake_registry.devices[0].id}

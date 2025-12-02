# tests/test_coordinator_subentry_visibility.py
"""Coordinator subentry visibility regression tests."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr

from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import (
    GoogleFindMyCoordinator,
    SubentryMetadata,
)


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for fixtures."""

    return f"{entry_id}-{key}-subentry"


class _StubDeviceEntry:
    """Minimal device entry stub exposing registry metadata."""

    def __init__(
        self,
        *,
        device_id: str,
        identifiers: set[tuple[str, str]],
        name: str | None = None,
    ) -> None:
        self.id = device_id
        self.identifiers = identifiers
        self.name = name
        self.name_by_user = None
        self.disabled_by = None


class _StubDeviceRegistry:
    """Stub registry returning known device entries by ID."""

    def __init__(self, entries: dict[str, _StubDeviceEntry]) -> None:
        self._entries = entries

    def async_get(self, device_id: str) -> _StubDeviceEntry | None:
        return self._entries.get(device_id)


class _ManagerStub:
    """Capture subentry manager updates for verification."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def update_visible_device_ids(
        self, subentry_key: str, device_ids: tuple[str, ...]
    ) -> None:
        self.calls.append((subentry_key, device_ids))


def test_visibility_accepts_namespaced_device_id() -> None:
    """Visibility checks must accept namespaced registry identifiers."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._subentry_metadata = {
        TRACKER_SUBENTRY_KEY: SubentryMetadata(
            key=TRACKER_SUBENTRY_KEY,
            config_subentry_id="subentry-1",
            features=(),
            title=None,
            poll_intervals=MappingProxyType({}),
            filters=MappingProxyType({}),
            feature_flags=MappingProxyType({}),
            visible_device_ids=("parent123:device-abc",),
            enabled_device_ids=(),
        )
    }

    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, "device-abc")
    assert coordinator.is_device_visible_in_subentry(
        TRACKER_SUBENTRY_KEY, "parent123:device-abc"
    )
    assert not coordinator.is_device_visible_in_subentry(
        TRACKER_SUBENTRY_KEY, "other-device"
    )


def test_refresh_normalizes_registry_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry identifiers in subentry settings must resolve to canonical IDs."""

    entry_id = "entry-test"
    canonical_id = "tracker-1"
    registry_id = "device-entry-test-1"

    registry = _StubDeviceRegistry(
        {
            registry_id: _StubDeviceEntry(
                device_id=registry_id,
                identifiers={(DOMAIN, f"{entry_id}:{canonical_id}")},
                name="Tracker One",
            )
        }
    )
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)

    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": [registry_id],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core",
        unique_id=f"{entry_id}-core",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry = SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={subentry.subentry_id: subentry},
        runtime_data=None,
    )

    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": canonical_id, "name": "Tracker One"}]
    coordinator._enabled_poll_device_ids = {canonical_id}
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 30
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

    coordinator._refresh_subentry_index()

    service_metadata = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_metadata is not None
    assert service_metadata.visible_device_ids == ()
    assert service_metadata.config_subentry_id == _stable_subentry_id(
        entry_id, SERVICE_SUBENTRY_KEY
    )

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert metadata.visible_device_ids == (registry_id, canonical_id)
    assert metadata.enabled_device_ids == (canonical_id,)

    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, canonical_id)
    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, registry_id)


def test_default_subentry_prefers_tracker_and_skips_service_manager_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracker subentry should be default and service updates must not emit manager calls."""

    entry_id = "entry-default"
    registry = _StubDeviceRegistry({})
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)

    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry_id}-service",
        subentry_id=_stable_subentry_id(entry_id, SERVICE_SUBENTRY_KEY),
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": ["device-1"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-trackers",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )

    entry = SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={
            service_subentry.subentry_id: service_subentry,
            tracker_subentry.subentry_id: tracker_subentry,
        },
        runtime_data=None,
    )

    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": "device-1", "name": "Tracker One"}]
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = SERVICE_SUBENTRY_KEY
    coordinator._subentry_manager = _ManagerStub()
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )

    coordinator._refresh_subentry_index()

    assert coordinator._default_subentry_key() == TRACKER_SUBENTRY_KEY
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.visible_device_ids == ()
    assert service_meta.config_subentry_id == _stable_subentry_id(
        entry_id, SERVICE_SUBENTRY_KEY
    )
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.visible_device_ids == ("device-1",)

    manager_stub = coordinator._subentry_manager
    assert isinstance(manager_stub, _ManagerStub)
    assert manager_stub.calls
    assert all(key != SERVICE_SUBENTRY_KEY for key, _ in manager_stub.calls)

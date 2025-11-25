# tests/test_coordinator_visibility_availability.py
"""Regression tests for visibility and availability handling in the coordinator."""

from __future__ import annotations

import time
from types import MappingProxyType, SimpleNamespace

from homeassistant.config_entries import ConfigSubentry

from custom_components.googlefindmy.button import GoogleFindMyPlaySoundButton
from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import (
    FcmStatus,
    GoogleFindMyCoordinator,
    SubentryMetadata,
)
from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker
from custom_components.googlefindmy.sensor import GoogleFindMyLastSeenSensor


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for refresh tests."""

    return f"{entry_id}-{key}-subentry"


def _build_entry_with_empty_visible_list() -> SimpleNamespace:
    """Return a config-entry stub containing an empty visible-device list."""

    entry = SimpleNamespace(
        entry_id="entry-empty-visible",
        title="Google Find My",
        data={},
        options={},
        subentries={},
        runtime_data=None,
    )
    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id="entry-empty-visible-service",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {"group_key": TRACKER_SUBENTRY_KEY, "visible_device_ids": []}
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core",
        unique_id="entry-empty-visible-core",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry.subentries[service_subentry.subentry_id] = service_subentry
    entry.subentries[tracker_subentry.subentry_id] = tracker_subentry
    return entry


def _build_coordinator(
    entry: SimpleNamespace, hass_stub: SimpleNamespace, device_id: str, name: str
) -> GoogleFindMyCoordinator:
    """Return a coordinator stub populated with a single tracker device."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": device_id, "name": name}]
    coordinator._enabled_poll_device_ids = {device_id}
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
    coordinator._device_location_data = {}
    coordinator._device_names = {}
    coordinator._present_last_seen = {device_id: time.monotonic()}
    coordinator._presence_ttl_s = 300
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )
    coordinator.can_play_sound = lambda _dev_id: True  # type: ignore[assignment]
    return coordinator


def test_refresh_recovers_devices_from_empty_visible_list() -> None:
    """Refreshing with an empty visible list must repopulate device metadata."""

    entry = _build_entry_with_empty_visible_list()
    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": "device-1", "name": "Device One"}]
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
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._subentry_manager = None
    coordinator._device_location_data = {}
    coordinator._device_names = {}
    coordinator._present_last_seen = {"device-1": time.monotonic()}
    coordinator._presence_ttl_s = 300
    coordinator.can_play_sound = lambda _dev_id: True  # type: ignore[assignment]

    coordinator._refresh_subentry_index()

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert metadata.visible_device_ids == ("device-1",)
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.visible_device_ids == ()
    assert service_meta.config_subentry_id == _stable_subentry_id(
        entry.entry_id, SERVICE_SUBENTRY_KEY
    )

    coordinator._store_subentry_snapshots(coordinator.data)
    subentry_identifier = coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )

    sensor = GoogleFindMyLastSeenSensor(
        coordinator,
        {"id": "device-1", "name": "Device One"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    button = GoogleFindMyPlaySoundButton(
        coordinator,
        {"id": "device-1", "name": "Device One"},
        "Device One",
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )

    assert sensor.available is True
    assert button.available is True


def test_reconfigure_without_changes_preserves_tracker_visibility() -> None:
    """Reload after reconfigure must keep trackers visible when config is unchanged."""

    entry = SimpleNamespace(
        entry_id="entry-reconfigure-visible",
        title="Google Find My",
        data={},
        options={},
        subentries={
            _stable_subentry_id(
                "entry-reconfigure-visible", SERVICE_SUBENTRY_KEY
            ): ConfigSubentry(
                data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
                subentry_type=SUBENTRY_TYPE_SERVICE,
                title="Service",
                unique_id="entry-reconfigure-visible-service",
                subentry_id=_stable_subentry_id(
                    "entry-reconfigure-visible", SERVICE_SUBENTRY_KEY
                ),
            )
        },
        runtime_data=None,
    )
    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})

    initial_coordinator = _build_coordinator(
        entry, hass_stub, "device-1", "Visible Tracker"
    )
    initial_coordinator._refresh_subentry_index()

    initial_metadata = initial_coordinator.get_subentry_metadata(
        key=TRACKER_SUBENTRY_KEY
    )
    assert initial_metadata is not None
    assert initial_metadata.visible_device_ids == ("device-1",)

    reconfigure_coordinator = _build_coordinator(
        entry, hass_stub, "device-1", "Visible Tracker"
    )
    reconfigure_coordinator._present_last_seen = {}
    reconfigure_coordinator._device_location_data = {
        "device-1": {
            "latitude": 1.0,
            "longitude": 2.0,
            "accuracy": 5.0,
            "last_seen": time.time(),
        }
    }
    reconfigure_coordinator._refresh_subentry_index()
    reconfigure_coordinator._store_subentry_snapshots(reconfigure_coordinator.data)

    reloaded_metadata = reconfigure_coordinator.get_subentry_metadata(
        key=TRACKER_SUBENTRY_KEY
    )
    assert reloaded_metadata is not None
    assert reloaded_metadata.visible_device_ids == ("device-1",)

    subentry_identifier = reconfigure_coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )
    tracker = GoogleFindMyDeviceTracker(
        reconfigure_coordinator,
        {"id": "device-1", "name": "Visible Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    sensor = GoogleFindMyLastSeenSensor(
        reconfigure_coordinator,
        {"id": "device-1", "name": "Visible Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    button = GoogleFindMyPlaySoundButton(
        reconfigure_coordinator,
        {"id": "device-1", "name": "Visible Tracker"},
        "Visible Tracker",
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )

    assert reconfigure_coordinator.is_device_present("device-1") is True
    assert tracker.available is True
    assert sensor.available is True
    assert button.available is True


def test_entities_remain_available_when_push_disconnected() -> None:
    """Sensors and buttons stay available if the device remains present without push."""

    entry = _build_entry_with_empty_visible_list()
    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    device = {"id": "device-1", "name": "Device One"}
    coordinator.data = [device]
    coordinator._device_names = {"device-1": "Device One"}
    now_wall = time.time()
    coordinator._device_location_data = {
        "device-1": {"last_seen": now_wall, "last_updated": now_wall}
    }
    coordinator._present_last_seen = {"device-1": time.monotonic()}
    coordinator._presence_ttl_s = 300
    coordinator._push_cooldown_until = 0.0
    coordinator._push_ready_memo = None
    coordinator._device_caps = {}
    coordinator._fcm_status_state = FcmStatus.DISCONNECTED
    coordinator._fcm_status_reason = "push offline"
    coordinator._fcm_status_changed_at = now_wall
    coordinator._feature_to_subentry = {
        "sensor": TRACKER_SUBENTRY_KEY,
        "button": TRACKER_SUBENTRY_KEY,
    }
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    tracker_subentry_id = _stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY)
    coordinator._subentry_metadata = {
        TRACKER_SUBENTRY_KEY: SubentryMetadata(
            key=TRACKER_SUBENTRY_KEY,
            config_subentry_id=tracker_subentry_id,
            features=("sensor", "button"),
            title="Core",
            poll_intervals={},
            filters={},
            feature_flags={},
            visible_device_ids=("device-1",),
            enabled_device_ids=("device-1",),
        )
    }
    coordinator._subentry_snapshots = {}
    coordinator._store_subentry_snapshots(coordinator.data)

    coordinator.api = SimpleNamespace(is_push_ready=lambda: False)

    subentry_identifier = coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )
    sensor = GoogleFindMyLastSeenSensor(
        coordinator,
        device,
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    button = GoogleFindMyPlaySoundButton(
        coordinator,
        device,
        "Device One",
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )

    assert coordinator.can_play_sound("device-1") is True
    assert sensor.available is True
    assert button.available is True

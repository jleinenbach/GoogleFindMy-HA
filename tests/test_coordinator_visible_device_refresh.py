# tests/test_coordinator_visible_device_refresh.py
"""Regression tests for visible-device refresh handling in the coordinator."""

from __future__ import annotations

import time
from types import MappingProxyType, SimpleNamespace

from custom_components.googlefindmy.button import GoogleFindMyPlaySoundButton
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.sensor import GoogleFindMyLastSeenSensor
from homeassistant.config_entries import ConfigSubentry


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
    subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": "core_tracking", "visible_device_ids": []}),
        subentry_type="googlefindmy_feature_group",
        title="Core",
        unique_id="entry-empty-visible-core",
    )
    entry.subentries[subentry.subentry_id] = subentry
    return entry


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
    coordinator._default_subentry_key_value = "core_tracking"
    coordinator._subentry_manager = None
    coordinator._device_location_data = {}
    coordinator._device_names = {}
    coordinator._present_last_seen = {"device-1": time.monotonic()}
    coordinator._presence_ttl_s = 300
    coordinator.can_play_sound = lambda _dev_id: True  # type: ignore[assignment]

    coordinator._refresh_subentry_index()

    metadata = coordinator.get_subentry_metadata(key="core_tracking")
    assert metadata is not None
    assert metadata.visible_device_ids == ("device-1",)

    coordinator._store_subentry_snapshots(coordinator.data)
    subentry_identifier = coordinator.stable_subentry_identifier(key="core_tracking")

    sensor = GoogleFindMyLastSeenSensor(
        coordinator,
        {"id": "device-1", "name": "Device One"},
        subentry_key="core_tracking",
        subentry_identifier=subentry_identifier,
    )
    button = GoogleFindMyPlaySoundButton(
        coordinator,
        {"id": "device-1", "name": "Device One"},
        "Device One",
        subentry_key="core_tracking",
        subentry_identifier=subentry_identifier,
    )

    assert sensor.available is True
    assert button.available is True

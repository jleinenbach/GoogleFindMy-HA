# tests/test_coordinator_significance.py
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


def _make_coordinator(existing: dict[str, Any]) -> GoogleFindMyCoordinator:
    """Create a coordinator instance with preloaded cache data for testing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {"device-1": dict(existing)}
    coordinator._device_names = {}
    coordinator._device_update_history = {}
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    return coordinator


def _stat_recorder() -> tuple[dict[str, int], Callable[[str], None]]:
    """Return a stat counter map and increment callback for assertions."""

    counts: dict[str, int] = {}

    def _increment(stat_name: str) -> None:
        counts[stat_name] = counts.get(stat_name, 0) + 1

    return counts, _increment


def _weighted_coordinates(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> tuple[float, float]:
    """Return the weighted fusion result used by the coordinator."""

    w_old = 1 / (max(1.0, float(existing["accuracy"])) ** 2)
    w_new = 1 / (max(1.0, float(incoming["accuracy"])) ** 2)
    total = w_old + w_new

    fused_lat = (existing["latitude"] * w_old + incoming["latitude"] * w_new) / total
    fused_lon = (existing["longitude"] * w_old + incoming["longitude"] * w_new) / total
    return fused_lat, fused_lon


def test_stale_timestamp_is_rejected_before_merge() -> None:
    """Out-of-order payloads must not regress cached coordinates."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "status": "coordinate",
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    stale = {
        "latitude": 37.4224,
        "longitude": -122.085,
        "accuracy": 30.0,
        "last_seen": existing["last_seen"] - 120,
        "status": "coordinate",
    }

    coordinator.update_device_cache("device-1", stale)

    cached = coordinator._device_location_data["device-1"]
    assert cached["latitude"] == pytest.approx(existing["latitude"])
    assert cached["longitude"] == pytest.approx(existing["longitude"])
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert stat_counts == {
        "invalid_ts_drop_count": 1,
        "drop_reason_invalid_ts": 1,
    }


def test_same_timestamp_with_new_altitude_is_significant() -> None:
    """Altitude-only updates fuse coordinates but refresh height."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "altitude": None,
    }
    coordinator = _make_coordinator(existing)

    new_data = {**existing, "altitude": 120.0}

    coordinator.update_device_cache("device-1", new_data)

    cached = coordinator._device_location_data["device-1"]
    assert cached["altitude"] == pytest.approx(120.0)
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert cached["status"] == "Fused (Weighted)"


def test_same_timestamp_with_altitude_delta_is_significant() -> None:
    """Altitude changes fuse while keeping timestamps stable."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "altitude": 110.0,
    }
    coordinator = _make_coordinator(existing)

    new_data = {**existing, "altitude": 126.5}

    coordinator.update_device_cache("device-1", new_data)

    cached = coordinator._device_location_data["device-1"]
    assert cached["altitude"] == pytest.approx(126.5)
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert cached["status"] == "Fused (Weighted)"


def test_accuracy_gain_is_significant_even_when_stationary() -> None:
    """Accuracy-only gains fuse coordinates without suppressing movement."""

    existing = {
        "latitude": 52.52,
        "longitude": 13.405,
        "accuracy": 150.0,
        "last_seen": 1_700_000_000.0,
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    new_data = {
        **existing,
        "latitude": existing["latitude"] + 0.00005,
        "longitude": existing["longitude"] + 0.00005,
        "accuracy": 100.0,  # ~33 % improvement triggers accuracy gate
        "last_seen": existing["last_seen"] + 15,
    }

    coordinator.update_device_cache("device-1", new_data)

    cached = coordinator._device_location_data["device-1"]
    fused_lat, fused_lon = _weighted_coordinates(existing, new_data)

    assert cached["latitude"] == pytest.approx(fused_lat)
    assert cached["longitude"] == pytest.approx(fused_lon)
    assert cached["accuracy"] == pytest.approx(100.0)
    assert cached["last_seen"] == pytest.approx(new_data["last_seen"])
    assert cached["status"] == "Fused (Weighted)"
    assert stat_counts == {"background_updates": 1, "fused_updates": 1}


def test_stationary_update_fuses_overlapping_coordinates() -> None:
    """Sub-threshold movement fuses coordinates instead of clamping."""

    existing = {
        "latitude": 40.7128,
        "longitude": -74.006,
        "accuracy": 150.0,
        "last_seen": 1_700_000_000.0,
        "status": "coordinate",
        "source_label": "semantic/unknown",
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    new_payload = {
        "latitude": existing["latitude"] + 0.0003,  # ~33 m delta, below accuracy
        "longitude": existing["longitude"] + 0.0003,
        "accuracy": 115.0,
        "last_seen": existing["last_seen"] + 30,
        "status": existing["status"],
    }

    coordinator.update_device_cache("device-1", new_payload)

    cached = coordinator._device_location_data["device-1"]
    fused_lat, fused_lon = _weighted_coordinates(existing, new_payload)

    assert cached["latitude"] == pytest.approx(fused_lat)
    assert cached["longitude"] == pytest.approx(fused_lon)
    assert cached["accuracy"] == pytest.approx(115.0)
    assert cached["last_seen"] == pytest.approx(new_payload["last_seen"])
    assert cached["status"] == "Fused (Weighted)"
    assert stat_counts == {"background_updates": 1, "fused_updates": 1}


def test_stationary_metadata_change_fuses_coordinates() -> None:
    """Low-movement updates fuse coordinates while refreshing metadata."""

    existing = {
        "latitude": 40.7128,
        "longitude": -74.006,
        "accuracy": 150.0,
        "last_seen": 1_700_000_000.0,
        "status": "coordinate",
        "battery_level": 0.9,
        "source_label": "semantic/unknown",
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    new_payload = {
        "latitude": existing["latitude"] + 0.0003,  # ~33 m delta, below accuracy
        "longitude": existing["longitude"] + 0.0003,
        "accuracy": 115.0,
        "last_seen": existing["last_seen"] + 45,
        "battery_level": 0.55,
        "status": "low_battery",
    }

    coordinator.update_device_cache("device-1", new_payload)

    cached = coordinator._device_location_data["device-1"]
    fused_lat, fused_lon = _weighted_coordinates(existing, new_payload)

    assert cached["latitude"] == pytest.approx(fused_lat)
    assert cached["longitude"] == pytest.approx(fused_lon)
    assert cached["accuracy"] == pytest.approx(115.0)
    assert cached["last_seen"] == pytest.approx(new_payload["last_seen"])
    assert cached["battery_level"] == pytest.approx(new_payload["battery_level"])
    assert cached["status"] == "Fused (Weighted)"
    assert stat_counts == {"background_updates": 1, "fused_updates": 1}


def test_identity_key_delta_triggers_resolver_refresh() -> None:
    """FCM-provided identity key updates must refresh the resolver cache."""

    existing = {
        "latitude": 40.0,
        "longitude": -75.0,
        "accuracy": 20.0,
        "last_seen": 1_700_000_000.0,
        "encrypted_identity_key": b"old",
        "owner_key_version": 1,
    }
    coordinator = _make_coordinator(existing)

    refresh_calls: list[str] = []
    coordinator._schedule_eid_resolver_refresh = lambda: refresh_calls.append("refresh")

    incoming = {
        "latitude": existing["latitude"] + 0.0002,
        "longitude": existing["longitude"] + 0.0002,
        "accuracy": 10.0,
        "last_seen": existing["last_seen"] + 60,
        "encrypted_identity_key": b"new",
        "owner_key_version": 2,
    }

    coordinator.update_device_cache("device-1", incoming)

    cached = coordinator._device_location_data["device-1"]
    assert cached["encrypted_identity_key"] == b"new"
    assert cached["owner_key_version"] == 2
    assert refresh_calls == ["refresh"]


def test_identity_key_stability_skips_resolver_refresh() -> None:
    """Repeated identity key payloads should not retrigger resolver work."""

    existing = {
        "latitude": 41.0,
        "longitude": -76.0,
        "accuracy": 15.0,
        "last_seen": 1_700_000_000.0,
        "encrypted_identity_key": b"stable",
        "owner_key_version": 3,
    }
    coordinator = _make_coordinator(existing)

    refresh_calls: list[str] = []
    coordinator._schedule_eid_resolver_refresh = lambda: refresh_calls.append("refresh")

    incoming = {
        "latitude": existing["latitude"],
        "longitude": existing["longitude"],
        "accuracy": 12.0,
        "last_seen": existing["last_seen"] + 90,
        "encrypted_identity_key": b"stable",
        "owner_key_version": 3,
    }

    coordinator.update_device_cache("device-1", incoming)

    assert refresh_calls == []


def test_update_cache_keeps_coordinates_when_semantic_refresh_arrives() -> None:
    """Semantic-only updates must not drop cached coordinates for a device."""

    existing = {
        "name": "Tracker",
        "latitude": 48.137154,
        "longitude": 11.576124,
        "accuracy": 30.0,
        "altitude": 520.0,
        "last_seen": 1_700_000_000.0,
        "last_seen_utc": "2023-11-14T22:13:20Z",
        "semantic_name": "Warehouse",
        "status": "coordinate",
    }
    coordinator = _make_coordinator(existing)

    incoming = {
        "name": "Tracker",
        "device_id": "device-1",
        "id": "device-1",
        "semantic_name": "Service Center",
        "status": "semantic_only",
        "last_seen": existing["last_seen"],
    }

    coordinator.update_device_cache("device-1", incoming)

    cached = coordinator._device_location_data["device-1"]
    assert cached["semantic_name"] == "Service Center"
    assert cached["latitude"] == pytest.approx(existing["latitude"])
    assert cached["longitude"] == pytest.approx(existing["longitude"])
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])


def test_update_cache_preserves_last_seen_when_timestamp_missing() -> None:
    """Payloads without last_seen must inherit the cached timestamp markers."""

    existing = {
        "name": "Tracker",
        "latitude": 34.052235,
        "longitude": -118.243683,
        "accuracy": 12.0,
        "last_seen": 1_700_100_000.0,
        "last_seen_utc": "2023-11-16T02:53:20Z",
        "semantic_name": "Studio",
        "status": "coordinate",
    }
    coordinator = _make_coordinator(existing)

    incoming = {
        "name": "Tracker",
        "device_id": "device-1",
        "id": "device-1",
        "semantic_name": "Set",
        "status": "semantic_only",
        # last_seen omitted on purpose
    }

    coordinator.update_device_cache("device-1", incoming)

    cached = coordinator._device_location_data["device-1"]
    assert cached["semantic_name"] == "Set"
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert cached["last_seen_utc"] == existing["last_seen_utc"]

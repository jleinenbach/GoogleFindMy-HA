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
    coordinator._movement_threshold = 50.0
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


def test_same_timestamp_with_new_altitude_is_significant() -> None:
    """Altitude-only updates stay stationary but retain freshness markers."""

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
    assert cached["altitude"] is None
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert cached["status"] == "Stationary (Below Threshold)"


def test_same_timestamp_with_altitude_delta_is_significant() -> None:
    """Altitude changes alone keep stationary cache coordinates intact."""

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
    assert cached["altitude"] == pytest.approx(existing["altitude"])
    assert cached["last_seen"] == pytest.approx(existing["last_seen"])
    assert cached["status"] == "Stationary (Below Threshold)"


def test_accuracy_gain_is_significant_even_when_stationary() -> None:
    """Accuracy-only gains below the movement threshold keep cached coordinates."""

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
    assert cached["latitude"] == pytest.approx(existing["latitude"])
    assert cached["longitude"] == pytest.approx(existing["longitude"])
    assert cached["accuracy"] == pytest.approx(existing["accuracy"])
    assert cached["last_seen"] == pytest.approx(new_data["last_seen"])
    assert cached["status"] == "Stationary (Below Threshold)"
    assert stat_counts == {"background_updates": 1}


def test_stationary_update_fuses_overlapping_coordinates() -> None:
    """Sub-threshold movement snaps back to cached coordinates for stability."""

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
    assert cached["latitude"] == pytest.approx(existing["latitude"])
    assert cached["longitude"] == pytest.approx(existing["longitude"])
    assert cached["accuracy"] == pytest.approx(existing["accuracy"])
    assert cached["last_seen"] == pytest.approx(new_payload["last_seen"])
    assert cached["status"] == "Stationary (Below Threshold)"
    assert stat_counts == {"background_updates": 1}


def test_stationary_metadata_change_fuses_coordinates() -> None:
    """Low-movement updates keep cached coordinates while refreshing metadata."""

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
    assert cached["latitude"] == pytest.approx(existing["latitude"])
    assert cached["longitude"] == pytest.approx(existing["longitude"])
    assert cached["accuracy"] == pytest.approx(existing["accuracy"])
    assert cached["last_seen"] == pytest.approx(new_payload["last_seen"])
    assert cached["battery_level"] == pytest.approx(new_payload["battery_level"])
    assert cached["status"] == "Stationary (Below Threshold)"
    assert stat_counts == {"background_updates": 1}


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

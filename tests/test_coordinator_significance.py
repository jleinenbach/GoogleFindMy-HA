# tests/test_coordinator_significance.py
from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


def _make_coordinator(existing: dict[str, Any]) -> GoogleFindMyCoordinator:
    """Create a coordinator instance with preloaded cache data for testing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {"device-1": dict(existing)}
    coordinator._movement_threshold = 50.0
    coordinator._device_names = {}
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    return coordinator


def test_same_timestamp_with_new_altitude_is_significant() -> None:
    """Adding altitude to an otherwise identical payload should be significant."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "altitude": None,
    }
    coordinator = _make_coordinator(existing)

    new_data = {**existing, "altitude": 120.0}

    assert coordinator._is_significant_update("device-1", new_data)


def test_same_timestamp_with_altitude_delta_is_significant() -> None:
    """A material change in altitude should bypass the duplicate gate."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "altitude": 110.0,
    }
    coordinator = _make_coordinator(existing)

    new_data = {**existing, "altitude": 126.5}

    assert coordinator._is_significant_update("device-1", new_data)


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

# tests/test_coordinator_significance.py
from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


def _make_coordinator(existing: dict[str, Any]) -> GoogleFindMyCoordinator:
    """Create a coordinator instance with preloaded cache data for testing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {"device-1": existing}
    coordinator._movement_threshold = 50.0
    coordinator.increment_stat = lambda *_args, **_kwargs: None
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

"""Regression test for hybrid low-accuracy polling updates."""

from __future__ import annotations

import time
from typing import Any, cast

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


@pytest.mark.asyncio
async def test_weighted_fusion_shields_trusted_anchor() -> None:
    """Location fusion should preserve trusted anchors when jitter overlaps."""

    now = time.time()
    device_id = "device-1"
    previous_latitude = 50.0
    previous_longitude = 10.0
    previous_accuracy = 25.0

    coordinator = cast(Any, GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator))
    coordinator._device_location_data = {
        device_id: {
            "id": device_id,
            "name": "Tracker",
            "latitude": previous_latitude,
            "longitude": previous_longitude,
            "accuracy": previous_accuracy,
            "last_seen": now - 60,
            "location_type": "trusted",
        }
    }
    coordinator._movement_threshold = 15
    coordinator.increment_stat = lambda *_, **__: None

    new_payload = {
        "id": device_id,
        "name": "Tracker",
        "latitude": previous_latitude + 0.00005,
        "longitude": previous_longitude + 0.00005,
        "accuracy": 150.0,
        "last_seen": now,
        "status": "GPS fix",
    }

    assert coordinator._is_significant_update(device_id, new_payload)

    assert new_payload["latitude"] == pytest.approx(previous_latitude)
    assert new_payload["longitude"] == pytest.approx(previous_longitude)
    assert new_payload["accuracy"] == pytest.approx(previous_accuracy)
    assert new_payload.get("location_type") == "trusted"
    assert new_payload.get("status") == "Stationary (at Anchor)"

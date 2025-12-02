"""Regression test for hybrid low-accuracy polling updates."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


@pytest.mark.asyncio
async def test_hybrid_update_preserves_cached_coordinates() -> None:
    """Low-accuracy poll updates should reuse cached coordinates but refresh timestamps."""

    now = time.time()
    previous_timestamp = now - 1000
    previous_latitude = 50.0
    previous_longitude = 10.0
    previous_accuracy = 20.0

    coordinator = cast(Any, GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator))
    coordinator._poll_lock = asyncio.Lock()
    coordinator._is_polling = False
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._note_fcm_deferral = lambda *_, **__: None
    coordinator._schedule_short_retry = lambda *_, **__: None
    coordinator._clear_fcm_deferral = lambda *_, **__: None
    coordinator.safe_update_metric = lambda *_, **__: None
    coordinator._get_google_home_filter = lambda: None
    coordinator._set_auth_state = lambda **__: None
    coordinator._should_preserve_precise_home_coordinates = lambda *_, **__: False
    coordinator._normalize_coords = lambda *_args, **_kwargs: True
    coordinator._track_device_interval = lambda *_, **__: None
    coordinator.increment_stat = lambda *_, **__: None
    coordinator._is_significant_update = lambda *_, **__: True
    coordinator._apply_report_type_cooldown = lambda *_, **__: None
    coordinator.push_updated = lambda *_, **__: None
    coordinator.async_set_updated_data = lambda *_, **__: None
    coordinator.async_set_update_error = lambda *_, **__: None
    coordinator._get_ignored_set = lambda: set()
    coordinator._build_snapshot_from_cache = lambda *_args, **_kwargs: []
    coordinator.device_poll_delay = 0
    coordinator._fcm_defer_started_mono = 0.0
    coordinator._last_poll_result = None
    coordinator._last_device_list = []
    coordinator._consecutive_timeouts = 0
    coordinator._min_accuracy_threshold = 100

    device_id = "device-1"
    coordinator._device_location_data = {
        device_id: {
            "id": device_id,
            "name": "Tracker",
            "latitude": previous_latitude,
            "longitude": previous_longitude,
            "accuracy": previous_accuracy,
            "last_seen": previous_timestamp,
        }
    }

    coordinator.api = SimpleNamespace(
        async_get_device_location=AsyncMock(
            return_value={
                "id": device_id,
                "name": "Tracker",
                "latitude": 51.0,
                "longitude": 11.0,
                "accuracy": 5000.0,
                "last_seen": now,
            }
        )
    )

    await coordinator._async_start_poll_cycle([{"id": device_id, "name": "Tracker"}])

    updated = coordinator._device_location_data[device_id]
    assert updated["latitude"] == previous_latitude
    assert updated["longitude"] == previous_longitude
    assert updated.get("accuracy") == previous_accuracy
    assert updated["last_seen"] == pytest.approx(now)

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import OPT_SEMANTIC_LOCATIONS
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.homeassistant import GoogleFindMyConfigEntryStub


class _DummyAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def async_get_device_location(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(self._payload)


class _TrackingFilter:
    def __init__(self, should_filter: bool = False, replacement: dict[str, float] | None = None) -> None:
        self.should_filter = should_filter
        self.replacement = replacement
        self.called = 0

    def should_filter_detection(self, *_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, float] | None]:
        self.called += 1
        return self.should_filter, self.replacement


class _RaisingFilter:
    def __init__(self) -> None:
        self.called = 0

    def should_filter_detection(self, *_args: Any, **_kwargs: Any) -> tuple[bool, dict[str, float] | None]:
        self.called += 1
        raise AssertionError("Spam filter should not run when semantic mapping applies")


def _base_coordinator(
    options: dict[str, Any], google_filter: Any, api_payload: dict[str, Any]
) -> GoogleFindMyCoordinator:
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = GoogleFindMyConfigEntryStub(options=options)
    coordinator.hass = SimpleNamespace()
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator.async_set_updated_data = lambda *_args, **_kwargs: None
    coordinator.push_updated = lambda *_args, **_kwargs: None
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._should_preserve_precise_home_coordinates = lambda *_args, **_kwargs: False
    coordinator._normalize_coords = lambda *_args, **_kwargs: True
    coordinator._is_significant_update = lambda *_args, **_kwargs: True
    coordinator.update_device_cache = lambda *_args, **_kwargs: None
    coordinator._set_auth_state = lambda **_kwargs: None
    coordinator._device_location_data = {}
    coordinator._device_poll_cooldown_until = {}
    coordinator._present_last_seen = {}
    coordinator._locate_inflight = set()
    coordinator._locate_cooldown_until = {}
    coordinator._device_update_history = {}
    coordinator._last_poll_mono = 0.0
    coordinator._consecutive_timeouts = 0
    coordinator.location_poll_interval = 0
    coordinator._min_accuracy_threshold = 0
    coordinator._movement_threshold = 0
    coordinator.data = []
    coordinator._last_device_list = []
    coordinator._get_ignored_set = lambda: set()
    coordinator._build_snapshot_from_cache = lambda *_args, **_kwargs: []
    coordinator._get_google_home_filter = lambda: google_filter
    coordinator.api = _DummyAPI(api_payload)
    coordinator.get_device_display_name = lambda device_id: device_id
    coordinator.can_request_location = lambda _device_id: True
    coordinator._api_push_ready = lambda: True
    coordinator._is_on_hass_loop = lambda: True
    return coordinator


@pytest.mark.asyncio
async def test_manual_locate_prefers_semantic_mapping() -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "Lobby": {"latitude": 1.25, "longitude": 2.5, "accuracy": 4.0}
        }
    }
    google_filter = _RaisingFilter()
    coordinator = _base_coordinator(options, google_filter, {"semantic_name": "lobby"})

    result = await coordinator.async_locate_device("device-1")

    assert result["latitude"] == pytest.approx(1.25)
    assert result["longitude"] == pytest.approx(2.5)
    assert result["accuracy"] == pytest.approx(4.0)
    assert result["location_type"] == "trusted"
    assert google_filter.called == 0


def _polling_coordinator(
    options: dict[str, Any], google_filter: Any, api_payload: dict[str, Any]
) -> GoogleFindMyCoordinator:
    coordinator = _base_coordinator(options, google_filter, api_payload)
    coordinator._poll_lock = asyncio.Lock()
    coordinator._is_polling = False
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._note_fcm_deferral = lambda *_args, **_kwargs: None
    coordinator._schedule_short_retry = lambda *_args, **_kwargs: None
    coordinator._clear_fcm_deferral = lambda: None
    coordinator._last_poll_result = None
    coordinator._startup_complete = True
    coordinator._fcm_defer_started_mono = 0.0
    coordinator._fcm_last_stage = 0
    coordinator.device_poll_delay = 0
    coordinator.safe_update_metric = lambda *_args, **_kwargs: None
    return coordinator


@pytest.mark.asyncio
async def test_poll_cycle_applies_mapping_before_spam_filter() -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "HomeHub": {"latitude": 10.0, "longitude": 20.0, "accuracy": 15.0}
        }
    }
    google_filter = _RaisingFilter()
    coordinator = _polling_coordinator(options, google_filter, {"semantic_name": "homehub"})

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    cached = coordinator._device_location_data["dev-1"]
    assert cached["latitude"] == pytest.approx(10.0)
    assert cached["longitude"] == pytest.approx(20.0)
    assert cached["accuracy"] == pytest.approx(15.0)
    assert cached["location_type"] == "trusted"
    assert google_filter.called == 0


@pytest.mark.asyncio
async def test_poll_cycle_preserves_spam_filter_for_unmapped_semantics() -> None:
    google_filter = _TrackingFilter(should_filter=True)
    coordinator = _polling_coordinator({}, google_filter, {"semantic_name": "Office"})

    await coordinator._async_start_poll_cycle([{"id": "dev-2", "name": "Device"}])

    assert "dev-2" not in coordinator._device_location_data
    assert google_filter.called == 1

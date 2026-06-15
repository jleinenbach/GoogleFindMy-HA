# tests/test_coordinator_locate_basics.py
"""Branch-Coverage tests for ``coordinator.locate``.

Phase 3 AP-C: target ``coordinator/locate.py`` branch-coverage by
exercising the pure helpers (``_normalize_coords``, ``_get_device_lock``,
``can_request_location``, ``can_play_sound``) and the gating branches of
the async helpers (``async_locate_device``, ``async_play_sound``,
``async_stop_sound``). Deep success-path branches (Nova roundtrip,
Google Home filter, weighted fusion, cache commit) remain out of scope
and stay for Phase 4.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from custom_components.googlefindmy.const import DEFAULT_MIN_POLL_INTERVAL
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.locate_mixin_stub import LocateStub


@pytest.fixture
def coord() -> LocateStub:
    """Return a default :class:`LocateStub` bound to a synthetic config entry."""

    entry = make_config_entry(entry_id="locate-test-entry")
    return LocateStub(config_entry=entry)


class TestNormalizeCoords:
    """Exercise ``_normalize_coords`` branches."""

    def test_missing_lat_returns_false(self, coord: LocateStub) -> None:
        payload: dict = {"longitude": 12.0}
        assert coord._normalize_coords(payload) is False
        coord.increment_stat.assert_not_called()

    def test_missing_lon_returns_false(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": 50.0}
        assert coord._normalize_coords(payload) is False
        coord.increment_stat.assert_not_called()

    def test_non_numeric_increments_invalid_and_returns_false(
        self, coord: LocateStub
    ) -> None:
        payload: dict = {"latitude": "abc", "longitude": "def"}
        assert coord._normalize_coords(payload) is False
        coord.increment_stat.assert_called_once_with("invalid_coords")

    def test_non_numeric_warn_off_skips_warning(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": "abc", "longitude": "def"}
        assert coord._normalize_coords(payload, warn_on_invalid=False) is False
        coord.increment_stat.assert_called_once_with("invalid_coords")

    @pytest.mark.parametrize(
        ("lat", "lon"),
        [
            (math.nan, 0.0),
            (math.inf, 0.0),
            (0.0, math.inf),
            (95.0, 0.0),
            (0.0, -181.0),
        ],
    )
    def test_out_of_range_increments_invalid(
        self, coord: LocateStub, lat: float, lon: float
    ) -> None:
        payload: dict = {"latitude": lat, "longitude": lon}
        assert coord._normalize_coords(payload) is False
        coord.increment_stat.assert_called_once_with("invalid_coords")

    def test_valid_coords_returns_true_and_normalizes(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": "50.0", "longitude": "12.0"}
        assert coord._normalize_coords(payload) is True
        assert payload["latitude"] == 50.0
        assert payload["longitude"] == 12.0

    def test_accuracy_valid_kept(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": 50.0, "longitude": 12.0, "accuracy": "5.0"}
        assert coord._normalize_coords(payload) is True
        assert payload["accuracy"] == 5.0

    def test_accuracy_invalid_dropped(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": 50.0, "longitude": 12.0, "accuracy": 0.0}
        assert coord._normalize_coords(payload) is True
        assert "accuracy" not in payload

    def test_accuracy_unparsable_dropped(self, coord: LocateStub) -> None:
        payload: dict = {"latitude": 50.0, "longitude": 12.0, "accuracy": "junk"}
        assert coord._normalize_coords(payload) is True
        assert "accuracy" not in payload


class TestGetDeviceLock:
    """Exercise ``_get_device_lock`` branches."""

    def test_create_new_lock(self, coord: LocateStub) -> None:
        lock = coord._get_device_lock("dev-1")
        assert isinstance(lock, asyncio.Lock)
        assert coord._device_action_locks["dev-1"] is lock

    def test_return_existing_lock(self, coord: LocateStub) -> None:
        first = coord._get_device_lock("dev-1")
        second = coord._get_device_lock("dev-1")
        assert first is second


class TestCanRequestLocation:
    """Exercise ``can_request_location`` branches."""

    def test_ignored_device_blocked(self, coord: LocateStub) -> None:
        coord.is_ignored.return_value = True
        assert coord.can_request_location("dev-1") is False

    def test_polling_in_progress_blocked(self, coord: LocateStub) -> None:
        coord._is_polling = True
        assert coord.can_request_location("dev-1") is False

    def test_inflight_blocked(self, coord: LocateStub) -> None:
        coord._locate_inflight.add("dev-1")
        assert coord.can_request_location("dev-1") is False

    def test_manual_cooldown_active_blocked(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 100.0,
        )
        coord._locate_cooldown_until["dev-1"] = 200.0
        assert coord.can_request_location("dev-1") is False

    def test_poll_cooldown_active_blocked(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 100.0,
        )
        coord._device_poll_cooldown_until["dev-1"] = 200.0
        assert coord.can_request_location("dev-1") is False

    def test_all_clear_allows(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 1000.0,
        )
        assert coord.can_request_location("dev-1") is True

    def test_expired_cooldown_allows(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 300.0,
        )
        coord._locate_cooldown_until["dev-1"] = 200.0
        coord._device_poll_cooldown_until["dev-1"] = 250.0
        assert coord.can_request_location("dev-1") is True


class TestCanPlaySound:
    """Exercise ``can_play_sound`` branches."""

    def test_capability_known_true(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": True}
        assert coord.can_play_sound("dev-1") is True

    def test_capability_known_false(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": False}
        assert coord.can_play_sound("dev-1") is False

    def test_push_not_ready_with_cooldown_blocks(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord._api_push_ready.return_value = False
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 100.0,
        )
        coord._push_cooldown_until = 200.0
        assert coord.can_play_sound("dev-1") is False

    def test_push_not_ready_no_cooldown_falls_back_optimistic(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord._api_push_ready.return_value = False
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 1000.0,
        )
        coord._ensure_device_name_cache.return_value = {"dev-1": "Phone"}
        assert coord.can_play_sound("dev-1") is True

    def test_known_device_optimistic_true(self, coord: LocateStub) -> None:
        coord._ensure_device_name_cache.return_value = {"dev-1": "Phone"}
        assert coord.can_play_sound("dev-1") is True

    def test_unknown_device_falls_back_optimistic(self, coord: LocateStub) -> None:
        coord._ensure_device_name_cache.return_value = {}
        assert coord.can_play_sound("dev-1") is True


class TestAsyncLocateDeviceGating:
    """Exercise gating branches of ``async_locate_device`` (no Nova roundtrip)."""

    async def test_blocks_when_cannot_request_location(self, coord: LocateStub) -> None:
        coord.is_ignored.return_value = True
        result = await coord.async_locate_device("dev-1")
        assert result == {}
        coord.api.async_get_device_location.assert_not_called()

    async def test_blocks_when_push_cooldown_active(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 100.0,
        )
        coord._api_push_ready.return_value = False
        coord._push_cooldown_until = 200.0
        result = await coord.async_locate_device("dev-1")
        assert result == {}
        coord.api.async_get_device_location.assert_not_called()

    async def test_empty_payload_returns_empty(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 1000.0,
        )
        coord.api.async_get_device_location.return_value = None
        result = await coord.async_locate_device("dev-1")
        assert result == {}
        assert "dev-1" not in coord._locate_inflight  # finally branch cleared it

    async def test_payload_without_coords_returns_empty(
        self, coord: LocateStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 1000.0,
        )
        coord.api.async_get_device_location.return_value = {
            "last_seen": 1234567890,
        }
        result = await coord.async_locate_device("dev-1")
        assert result == {}


class TestAsyncPlaySoundGating:
    """Exercise gating branches of ``async_play_sound``."""

    async def test_blocks_when_cannot_play_sound(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": False}
        ok = await coord.async_play_sound("dev-1")
        assert ok is False
        coord.api.async_play_sound.assert_not_called()

    async def test_success_stores_uuid(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": True}
        ok = await coord.async_play_sound("dev-1")
        assert ok is True
        assert coord._sound_request_uuids.get("dev-1") == "uuid-stub"
        coord._async_save_sound_uuids.assert_awaited_once()

    async def test_failure_notes_problem(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": True}
        coord.api.async_play_sound.return_value = (False, None)
        ok = await coord.async_play_sound("dev-1")
        assert ok is False
        coord._note_push_transport_problem.assert_called_once()

    async def test_unexpected_exception_returns_false(self, coord: LocateStub) -> None:
        coord._device_caps["dev-1"] = {"can_ring": True}
        coord.api.async_play_sound.side_effect = RuntimeError("boom")
        ok = await coord.async_play_sound("dev-1")
        assert ok is False
        coord.note_error.assert_called_once()
        coord._note_push_transport_problem.assert_called_once()


class TestAsyncStopSoundGating:
    """Exercise gating branches of ``async_stop_sound``."""

    async def test_blocks_when_push_not_ready(self, coord: LocateStub) -> None:
        coord._api_push_ready.return_value = False
        ok = await coord.async_stop_sound("dev-1")
        assert ok is False
        coord.api.async_stop_sound.assert_not_called()

    async def test_uses_cached_uuid_when_none_passed(self, coord: LocateStub) -> None:
        coord._sound_request_uuids["dev-1"] = "cached-uuid"
        ok = await coord.async_stop_sound("dev-1")
        assert ok is True
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", "cached-uuid")
        # successful stop removes the uuid
        assert "dev-1" not in coord._sound_request_uuids

    async def test_explicit_uuid_overrides_cache(self, coord: LocateStub) -> None:
        coord._sound_request_uuids["dev-1"] = "cached-uuid"
        ok = await coord.async_stop_sound("dev-1", request_uuid="explicit")
        assert ok is True
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", "explicit")

    async def test_missing_uuid_warns_but_attempts_stop(
        self, coord: LocateStub
    ) -> None:
        ok = await coord.async_stop_sound("dev-1")
        assert ok is True
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", None)

    async def test_failure_notes_problem(self, coord: LocateStub) -> None:
        coord.api.async_stop_sound.return_value = False
        ok = await coord.async_stop_sound("dev-1", request_uuid="x")
        assert ok is False
        coord._note_push_transport_problem.assert_called_once()


_ = DEFAULT_MIN_POLL_INTERVAL  # silence unused-import lint when production no-ops

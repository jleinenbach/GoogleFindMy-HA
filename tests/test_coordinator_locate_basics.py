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
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.googlefindmy.const import (
    DEFAULT_MIN_POLL_INTERVAL,
    StopSoundOutcome,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyLookupTransientError,
    SharedKeyMismatchError,
    StaleOwnerKeyError,
)
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


class TestAsyncLocateDeviceDecryptFailure:
    """Codex P2: manual locate must handle stale/missing shared-key failures the
    same way the poll path does (feed the shared escalation counter, start reauth),
    not swallow ``DecryptionError`` into a generic ``HomeAssistantError``."""

    @pytest.fixture(autouse=True)
    def _pass_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Move the clock past every cooldown gate so the Nova call is reached."""
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.locate.time.monotonic",
            lambda: 1000.0,
        )

    async def test_stale_owner_key_records_per_tracker_no_reauth(
        self, coord: LocateStub
    ) -> None:
        """A per-tracker StaleOwnerKeyError feeds note_decrypt_failure(stale=True),
        never starts an account reauth, and returns an empty result."""
        coord.note_decrypt_failure = MagicMock(return_value=False)
        coord.config_entry.async_start_reauth = MagicMock()
        coord.api.async_get_device_location.side_effect = StaleOwnerKeyError(
            "tracker v1 < v2"
        )

        result = await coord.async_locate_device("dev-1")

        assert result == {}
        coord.note_decrypt_failure.assert_called_once()
        assert coord.note_decrypt_failure.call_args.kwargs.get("stale") is True
        coord.config_entry.async_start_reauth.assert_not_called()

    async def test_decrypt_failure_below_threshold_returns_empty(
        self, coord: LocateStub
    ) -> None:
        """Below the escalation threshold the manual locate degrades gracefully to
        an empty result (mirrors the poll path that keeps polling), and must not
        start a reauth flow."""
        coord.note_decrypt_failure = MagicMock(return_value=False)
        coord.config_entry.async_start_reauth = MagicMock()
        coord.api.async_get_device_location.side_effect = SharedKeyMismatchError(
            "stale shared key"
        )

        result = await coord.async_locate_device("dev-1")

        assert result == {}
        coord.note_decrypt_failure.assert_called_once()
        assert coord.note_decrypt_failure.call_args.kwargs.get("stale") is False
        coord.config_entry.async_start_reauth.assert_not_called()

    async def test_transient_owner_key_lookup_returns_empty_no_reauth_counter(
        self, coord: LocateStub
    ) -> None:
        """R4/AP5: a transient owner-key lookup miss during a manual locate degrades
        to an empty result WITHOUT feeding the account-wide reauth counter.

        ``OwnerKeyLookupTransientError`` is not a ``DecryptionError``, so the
        dedicated ``except OwnerKeyLookupTransientError`` block (before the
        ``DecryptionError`` handler) skips the device without calling
        ``note_decrypt_failure`` and never starts a reauth flow.
        """
        coord.note_decrypt_failure = MagicMock(return_value=False)
        coord.config_entry.async_start_reauth = MagicMock()
        coord.api.async_get_device_location.side_effect = OwnerKeyLookupTransientError(
            "Owner key retrieval did not complete (transient)."
        )

        result = await coord.async_locate_device("dev-1")

        assert result == {}
        coord.note_decrypt_failure.assert_not_called()
        coord._set_auth_state.assert_not_called()
        coord.config_entry.async_start_reauth.assert_not_called()

    async def test_decrypt_failure_at_threshold_starts_reauth(
        self, coord: LocateStub
    ) -> None:
        """When the shared counter says escalate (note_decrypt_failure True), manual
        locate marks the auth state failed, starts the reauth flow, and surfaces a
        user-facing HomeAssistantError instead of an empty result."""
        coord.note_decrypt_failure = MagicMock(return_value=True)
        coord.config_entry.async_start_reauth = MagicMock()
        coord.api.async_get_device_location.side_effect = DecryptionError(
            "shared key gone"
        )

        with pytest.raises(HomeAssistantError) as excinfo:
            await coord.async_locate_device("dev-1")

        assert "re-authentication has been started" in str(excinfo.value)
        coord._set_auth_state.assert_called_once()
        assert coord._set_auth_state.call_args.kwargs.get("failed") is True
        coord.config_entry.async_start_reauth.assert_called_once()


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
        outcome = await coord.async_stop_sound("dev-1")
        # A suppressed stop was never sent, so it is a failure, not a silent
        # "uncorrelated". The service layer has to raise on it.
        assert outcome is StopSoundOutcome.FAILED
        coord.api.async_stop_sound.assert_not_called()

    async def test_uses_cached_uuid_when_none_passed(self, coord: LocateStub) -> None:
        coord._sound_request_uuids["dev-1"] = "cached-uuid"
        outcome = await coord.async_stop_sound("dev-1")
        assert outcome is StopSoundOutcome.CANCELLED
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", "cached-uuid")
        # successful stop removes the uuid
        assert "dev-1" not in coord._sound_request_uuids

    async def test_explicit_uuid_does_not_drop_our_own_cached_key(
        self, coord: LocateStub
    ) -> None:
        """A foreign cancel key must not evict our own handle.

        The caller may pass the key of a *different* ring (that is precisely the
        BSkando#195 scenario). Popping our cached key on its behalf would throw
        away the only handle for a ring that may still be running.
        """

        coord._sound_request_uuids["dev-1"] = "cached-uuid"
        outcome = await coord.async_stop_sound("dev-1", request_uuid="explicit")
        assert outcome is StopSoundOutcome.CANCELLED
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", "explicit")
        assert coord._sound_request_uuids["dev-1"] == "cached-uuid"

    async def test_missing_uuid_reports_uncorrelated(self, coord: LocateStub) -> None:
        outcome = await coord.async_stop_sound("dev-1")
        # Submitted, but nothing proves an effect.
        assert outcome is StopSoundOutcome.UNCORRELATED
        coord.api.async_stop_sound.assert_awaited_once_with("dev-1", None)

    async def test_failure_notes_problem(self, coord: LocateStub) -> None:
        coord.api.async_stop_sound.return_value = False
        outcome = await coord.async_stop_sound("dev-1", request_uuid="x")
        assert outcome is StopSoundOutcome.FAILED
        coord._note_push_transport_problem.assert_called_once()

    async def test_failed_stop_keeps_a_fresh_cancel_key(
        self, coord: LocateStub
    ) -> None:
        """IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY: a rejected stop spends nothing."""

        coord._sound_request_uuids["dev-1"] = "cached-uuid"
        coord.api.async_stop_sound.return_value = False
        outcome = await coord.async_stop_sound("dev-1")
        assert outcome is StopSoundOutcome.FAILED
        assert coord._sound_request_uuids["dev-1"] == "cached-uuid"


_ = DEFAULT_MIN_POLL_INTERVAL  # silence unused-import lint when production no-ops

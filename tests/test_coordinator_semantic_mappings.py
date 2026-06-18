from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryAuthFailed

from custom_components.googlefindmy.const import OPT_SEMANTIC_LOCATIONS
from custom_components.googlefindmy.coordinator import (
    CryptoStatus,
    GoogleFindMyCoordinator,
)
from custom_components.googlefindmy.coordinator.polling import (
    _MAX_DECRYPT_FAILURES,
    _MAX_TRANSIENT_AUTH_FAILURES,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    SharedKeyMismatchError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaAuthPermanentError,
)
from tests.helpers.homeassistant import GoogleFindMyConfigEntryStub


class _DummyAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def async_get_device_location(
        self, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return dict(self._payload)


class _TrackingFilter:
    def __init__(
        self, should_filter: bool = False, replacement: dict[str, float] | None = None
    ) -> None:
        self.should_filter = should_filter
        self.replacement = replacement
        self.called = 0

    def should_filter_detection(
        self, *_args: Any, **_kwargs: Any
    ) -> tuple[bool, dict[str, float] | None]:
        self.called += 1
        return self.should_filter, self.replacement


class _RaisingFilter:
    def __init__(self) -> None:
        self.called = 0

    def should_filter_detection(
        self, *_args: Any, **_kwargs: Any
    ) -> tuple[bool, dict[str, float] | None]:
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
    coordinator._should_preserve_precise_home_coordinates = (
        lambda *_args, **_kwargs: False
    )
    coordinator._normalize_coords = lambda *_args, **_kwargs: True
    coordinator._is_significant_update = lambda *_args, **_kwargs: True
    coordinator.update_device_cache = lambda *_args, **_kwargs: None
    coordinator._set_auth_state = lambda **_kwargs: None
    coordinator._device_location_data = {}
    coordinator._device_poll_cooldown_until = {}
    coordinator._present_last_seen = {}
    coordinator._locate_inflight = set()
    coordinator._locate_cooldown_until = {}
    coordinator._device_action_locks = {}
    coordinator._device_update_history = {}
    coordinator._last_poll_mono = 0.0
    coordinator._consecutive_timeouts = 0
    coordinator._consecutive_transient_auth_failures = 0
    coordinator._last_transient_auth_error = None
    coordinator._consecutive_decrypt_failures = 0
    coordinator._last_decrypt_reauth_monotonic = None
    coordinator._last_decrypt_error = None
    coordinator._crypto_status_state = CryptoStatus.UNKNOWN
    coordinator._crypto_status_reason = None
    coordinator._crypto_status_changed_at = None
    # Deliberately low monotonic value: simulates a freshly booted runner (process
    # uptime below the reauth cooldown). With the None sentinel the first escalation
    # must fire regardless, so this pins the whole loop-test class against the
    # uptime-dependent flake that slipped through CI.
    coordinator._monotonic = lambda: 100.0
    coordinator.location_poll_interval = 0
    coordinator.data = []
    coordinator._last_device_list = []
    coordinator._get_ignored_set = set
    coordinator._build_snapshot_from_cache = lambda *_args, **_kwargs: []
    coordinator._get_google_home_filter = lambda: google_filter
    coordinator.api = _DummyAPI(api_payload)
    coordinator.get_device_display_name = lambda device_id: device_id
    coordinator.can_request_location = lambda _device_id: True
    coordinator._api_push_ready = lambda: True
    coordinator._is_on_hass_loop = lambda: True
    coordinator._semantic_label_cache = {}
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


@pytest.mark.parametrize(
    "api_name",
    ["living room", "Living Room", "Near Living Room"],
)
def test_semantic_mapping_normalizes_api_names(api_name: str) -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "Living Room": {"latitude": 8.5, "longitude": 9.5, "accuracy": 10.0}
        }
    }
    coordinator = _push_coordinator(options)

    coordinator.update_device_cache(
        "dev-norm",
        {
            "semantic_name": api_name,
            "last_seen": 10,
        },
    )

    cached = coordinator._device_location_data["dev-norm"]
    assert cached["latitude"] == pytest.approx(8.5)
    assert cached["longitude"] == pytest.approx(9.5)
    assert cached["accuracy"] == pytest.approx(10.0)


def test_semantic_mapping_rejects_partial_matches() -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "Kitchen": {"latitude": 3.0, "longitude": 4.0, "accuracy": 5.0}
        }
    }
    coordinator = _push_coordinator(options)

    coordinator.update_device_cache(
        "dev-partial",
        {"semantic_name": "Kitchen 2", "last_seen": 20},
    )

    cached = coordinator._device_location_data["dev-partial"]
    assert cached.get("latitude") is None
    assert cached.get("longitude") is None
    assert cached.get("semantic_name") == "Kitchen 2"


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
    coordinator._schedule_eid_resolver_refresh = lambda: None
    coordinator.note_error = lambda *_args, **_kwargs: None
    coordinator.async_set_update_error = lambda *_args, **_kwargs: None
    coordinator._last_poll_result = None
    coordinator._startup_complete = True
    coordinator._fcm_defer_started_mono = 0.0
    coordinator._fcm_last_stage = 0
    coordinator.device_poll_delay = 0
    coordinator.safe_update_metric = lambda *_args, **_kwargs: None
    return coordinator


def _push_coordinator(options: dict[str, Any]) -> GoogleFindMyCoordinator:
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.config_entry = GoogleFindMyConfigEntryStub(options=options)
    coordinator.hass = SimpleNamespace()
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._is_significant_update = lambda *_args, **_kwargs: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._device_location_data = {}
    coordinator._device_name_cache = {}
    coordinator._device_update_history = {}
    coordinator._device_poll_cooldown_until = {}
    coordinator._present_last_seen = {}
    coordinator._semantic_label_cache = {}
    return coordinator


@pytest.mark.asyncio
async def test_poll_cycle_applies_mapping_before_spam_filter() -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "HomeHub": {"latitude": 10.0, "longitude": 20.0, "accuracy": 15.0}
        }
    }
    google_filter = _RaisingFilter()
    coordinator = _polling_coordinator(
        options, google_filter, {"semantic_name": "homehub"}
    )

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


@pytest.mark.asyncio
async def test_poll_cycle_preserves_coordinates_and_updates_semantic_name() -> None:
    google_filter = _TrackingFilter(should_filter=False)
    coordinator = _polling_coordinator(
        {}, google_filter, {"semantic_name": "Unknown Room", "last_seen": 200}
    )
    coordinator._device_location_data["dev-hybrid"] = {
        "latitude": 50.0,
        "longitude": 10.0,
        "accuracy": 5.0,
        "last_seen": 100.0,
    }

    await coordinator._async_start_poll_cycle(
        [{"id": "dev-hybrid", "name": "Hybrid Device"}]
    )

    cached = coordinator._device_location_data["dev-hybrid"]
    assert cached["latitude"] == pytest.approx(50.0)
    assert cached["longitude"] == pytest.approx(10.0)
    assert cached["semantic_name"] == "Unknown Room"
    assert cached["last_seen"] == pytest.approx(200)


def test_push_cache_applies_semantic_mapping() -> None:
    options = {
        OPT_SEMANTIC_LOCATIONS: {
            "Lobby": {"latitude": 5.0, "longitude": 6.0, "accuracy": 7.5}
        }
    }
    coordinator = _push_coordinator(options)

    coordinator.update_device_cache(
        "dev-3", {"semantic_name": "lobby", "last_seen": 1234}
    )

    cached = coordinator._device_location_data["dev-3"]
    assert cached["latitude"] == pytest.approx(5.0)
    assert cached["longitude"] == pytest.approx(6.0)
    assert cached["accuracy"] == pytest.approx(7.5)
    assert cached["location_type"] == "trusted"


@pytest.mark.asyncio
async def test_semantic_labels_are_recorded_with_device_ids() -> None:
    google_filter = _TrackingFilter(should_filter=False)
    coordinator = _polling_coordinator(
        {},
        google_filter,
        {
            "semantic_name": "Lobby",
            "latitude": 1.0,
            "longitude": 2.0,
            "accuracy": 3.0,
        },
    )

    await coordinator._async_start_poll_cycle([{"id": "dev-3", "name": "Device"}])

    observations = coordinator.get_observed_semantic_labels()
    assert [obs.label for obs in observations] == ["Lobby"]
    assert observations[0].devices == {"dev-3"}


class _DecryptFailAPI:
    """API stub whose location lookup always raises the given decryption error.

    Used to drive the coordinator poll loop through the decrypt-failure handlers
    end to end (the isolated escalation logic is unit-tested separately in
    tests/test_coordinator_decrypt_reauth_escalation.py).
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def async_get_device_location(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise self._exc


@pytest.mark.asyncio
async def test_poll_cycle_escalates_persistent_decrypt_failure_to_reauth() -> None:
    """End-to-end: a persistent account-wide stale shared key escalates the poll
    loop to ConfigEntryAuthFailed after _MAX_DECRYPT_FAILURES cycles, so Home
    Assistant shows the reauth card. The cycles before the threshold must NOT
    escalate (self-heal still gets a chance)."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(SharedKeyMismatchError("stale shared key"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)

    # Below threshold: keep polling, no reauth escalation yet.
    for _ in range(_MAX_DECRYPT_FAILURES - 1):
        await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert not any(kw.get("failed") for kw in auth_calls)
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1

    # Threshold cycle: escalate to a reauth flow. The poll cycle runs as a
    # fire-and-forget task, so it starts the entry reauth flow directly rather
    # than raising (a raised ConfigEntryAuthFailed would never reach the awaited
    # coordinator refresh and HA's automatic reauth would never fire).
    coordinator.config_entry.async_start_reauth = MagicMock()
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)
    assert any(kw.get("failed") for kw in auth_calls)


@pytest.mark.asyncio
async def test_poll_cycle_stale_owner_key_never_escalates() -> None:
    """End-to-end: a per-tracker StaleOwnerKeyError must never escalate to an
    account-wide reauth (an account reauth would not fix one outdated tracker) and
    must not drive the account-wide decrypt counter."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(StaleOwnerKeyError("tracker v1 < v2"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)

    for _ in range(_MAX_DECRYPT_FAILURES + 2):
        await coordinator._async_start_poll_cycle([{"id": "tag", "name": "Tag"}])

    assert coordinator._consecutive_decrypt_failures == 0
    assert not any(kw.get("failed") for kw in auth_calls)


class _DecryptThenSucceedAPI:
    """Raises a decryption error for the first ``fail_times`` calls, then succeeds.

    Used to prove the consecutive-decrypt-failure counter resets on a successful
    cycle (self-heal / recovered shared key), so a later isolated failure does not
    trip a premature reauth.
    """

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc = exc
        self._fail = fail_times
        self.calls = 0

    async def async_get_device_location(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self._fail:
            raise self._exc
        # A genuinely decryptable report: positive proof the account-wide shared
        # key works again. Only such a real decrypt clears the counter -- an empty
        # / no-reporter response (falsy) is an idle outcome and must NOT (see
        # test_poll_cycle_idle_does_not_reset_decrypt_counter).
        return {"latitude": 1.0, "longitude": 2.0, "accuracy": 5.0}


@pytest.mark.asyncio
async def test_poll_cycle_decrypt_counter_resets_on_success() -> None:
    """A successful poll cycle clears the consecutive-decrypt-failure counter, so a
    recovered shared key (or self-heal) does not later trip a spurious reauth."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptThenSucceedAPI(
        SharedKeyMismatchError("transient stale"), fail_times=_MAX_DECRYPT_FAILURES - 1
    )

    # Below-threshold failing cycles raise no reauth and accumulate the counter.
    for _ in range(_MAX_DECRYPT_FAILURES - 1):
        await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1

    # A successful cycle resets the counter back to zero.
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator._consecutive_decrypt_failures == 0


class _DecryptThenIdleAPI:
    """Raises a decryption error for the first ``fail_times`` calls, then returns
    an empty (no-reporter) result.

    Models an intermittently reporting BLE tracker: failing decrypt cycles are
    interleaved with idle cycles that simply return no location data. Such idle
    cycles carry no positive proof the shared key recovered and must therefore
    NOT clear the accumulated decrypt-failure counter.
    """

    def __init__(self, exc: Exception, fail_times: int) -> None:
        self._exc = exc
        self._fail = fail_times
        self.calls = 0

    async def async_get_device_location(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        self.calls += 1
        if self.calls <= self._fail:
            raise self._exc
        return []  # idle: no reporter in range, nothing decrypted this cycle


@pytest.mark.asyncio
async def test_poll_cycle_idle_does_not_reset_decrypt_counter() -> None:
    """An idle cycle (empty/no-reporter result) must NOT clear accumulated decrypt
    failures: without a real successful decrypt there is no proof the stale shared
    key recovered. Otherwise an intermittently reporting BLE tracker would let idle
    cycles perpetually reset the counter, so the reauth threshold is never reached
    and the user stays stuck with no location updates and no reauth prompt
    (Codex finding on PR #182)."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptThenIdleAPI(
        SharedKeyMismatchError("transient stale"), fail_times=_MAX_DECRYPT_FAILURES - 1
    )

    # Below-threshold failing cycles accumulate the counter without escalating.
    for _ in range(_MAX_DECRYPT_FAILURES - 1):
        await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1

    # An idle cycle in between must leave the counter intact (not reset to zero).
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1


class _PerDeviceDecryptAPI:
    """Raises a decryption error for one device id, succeeds (empty) for others."""

    def __init__(self, exc: Exception, failing_id: str) -> None:
        self._exc = exc
        self._failing = failing_id

    async def async_get_device_location(
        self, device_id: str, _device_name: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        if device_id == self._failing:
            raise self._exc
        return {}


@pytest.mark.asyncio
async def test_poll_cycle_partial_decrypt_failure_still_escalates() -> None:
    """Multi-device: one tracker decrypts fine while another keeps failing with a
    stale account-wide shared key. A per-device reset would null the counter every
    cycle (one healthy tracker masking the others) and never escalate; the
    cycle-gated reset preserves the count so escalation still fires after the
    threshold. Regression guard for the independent-review finding."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _PerDeviceDecryptAPI(
        SharedKeyMismatchError("stale shared key"), failing_id="bad"
    )
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)
    devices = [{"id": "good", "name": "Good"}, {"id": "bad", "name": "Bad"}]

    for _ in range(_MAX_DECRYPT_FAILURES - 1):
        await coordinator._async_start_poll_cycle(devices)
    # The healthy device must NOT have reset the account-wide counter.
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1

    coordinator.config_entry.async_start_reauth = MagicMock()
    await coordinator._async_start_poll_cycle(devices)
    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)
    assert any(kw.get("failed") for kw in auth_calls)


@pytest.mark.asyncio
async def test_poll_cycle_counts_multi_device_decrypt_failure_once() -> None:
    """Codex P2 regression: one poll cycle in which several trackers all hit the
    same account-wide stale shared key must advance the consecutive-failure counter
    exactly ONCE, not once per device.

    Counting per device let a multi-device account cross _MAX_DECRYPT_FAILURES
    within the first cycle and escalate to reauth immediately, defeating the
    documented "N consecutive cycles" budget that gives the in-decrypt self-heal a
    few cycles before re-authentication."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(SharedKeyMismatchError("stale shared key"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)
    # More failing trackers in a single cycle than the escalation threshold: a
    # per-device counter would already escalate here.
    devices = [
        {"id": "dev-1", "name": "One"},
        {"id": "dev-2", "name": "Two"},
        {"id": "dev-3", "name": "Three"},
    ]
    assert len(devices) >= _MAX_DECRYPT_FAILURES

    # First cycle: counter advances by one, no escalation despite 3 failing devices.
    await coordinator._async_start_poll_cycle(devices)
    assert coordinator._consecutive_decrypt_failures == 1
    assert not any(kw.get("failed") for kw in auth_calls)

    # Second cycle: still below threshold (2/3).
    await coordinator._async_start_poll_cycle(devices)
    assert coordinator._consecutive_decrypt_failures == _MAX_DECRYPT_FAILURES - 1
    assert not any(kw.get("failed") for kw in auth_calls)

    # Third consecutive cycle reaches the threshold -> escalate exactly once.
    coordinator.config_entry.async_start_reauth = MagicMock()
    await coordinator._async_start_poll_cycle(devices)
    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)
    assert any(kw.get("failed") for kw in auth_calls)


@pytest.mark.asyncio
async def test_poll_cycle_nova_permanent_auth_starts_reauth() -> None:
    """A permanent Nova auth failure in the background poll cycle starts the entry
    reauth flow directly. The cycle runs as a fire-and-forget task
    (``hass.async_create_task``), so a raised ConfigEntryAuthFailed would never reach
    the awaited coordinator refresh and HA's automatic reauth would never fire."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(NovaAuthPermanentError(401, "perm"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)
    coordinator.config_entry.async_start_reauth = MagicMock()

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)
    assert any(kw.get("failed") for kw in auth_calls)


@pytest.mark.asyncio
async def test_poll_cycle_transient_nova_auth_starts_reauth_after_threshold() -> None:
    """A transient Nova auth failure escalates only after
    _MAX_TRANSIENT_AUTH_FAILURES consecutive cycles, then starts the entry reauth
    flow directly (the fire-and-forget poll task cannot raise into HA)."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(NovaAuthError(401, "transient"))
    coordinator.config_entry.async_start_reauth = MagicMock()

    # Below threshold: keep polling, no reauth escalation yet.
    for _ in range(_MAX_TRANSIENT_AUTH_FAILURES - 1):
        await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    coordinator.config_entry.async_start_reauth.assert_not_called()

    # Threshold cycle escalates exactly once.
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)


@pytest.mark.asyncio
async def test_poll_cycle_reauth_noop_without_config_entry() -> None:
    """Defensive guard: with no config entry bound the poll cycle cannot start
    reauth, but it must not crash (``_request_poll_reauth`` no-entry branch)."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(SharedKeyMismatchError("stale shared key"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)
    coordinator.config_entry = None

    # Drive to the escalation threshold; the helper hits its no-entry guard.
    for _ in range(_MAX_DECRYPT_FAILURES):
        await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    # No crash, and the auth state was still flagged at the threshold cycle.
    assert any(kw.get("failed") for kw in auth_calls)


@pytest.mark.asyncio
async def test_poll_cycle_direct_config_entry_auth_failed_starts_reauth() -> None:
    """A ConfigEntryAuthFailed raised directly by the API (e.g. HTTP 401/403 in
    async_get_device_location) reaches the poll loop's ConfigEntryAuthFailed handler
    without passing through the typed SpotAuth/NovaAuth handlers. It must start the
    entry reauth flow directly rather than re-raising into the fire-and-forget poll
    task, where the exception would never reach HA."""
    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(ConfigEntryAuthFailed("session expired"))
    auth_calls: list[dict[str, Any]] = []
    coordinator._set_auth_state = lambda **kwargs: auth_calls.append(kwargs)
    coordinator.config_entry.async_start_reauth = MagicMock()

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    coordinator.config_entry.async_start_reauth.assert_called_once_with(coordinator.hass)
    assert any(kw.get("failed") for kw in auth_calls)


class _MixedDecryptAPI:
    """API stub that fails for one device id and returns a payload for others.

    Drives a single poll cycle that contains both a per-tracker stale key and a
    healthy, decrypting device, to prove the crypto sensor does not flicker the
    tracker_key_outdated state to OK when a sibling decrypts successfully.
    """

    def __init__(self, fail_id: str, exc: Exception, ok_payload: dict[str, Any]) -> None:
        self._fail_id = fail_id
        self._exc = exc
        self._ok_payload = ok_payload

    async def async_get_device_location(
        self, device_id: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        if device_id == self._fail_id:
            raise self._exc
        return dict(self._ok_payload)


@pytest.mark.asyncio
async def test_poll_cycle_successful_decrypt_sets_crypto_ok() -> None:
    """A cycle that decrypts usable location data reports crypto OK (D6 source)."""

    coordinator = _polling_coordinator(
        {}, _TrackingFilter(), {"latitude": 1.0, "longitude": 2.0, "last_seen": 100}
    )

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    assert coordinator.crypto_status.state == CryptoStatus.OK


@pytest.mark.asyncio
async def test_poll_cycle_shared_key_failure_sets_crypto_invalid() -> None:
    """An account-wide SharedKeyMismatchError surfaces as shared_key_invalid."""

    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(SharedKeyMismatchError("stale shared key"))
    coordinator._set_auth_state = lambda **_kwargs: None

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    assert coordinator.crypto_status.state == CryptoStatus.SHARED_KEY_INVALID


@pytest.mark.asyncio
async def test_poll_cycle_stale_key_sets_tracker_outdated() -> None:
    """A per-tracker StaleOwnerKeyError surfaces as tracker_key_outdated."""

    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _DecryptFailAPI(StaleOwnerKeyError("tracker v1 < v2"))
    coordinator._set_auth_state = lambda **_kwargs: None

    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])

    assert coordinator.crypto_status.state == CryptoStatus.TRACKER_KEY_OUTDATED


@pytest.mark.asyncio
async def test_poll_cycle_timeout_only_does_not_clear_prior_failure() -> None:
    """A cycle without any successful decrypt must NOT downgrade a prior
    shared_key_invalid to OK -- absence of an error is not proof of health.

    Mutation guard for the ``cycle_had_successful_decrypt`` gate: replacing it
    with the weaker ``devices`` condition turns the second assertion red.
    """

    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator._set_auth_state = lambda **_kwargs: None

    # Cycle 1: account-wide failure -> shared_key_invalid (below threshold).
    coordinator.api = _DecryptFailAPI(SharedKeyMismatchError("stale shared key"))
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator.crypto_status.state == CryptoStatus.SHARED_KEY_INVALID

    # Cycle 2: idle (empty location, no successful decrypt) -> state unchanged.
    coordinator.api = _DummyAPI({})
    await coordinator._async_start_poll_cycle([{"id": "dev-1", "name": "Hub"}])
    assert coordinator.crypto_status.state == CryptoStatus.SHARED_KEY_INVALID


@pytest.mark.asyncio
async def test_poll_cycle_mixed_stale_and_success_keeps_tracker_outdated() -> None:
    """A healthy sibling in a cycle that also hit a stale key must NOT flicker the
    sensor to OK.

    Mutation guard for the ``not cycle_had_stale_key`` gate: dropping it lets the
    successful sibling overwrite tracker_key_outdated with OK and turns this red.
    """

    coordinator = _polling_coordinator({}, _TrackingFilter(), {})
    coordinator.api = _MixedDecryptAPI(
        "dev-stale",
        StaleOwnerKeyError("tracker v1 < v2"),
        {"latitude": 1.0, "longitude": 2.0, "last_seen": 100},
    )
    coordinator._set_auth_state = lambda **_kwargs: None

    await coordinator._async_start_poll_cycle(
        [{"id": "dev-stale", "name": "Old"}, {"id": "dev-ok", "name": "Hub"}]
    )

    assert coordinator.crypto_status.state == CryptoStatus.TRACKER_KEY_OUTDATED

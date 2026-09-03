# tests/test_coordinator_status.py
"""Regression tests for coordinator status handling (API vs. FCM)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir

from custom_components.googlefindmy._reauth_reason import ReauthReasonCode
from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
    CRASH_LOOP_FATAL_PREFIX,
)
from custom_components.googlefindmy.binary_sensor import GoogleFindMyPollingSensor
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DOMAIN,
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    ISSUE_AUTH_EXPIRED_KEY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
    issue_id_for,
)
from custom_components.googlefindmy.coordinator import (
    _FCM_FALLBACK_POLL_AFTER_S,
    ApiStatus,
    FcmStatus,
    GoogleFindMyCoordinator,
)
from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker
from tests.helpers import drain_loop


class _DummyBus:
    """Capture fired events for assertions."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict | None]] = []

    def async_fire(self, event: str, data: dict | None = None) -> None:
        self.fired.append((event, data))


class _DummyConfigEntries:
    """Stub Home Assistant config_entries manager.

    The real ``ConfigEntries`` manager exposes no ``async_start_reauth``
    helper -- reauth is entry-scoped via ``ConfigEntry.async_start_reauth``.
    The stub therefore deliberately omits it: a stray production call to
    ``hass.config_entries.async_start_reauth(...)`` raises ``AttributeError``
    here exactly as it would at runtime, instead of being silently recorded
    by a fictional method.
    """

    def __init__(self) -> None:
        self.setup_calls: list[str] = []

    def async_get_subentries(self, _entry_id: str) -> list[Any]:
        return []

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


class _DummyEntry:
    """Minimal ConfigEntry stub with async_start_reauth helper."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.data = {CONF_GOOGLE_EMAIL: "user@example.com"}
        self.reauth_calls = 0

    def async_start_reauth(self, hass) -> None:  # noqa: D401 - stub signature
        # Mirror the real ``ConfigEntry.async_start_reauth``: a synchronous
        # ``@callback`` returning ``None`` (an async stub would mask a stray
        # ``await`` in production code).
        self.reauth_calls += 1


class _DummyHass:
    """Minimal Home Assistant stub for coordinator tests."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()
        self.config_entries = _DummyConfigEntries()
        self.data: dict[str, dict] = {DOMAIN: {}}

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub
        return self.loop.create_task(coro, name=name)


class _DummyAPI:
    """Minimal API stub implementing the methods touched in tests."""

    def __init__(self) -> None:
        self.raise_auth = False
        self.device_list: list[dict[str, str]] = []
        self.fcm: Any | None = None

    async def async_get_basic_device_list(self) -> list[dict[str, str]]:
        if self.raise_auth:
            raise ConfigEntryAuthFailed("Invalid auth token")
        return list(self.device_list)

    def is_push_ready(self) -> bool:
        return True


class _DummyCache:
    """Entry-scoped cache stub providing async get/set helpers."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        """Return a stored value or None when absent."""

        return self._store.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        """Persist a value in the in-memory store."""

        self._store[key] = value

    async def get(self, key: str) -> Any:  # pragma: no cover - legacy compatibility
        """Compatibility alias for older tests still awaiting get()."""

        return await self.async_get_cached_value(key)


@pytest.fixture
def dummy_api(monkeypatch: pytest.MonkeyPatch) -> _DummyAPI:
    """Provide a DummyAPI instance injected into the coordinator under test."""

    api = _DummyAPI()

    def _factory(*_args, **_kwargs) -> _DummyAPI:
        return api

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.GoogleFindMyAPI",
        _factory,
    )
    return api


@pytest.fixture
def coordinator(
    monkeypatch: pytest.MonkeyPatch, dummy_api: _DummyAPI
) -> GoogleFindMyCoordinator:
    """Instantiate a coordinator with lightweight stubs for hass/cache."""

    loop = asyncio.new_event_loop()
    hass = _DummyHass(loop)
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    coord = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coord.config_entry = _DummyEntry()
    coord.async_set_updated_data = Mock()
    coord._async_build_device_snapshot_with_fallbacks = AsyncMock(return_value=[])
    coord._async_start_poll_cycle = AsyncMock()
    coord._ensure_registry_for_devices = lambda *_args, **_kwargs: 0
    coord._schedule_short_retry = lambda *_args, **_kwargs: None
    coord._get_ignored_set = set
    coord._is_fcm_ready_soft = lambda: True
    coord._set_fcm_status(FcmStatus.CONNECTED)
    yield coord
    drain_loop(loop)


def test_api_auth_error_preserves_fcm_status(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """ConfigEntryAuthFailed surfaces while keeping push transport marked connected."""

    dummy_api.raise_auth = True

    loop = coordinator.hass.loop
    with pytest.raises(ConfigEntryAuthFailed):
        loop.run_until_complete(coordinator._async_update_data())

    assert coordinator.api_status.state == ApiStatus.REAUTH
    assert coordinator.fcm_status.state == FcmStatus.CONNECTED
    # The coordinator must not start reauth manually; it relies on HA's
    # automatic reauth triggered by raising ConfigEntryAuthFailed. Only the
    # entry-level call exists in the real API, so that is what we guard. The
    # manager has no async_start_reauth, so a stray manager call would raise
    # AttributeError above instead of passing silently.
    assert coordinator.config_entry.reauth_calls == 0
    assert "Invalid" in (coordinator.api_status.reason or "")


def test_fatal_fcm_registration_triggers_reauth(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """A fatal FCM registration error escalates to ConfigEntryAuthFailed."""

    fatal_error = "GCM Registration failed (404): Credentials invalid"

    class _DummyFcm:
        def __init__(self, message: str) -> None:
            self._fatal_error = message
            self._fatal_errors = {coordinator.config_entry.entry_id: message}

    dummy_api.fcm = _DummyFcm(fatal_error)
    coordinator.config_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, fcm_receiver=dummy_api.fcm
    )
    coordinator._is_fcm_ready_soft = lambda: False

    loop = coordinator.hass.loop
    with pytest.raises(ConfigEntryAuthFailed):
        loop.run_until_complete(coordinator._async_update_data())

    assert coordinator.auth_error_active is True
    assert coordinator._auth_error_message == fatal_error
    assert coordinator.hass.bus.fired[-1] == (
        EVENT_AUTH_ERROR,
        {
            "entry_id": coordinator.config_entry.entry_id,
            "email": coordinator._get_account_email(),
            "message": fatal_error,
        },
    )


def test_threshold_fcm_fatal_tags_reauth_code(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """FIX 3: a non-immediate FCM fatal that persists to the retry threshold
    escalates to ConfigEntryAuthFailed tagged with FCM_AUTH_FATAL."""

    # Non-auth, non-crash-loop fatal ⇒ counter path, not the immediate raise.
    fatal_error = "Transient FCM error: connection reset"

    class _DummyFcm:
        def __init__(self, message: str) -> None:
            self._fatal_error = message
            self._fatal_errors = {coordinator.config_entry.entry_id: message}

    dummy_api.fcm = _DummyFcm(fatal_error)
    coordinator.config_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, fcm_receiver=dummy_api.fcm
    )
    coordinator._is_fcm_ready_soft = lambda: False
    # One short of the threshold; the same recurring fatal tips it over.
    coordinator._fcm_last_error = fatal_error
    coordinator._fcm_error_count = 2

    loop = coordinator.hass.loop
    with pytest.raises(ConfigEntryAuthFailed) as excinfo:
        loop.run_until_complete(coordinator._async_update_data())

    # Mutation-sharp: the wrong (or missing) tag fails this identity check.
    assert excinfo.value.reauth_code is ReauthReasonCode.FCM_AUTH_FATAL


def test_global_fatal_error_ignored_for_clean_entry(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """A global fatal flag should not trip reauth when entry-scoped errors are clear."""

    fatal_error = "Global fail"

    class _DummyFcm:
        def __init__(self, message: str) -> None:
            self._fatal_error = message
            self._fatal_errors: dict[str, str] = {}

    dummy_api.fcm = _DummyFcm(fatal_error)
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator.config_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, fcm_receiver=dummy_api.fcm
    )

    loop = coordinator.hass.loop
    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []
    assert coordinator.auth_error_active is False
    assert coordinator.hass.bus.fired == []


# --------------------------------------------------------------------------
# AP7 / CA-6: re-auth-escalation dispatch wiring for the crash-loop fatal.
#
# cov-integ's ``test_coordinator_polling_fatal_channel.py`` only pins the
# pure ``_classify_fcm_fatal`` function (RV-G3q scenarios a+b). It never
# drives ``_async_update_data``, so a regression that re-wires the
# CRASH_LOOP_EXCLUDED branch back into the re-auth path (the #1086
# channel-confusion bug, RV-G3q) would not turn any cov-integ test red.
# These two tests pin the consumer-branch wiring (RV-G3q scenarios c+d):
#   (c) no ConfigEntryAuthFailed / no auth-error flip for a cap fatal;
#   (d) a crash-loop fatal resets a stale transient auth-error count.
# --------------------------------------------------------------------------


def test_crash_loop_fatal_excluded_from_reauth_escalation(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """AP7/CA-6 (c): a supervisor short-run-cap fatal must NOT escalate to re-auth.

    The FCM receiver's poison-message cap publishes its terminal state into
    the same ``_fatal_errors`` map the re-auth escalation consumes. Polling
    ``_async_update_data`` well past ``_FCM_ERROR_RETRY_THRESHOLD`` with a
    ``CRASH_LOOP_FATAL_PREFIX`` fatal must never raise ConfigEntryAuthFailed
    nor flip the auth-error state.
    """
    fatal_error = f"{CRASH_LOOP_FATAL_PREFIX} entry abc stopped after 10 short runs"

    class _DummyFcm:
        def __init__(self, message: str) -> None:
            self._fatal_error = message
            self._fatal_errors = {coordinator.config_entry.entry_id: message}

    dummy_api.fcm = _DummyFcm(fatal_error)
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator.config_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, fcm_receiver=dummy_api.fcm
    )
    coordinator._is_fcm_ready_soft = lambda: False

    loop = coordinator.hass.loop
    # Five cycles is comfortably past the threshold (3); no escalation allowed.
    for _ in range(5):
        result = loop.run_until_complete(coordinator._async_update_data())
        assert result == []

    assert coordinator.auth_error_active is False
    assert coordinator.hass.bus.fired == []
    assert coordinator._fcm_error_count == 0


def test_crash_loop_fatal_resets_stale_auth_error_count(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """AP7/CA-6 (d): a crash-loop fatal resets a stale transient auth-error count.

    A partial count left over from earlier transient (non-cap) auth fatals
    must not let a later real auth fatal inherit the crash-loop window's
    count and escalate prematurely.
    """
    fatal_error = f"{CRASH_LOOP_FATAL_PREFIX} entry abc stopped after 10 short runs"

    class _DummyFcm:
        def __init__(self, message: str) -> None:
            self._fatal_error = message
            self._fatal_errors = {coordinator.config_entry.entry_id: message}

    dummy_api.fcm = _DummyFcm(fatal_error)
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator.config_entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, fcm_receiver=dummy_api.fcm
    )
    coordinator._is_fcm_ready_soft = lambda: False
    # Simulate two prior transient (non-cap) auth-error cycles.
    coordinator._fcm_error_count = 2
    coordinator._fcm_last_error = "GCM Registration failed (401): transient"

    loop = coordinator.hass.loop
    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []
    assert coordinator._fcm_error_count == 0
    assert coordinator._fcm_last_error is None
    assert coordinator.auth_error_active is False


def test_api_status_recovers_after_success(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """Successful polling resets API status and clears the auth error flag."""

    # First, simulate a failure to set reauth state.
    dummy_api.raise_auth = True
    loop = coordinator.hass.loop
    with pytest.raises(ConfigEntryAuthFailed):
        loop.run_until_complete(coordinator._async_update_data())

    # Next, simulate a successful refresh.
    dummy_api.raise_auth = False
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._async_build_device_snapshot_with_fallbacks.return_value = []

    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []
    assert coordinator.api_status.state == ApiStatus.OK
    assert coordinator.api_status.reason is None
    assert coordinator.fcm_status.state == FcmStatus.CONNECTED
    assert coordinator.auth_error_active is False


def test_successful_update_clears_lingering_repair_issue(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """A successful poll clears lingering Repairs issues after a restart."""

    hass = coordinator.hass
    entry = coordinator.config_entry
    issue_id = issue_id_for(entry.entry_id)

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_AUTH_EXPIRED_KEY,
        translation_placeholders={"email": "user@example.com"},
    )

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    loop = hass.loop
    dummy_api.raise_auth = False
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._async_build_device_snapshot_with_fallbacks.return_value = []

    coordinator.data = []
    coordinator.async_set_updated_data.reset_mock()
    called_flag = {"value": False}

    def _mark_called(*_args, **_kwargs) -> None:
        called_flag["value"] = True

    coordinator.async_set_updated_data.side_effect = _mark_called
    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    ok_events = [event for event, _data in hass.bus.fired if event == EVENT_AUTH_OK]
    assert ok_events == [EVENT_AUTH_OK]
    assert coordinator.async_set_updated_data.called, (
        "Listeners should be notified after clearing issue"
    )
    assert called_flag["value"]


def test_push_updated_keeps_known_name_for_blank_snapshots(
    monkeypatch: pytest.MonkeyPatch,
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """Ensure cached display names survive blank device snapshots and push updates."""

    # Let the coordinator hand the raw device list through to the snapshot builder.
    coordinator._async_build_device_snapshot_with_fallbacks = AsyncMock(
        side_effect=lambda devices: devices
    )

    loop = coordinator.hass.loop
    dummy_api.device_list = [{"id": "dev-1", "name": "Pixel 9"}]
    loop.run_until_complete(coordinator._async_update_data())

    # Simulate a follow-up list without a usable name; cache should preserve the old label.
    dummy_api.device_list = [{"id": "dev-1", "name": ""}]
    loop.run_until_complete(coordinator._async_update_data())

    assert coordinator._device_names["dev-1"] == "Pixel 9"

    # Prepare cached location data to make push_updated build a rich snapshot.
    now = time.time()
    coordinator._device_location_data["dev-1"] = {
        "device_id": "dev-1",
        "name": "Pixel 9",
        "latitude": 37.0,
        "longitude": -122.0,
        "accuracy": 5,
        "last_updated": now,
    }

    captured: list[list[dict[str, Any]]] = []

    def _capture(snapshot: list[dict[str, Any]]) -> None:
        captured.append(snapshot)
        coordinator.data = snapshot

    coordinator.async_set_updated_data = _capture
    coordinator._is_on_hass_loop = lambda: True

    # Push a snapshot while the latest API payload still lacks a display name.
    coordinator.push_updated(["dev-1"])

    assert captured, "push_updated should publish a snapshot"
    snapshot = captured[-1]
    assert snapshot[0]["name"] == "Pixel 9"

    # Entities should continue to expose the persisted display name.
    monkeypatch.setattr(
        "custom_components.googlefindmy.entity.GoogleFindMyEntity.maybe_update_device_registry_name",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy._opt",
        lambda _entry, _key, default=None: default,
        raising=False,
    )

    subentry_identifier = coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )
    entity = GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "dev-1", "name": "Pixel 9"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    entity.hass = coordinator.hass
    entity.entity_id = "device_tracker.googlefindmy_dev_1"
    entity._handle_coordinator_update()

    # With has_entity_name=True, _attr_name is None; the entity name is derived
    # from the device registry. The coordinator cache preserves the display name.
    assert entity._attr_name is None
    assert entity._attr_has_entity_name is True
    assert entity.subentry_key == TRACKER_SUBENTRY_KEY
    assert subentry_identifier in entity.unique_id


def test_device_tracker_respects_coordinator_unavailability(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """Availability should mirror coordinator health before device checks."""

    coordinator.is_device_visible_in_subentry = lambda *_args, **_kwargs: True
    coordinator.get_device_location_data_for_subentry = lambda *_args, **_kwargs: {
        "latitude": 0.0,
        "longitude": 0.0,
    }
    coordinator.is_device_present = lambda _dev_id: True

    subentry_identifier = coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )
    tracker = GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "dev-1", "name": "Pixel 9"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    tracker.hass = coordinator.hass
    tracker.entity_id = "device_tracker.googlefindmy_dev_1"

    coordinator._last_update_success = False
    assert tracker.available is False

    coordinator._last_update_success = True
    assert tracker.available is True


def test_polling_sensor_inherits_coordinator_availability(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """Diagnostic polling sensor availability follows the coordinator."""

    subentry_identifier = coordinator.stable_subentry_identifier(
        key=SERVICE_SUBENTRY_KEY
    )
    polling = GoogleFindMyPollingSensor(
        coordinator,
        _DummyEntry(),
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=subentry_identifier,
    )
    polling.hass = coordinator.hass

    coordinator._last_update_success = False
    assert polling.available is False

    coordinator._last_update_success = True
    assert polling.available is True


def test_poll_snapshot_reuses_cached_name_for_blank_payload(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """Poll snapshots should reuse cached names when payload omits them."""

    loop = coordinator.hass.loop
    coordinator._async_build_device_snapshot_with_fallbacks = AsyncMock(
        side_effect=lambda devices: devices,
    )

    dummy_api.device_list = [{"id": "dev-1", "name": "Pixel 9"}]
    initial_snapshot = loop.run_until_complete(coordinator._async_update_data())
    assert initial_snapshot[0]["name"] == "Pixel 9"

    dummy_api.device_list = [{"id": "dev-1", "name": ""}]
    follow_up_snapshot = loop.run_until_complete(coordinator._async_update_data())

    assert follow_up_snapshot[0]["name"] == "Pixel 9"


def test_poll_cycle_forces_after_fcm_timeout(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """After the FCM grace window expires, polling should proceed in degraded mode."""

    loop = coordinator.hass.loop
    coordinator._async_build_device_snapshot_with_fallbacks.return_value = []
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]

    coordinator._enabled_poll_device_ids = {"dev-1"}
    coordinator._is_fcm_ready_soft = lambda: False
    coordinator._fcm_defer_started_mono = time.monotonic() - (
        _FCM_FALLBACK_POLL_AFTER_S + 5
    )
    coordinator._last_poll_mono = time.monotonic() - (
        coordinator.location_poll_interval + 1
    )
    coordinator._async_start_poll_cycle.reset_mock()

    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []
    coordinator._async_start_poll_cycle.assert_awaited()
    call = coordinator._async_start_poll_cycle.await_args
    assert call is not None
    assert call.args and call.args[0][0]["id"] == "dev-1"
    assert call.kwargs.get("force") is True


def test_update_skips_devices_without_valid_id(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid or duplicate device entries should be ignored during updates."""

    caplog.set_level("DEBUG")
    coordinator._async_build_device_snapshot_with_fallbacks.return_value = []
    dummy_api.device_list = [
        {},
        {"id": 123, "name": "Numeric identifier"},
        {"id": "  dev-1  ", "name": "Pixel 9"},
        {"id": "dev-1", "name": "Pixel 9 duplicate"},
        {"id": "dev-2", "name": "Pixel 9 Pro"},
    ]

    loop = coordinator.hass.loop
    result = loop.run_until_complete(coordinator._async_update_data())

    assert result == []

    await_args = coordinator._async_build_device_snapshot_with_fallbacks.await_args
    assert await_args is not None
    visible_devices = list(await_args.args[0])
    assert [dev["id"] for dev in visible_devices] == ["dev-1", "dev-2"]
    assert coordinator._present_device_ids == {"dev-1", "dev-2"}
    assert any(
        "Skipping device without valid id" in record.message
        for record in caplog.records
    )
    assert any(
        "Skipping duplicate device entry for id=dev-1" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Regression tests for push_updated() merge semantics (PR #167 + F-9).
# T1: push merges with self.data, T2: pushed device overwrites only itself,
# T3: subentry index sees the merged_snapshot (F-9, prevents stale index).
# ---------------------------------------------------------------------------


def _prime_push_updated(
    coordinator: GoogleFindMyCoordinator,
) -> list[list[dict[str, Any]]]:
    """Patch coordinator to capture snapshots published by push_updated()."""

    captured: list[list[dict[str, Any]]] = []

    def _capture(snapshot: list[dict[str, Any]]) -> None:
        captured.append(snapshot)
        coordinator.data = snapshot

    coordinator.async_set_updated_data = _capture
    coordinator._is_on_hass_loop = lambda: True
    coordinator._async_build_device_snapshot_with_fallbacks = AsyncMock(
        side_effect=lambda devices: devices
    )
    return captured


def test_push_updated_merges_with_existing_data(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """F-3: push for dev-1 must keep dev-2 (and others) in published snapshot."""

    now = time.time()
    coordinator._device_location_data["dev-1"] = {
        "device_id": "dev-1",
        "name": "Pixel 9",
        "latitude": 37.0,
        "longitude": -122.0,
        "accuracy": 5,
        "last_updated": now,
    }
    coordinator._device_names["dev-1"] = "Pixel 9"
    coordinator._device_names["dev-2"] = "iPhone 15"

    # dev-2 already lives in self.data (the previous published snapshot).
    coordinator.data = [
        {
            "device_id": "dev-2",
            "name": "iPhone 15",
            "latitude": 40.0,
            "longitude": -74.0,
            "accuracy": 8,
            "last_updated": now - 5.0,
        }
    ]

    captured = _prime_push_updated(coordinator)
    coordinator.push_updated(["dev-1"])

    assert captured, "push_updated should publish a snapshot"
    snapshot = captured[-1]
    ids = {row["device_id"] for row in snapshot}
    assert ids == {"dev-1", "dev-2"}, (
        f"merged snapshot must keep both devices; got {ids}"
    )


def test_push_updated_overwrites_pushed_device_only(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """F-3: pushed device record wins on collision; others stay byte-identical."""

    now = time.time()
    coordinator._device_location_data["dev-1"] = {
        "device_id": "dev-1",
        "name": "Pixel 9",
        "latitude": 41.0,
        "longitude": -100.0,
        "accuracy": 3,
        "last_updated": now,
    }
    coordinator._device_names["dev-1"] = "Pixel 9"
    coordinator._device_names["dev-2"] = "iPhone 15"

    stale_dev1 = {
        "device_id": "dev-1",
        "name": "Pixel 9",
        "latitude": 37.0,
        "longitude": -122.0,
        "accuracy": 50,
        "last_updated": now - 60.0,
    }
    untouched_dev2 = {
        "device_id": "dev-2",
        "name": "iPhone 15",
        "latitude": 40.0,
        "longitude": -74.0,
        "accuracy": 8,
        "last_updated": now - 5.0,
    }
    coordinator.data = [stale_dev1, untouched_dev2]

    captured = _prime_push_updated(coordinator)
    coordinator.push_updated(["dev-1"])

    snapshot = captured[-1]
    by_id = {row["device_id"]: row for row in snapshot}
    assert by_id["dev-1"]["latitude"] == 41.0, "pushed value must win on collision"
    assert by_id["dev-1"]["accuracy"] == 3
    assert by_id["dev-2"] == untouched_dev2, (
        "non-pushed devices must remain byte-identical"
    )


def test_push_updated_subentry_index_uses_merged_snapshot(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """F-9: _refresh_subentry_index must see the merged_snapshot (not None/stale)."""

    now = time.time()
    coordinator._device_location_data["dev-1"] = {
        "device_id": "dev-1",
        "name": "Pixel 9",
        "latitude": 37.0,
        "longitude": -122.0,
        "accuracy": 5,
        "last_updated": now,
    }
    coordinator._device_names["dev-1"] = "Pixel 9"
    coordinator._device_names["dev-2"] = "iPhone 15"
    coordinator.data = [
        {
            "device_id": "dev-2",
            "name": "iPhone 15",
            "latitude": 40.0,
            "longitude": -74.0,
            "accuracy": 8,
            "last_updated": now - 5.0,
        }
    ]

    captured_index_calls: list[Any] = []
    original_refresh = coordinator._refresh_subentry_index

    def _spy_refresh(
        visible_devices: Any = None,
        *,
        skip_manager_update: bool = False,
        skip_repair: bool = False,
    ) -> None:
        captured_index_calls.append(visible_devices)
        original_refresh(
            visible_devices,
            skip_manager_update=skip_manager_update,
            skip_repair=skip_repair,
        )

    coordinator._refresh_subentry_index = _spy_refresh  # type: ignore[method-assign]
    _prime_push_updated(coordinator)
    coordinator.push_updated(["dev-1"])

    assert captured_index_calls, "push_updated must refresh the subentry index"
    visible_arg = captured_index_calls[-1]
    assert visible_arg is not None, "F-9: index must receive merged_snapshot, not None"
    ids_seen = {
        dev.get("device_id") or dev.get("id")
        for dev in visible_arg
        if isinstance(dev, dict)
    }
    assert ids_seen == {"dev-1", "dev-2"}, (
        f"F-9: index must see both devices via merged_snapshot; got {ids_seen}"
    )


def test_push_updated_excludes_ignored_devices_from_merge(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """Codex-review regression: an ignored device already present in
    self.data must NOT reappear in the merged push snapshot, otherwise the
    user's ignore setting is silently defeated until a full poll.

    See: codex-review on PR #172 / commit 7291389e2b.
    """

    now = time.time()
    # dev-new is the actively pushed device.
    coordinator._device_names["dev-new"] = "Active"
    coordinator._device_location_data["dev-new"] = {
        "device_id": "dev-new",
        "name": "Active",
        "latitude": 1.0,
        "longitude": 2.0,
        "accuracy": 5,
        "last_updated": now,
    }

    # dev-old already lives in the previously published snapshot.
    coordinator.data = [
        {
            "device_id": "dev-old",
            "name": "Ignored",
            "latitude": 9.0,
            "longitude": 9.0,
            "accuracy": 8,
            "last_updated": now - 10.0,
        },
        {
            "device_id": "dev-new",
            "name": "Active",
            "latitude": 1.0,
            "longitude": 2.0,
            "accuracy": 5,
            "last_updated": now - 1.0,
        },
    ]

    # User ignores dev-old at runtime; ids filter must apply on BOTH sides
    # of the dict-union merge.
    coordinator._get_ignored_set = lambda: {"dev-old"}

    captured = _prime_push_updated(coordinator)
    coordinator.push_updated(["dev-new"])

    assert captured, "push_updated should publish a snapshot"
    snapshot = captured[-1]
    ids = {row["device_id"] for row in snapshot}
    assert "dev-old" not in ids, (
        f"ignored device must not reappear via old_devices merge; got {ids}"
    )
    assert ids == {"dev-new"}, (
        f"merged snapshot must contain only the non-ignored push target; got {ids}"
    )


def test_issue_183_status_stuck_on_false_readiness_then_recovers(
    coordinator: GoogleFindMyCoordinator,
    dummy_api: _DummyAPI,
) -> None:
    """Issue #183: a false-negative readiness pins fcm_status, a true one heals.

    Discriminates the candidate root causes at the coordinator-status layer:

    * H2 (false readiness): while ``api.is_push_ready()`` returns ``False`` the
      poll-top recovery never promotes the status, so it stays non-CONNECTED
      across cycles even though the receiver is healthy -- the "Disconnected
      until restart" symptom.
    * H1 (frozen cache) refuted: the moment readiness returns ``True`` a single
      poll cycle restores CONNECTED, so the status is re-evaluated, not frozen.
    * H3 (receiver down) refuted: only the readiness signal is toggled here; the
      coordinator recovers without any change to the receiver itself.
    """

    loop = coordinator.hass.loop
    coordinator._async_build_device_snapshot_with_fallbacks.return_value = []
    # A real device lets the poll cycle complete (avoids the cold-start guard);
    # the assertions below only inspect the status set by the recovery block.
    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._enabled_poll_device_ids = {"dev-1"}

    # Use the real soft-check so it consults ``api.is_push_ready`` (the desync
    # victim in issue #183), instead of the fixture's hard-wired ``True``.
    coordinator._is_fcm_ready_soft = type(coordinator)._is_fcm_ready_soft.__get__(
        coordinator
    )

    # Reload start state: a healthy receiver, but readiness reports False.
    dummy_api.is_push_ready = lambda: False

    loop.run_until_complete(coordinator._async_update_data())
    assert coordinator.fcm_status.state != FcmStatus.CONNECTED

    # A second cycle keeps it stuck (does not self-heal while readiness is False).
    loop.run_until_complete(coordinator._async_update_data())
    assert coordinator.fcm_status.state != FcmStatus.CONNECTED

    # Readiness becomes correct again -> the very next cycle recovers.
    dummy_api.is_push_ready = lambda: True
    loop.run_until_complete(coordinator._async_update_data())
    assert coordinator.fcm_status.state == FcmStatus.CONNECTED


# --------------------------------------------------------------------------
# The device list as the proof source for the transient auth counter.
#
# Moving the counter reset behind the empty guard in the poll cycle takes away
# its only everyday reset source: a fleet of idle BLE tags returns empty and no
# longer clears anything. Without a replacement a counter that once reached 2
# would sit there forever, and a single later hiccup would ask a user with a
# perfectly good login to sign in again.
#
# ``async_get_basic_device_list`` is the strongest proof source in the tree:
# it has no non-throwing error exit, so a non-throwing return means Nova
# accepted the account token -- which is exactly what the counter counts. An
# expired login raises ``ConfigEntryAuthFailed`` and never reaches the reset,
# which is what separates this source from the poll path it replaces.
# --------------------------------------------------------------------------


def test_a_fresh_device_list_resets_the_transient_auth_counter(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """A real device-list refresh proves the credentials and clears the counter."""

    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._consecutive_transient_auth_failures = 2

    loop = coordinator.hass.loop
    loop.run_until_complete(coordinator._async_update_data())

    assert coordinator._consecutive_transient_auth_failures == 0


def test_a_cached_device_list_does_not_reset_the_counter(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """A skipped refresh proves nothing; the reset belongs in the fetch branch.

    The cached branch never talks to Nova. Resetting there would reintroduce
    the very defect this change removes, one layer up: a counter cleared by
    something that did not happen.
    """

    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._last_device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._last_list_poll_mono = time.monotonic()
    coordinator._consecutive_transient_auth_failures = 2

    loop = coordinator.hass.loop
    loop.run_until_complete(coordinator._async_update_data())

    assert coordinator._consecutive_transient_auth_failures == 2


def test_a_fresh_device_list_clears_the_last_transient_error(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """The stored cause is cleared with the counter, not left behind.

    Separate from the counter assertion on purpose: the diagnostic snapshot
    exports both, and a stale cause next to a zero counter names a failure that
    is over.
    """

    dummy_api.device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator._consecutive_transient_auth_failures = 2
    coordinator._last_transient_auth_error = "expired"

    loop = coordinator.hass.loop
    loop.run_until_complete(coordinator._async_update_data())

    assert coordinator._last_transient_auth_error is None


def test_a_failing_device_list_leaves_the_counter_alone(
    coordinator: GoogleFindMyCoordinator, dummy_api: _DummyAPI
) -> None:
    """An expired login cannot mask itself through the new reset source.

    This is the guard that makes the new source safe to rely on: the reset must
    sit AFTER the await, so a raising call never reaches it.
    """

    dummy_api.raise_auth = True
    coordinator._consecutive_transient_auth_failures = 2
    coordinator._last_transient_auth_error = "expired"

    loop = coordinator.hass.loop
    with pytest.raises(ConfigEntryAuthFailed):
        loop.run_until_complete(coordinator._async_update_data())

    assert coordinator._consecutive_transient_auth_failures == 2
    assert coordinator._last_transient_auth_error == "expired"

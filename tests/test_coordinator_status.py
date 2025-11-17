# tests/test_coordinator_status.py
"""Regression tests for coordinator status handling (API vs. FCM)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.googlefindmy.coordinator import (
    ApiStatus,
    FcmStatus,
    GoogleFindMyCoordinator,
    _FCM_FALLBACK_POLL_AFTER_S,
)
from custom_components.googlefindmy.binary_sensor import GoogleFindMyPollingSensor
from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DOMAIN,
    EVENT_AUTH_OK,
    ISSUE_AUTH_EXPIRED_KEY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
    issue_id_for,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.exceptions import ConfigEntryAuthFailed

from tests.helpers import drain_loop


class _DummyBus:
    """Capture fired events for assertions."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict | None]] = []

    def async_fire(self, event: str, data: dict | None = None) -> None:
        self.fired.append((event, data))


class _DummyConfigEntries:
    """Stub Home Assistant config_entries manager."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.setup_calls: list[str] = []

    async def async_start_reauth(self, entry: object) -> None:
        self.calls.append(entry)

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

    async def async_start_reauth(self, hass) -> None:  # noqa: D401 - stub signature
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
        "custom_components.googlefindmy.coordinator.GoogleFindMyAPI",
        _factory,
    )
    return api


@pytest.fixture
def coordinator(
    monkeypatch: pytest.MonkeyPatch, dummy_api: _DummyAPI
) -> GoogleFindMyCoordinator:
    """Instantiate a coordinator with lightweight stubs for hass/cache."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
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
    coord._get_ignored_set = lambda: set()
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
    assert coordinator.config_entry.reauth_calls == 0
    assert coordinator.hass.config_entries.calls == []
    assert "Invalid" in (coordinator.api_status.reason or "")


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

    assert entity._attr_name == "Pixel 9"
    assert entity.subentry_key == TRACKER_SUBENTRY_KEY
    assert subentry_identifier in entity.unique_id


def test_device_tracker_respects_coordinator_unavailability(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """Availability should mirror coordinator health before device checks."""

    coordinator.is_device_visible_in_subentry = lambda *_args, **_kwargs: True
    coordinator.get_device_location_data_for_subentry = (
        lambda *_args, **_kwargs: {"latitude": 0.0, "longitude": 0.0}
    )
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

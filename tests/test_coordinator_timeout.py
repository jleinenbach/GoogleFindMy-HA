from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from tests.helpers import drain_loop


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(self, _key: str, _value):  # pragma: no cover - stub
        return None


class _DummyBus:
    """Provide an async_listen placeholder used by the coordinator."""

    def async_listen(self, *_args, **_kwargs):  # pragma: no cover - stub
        return lambda: None

    def async_fire(self, *_args, **_kwargs):  # pragma: no cover - stub
        return None


class _DummyHass:
    """Lightweight Home Assistant stub capturing created tasks."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        return self.loop.create_task(coro, name=name)


class _TimeoutAPI:
    """API stub that always times out during location requests."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        raise TimeoutError()


class _AuthFailureAPI:
    """API stub that forces an auth failure during location requests."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        raise SpotApiEmptyResponseError()


class _PartialTimeoutAPI:
    """API stub that times out once but succeeds for other devices."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def async_get_device_location(self, dev_id: str, dev_name: str):
        self.calls.append(dev_id)
        if dev_id == "dev-timeout":
            raise TimeoutError()
        return {
            "id": dev_id,
            "name": dev_name,
            "latitude": 1.0,
            "longitude": 1.0,
            "accuracy": 5.0,
            "last_seen": time.time(),
        }


def test_poll_timeout_sets_update_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts should propagate as update errors and mark the cycle as failed."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = _DummyHass(loop)

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = _TimeoutAPI()
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = lambda: set()
    coordinator._last_device_list = [{"id": "dev-1", "name": "Device"}]

    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data):
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(
                [{"id": "dev-1", "name": "Device"}], force=True
            )
        )
    finally:
        drain_loop(loop)

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.stats["timeouts"] == 1


def test_poll_auth_failure_raises_auth_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth failures should translate to ConfigEntryAuthFailed and mark the cycle failed."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = _DummyHass(loop)

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = _AuthFailureAPI()
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = lambda: set()
    coordinator._last_device_list = [{"id": "dev-1", "name": "Device"}]

    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data):
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data

    with pytest.raises(ConfigEntryAuthFailed):
        try:
            loop.run_until_complete(
                coordinator._async_start_poll_cycle(
                    [{"id": "dev-1", "name": "Device"}], force=True
                )
            )
        finally:
            drain_loop(loop)

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)


def test_poll_timeout_still_processes_other_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout for one device should not prevent polling of the rest."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = _DummyHass(loop)

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    api = _PartialTimeoutAPI()
    coordinator.api = api
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = lambda: set()
    coordinator._last_device_list = [
        {"id": "dev-timeout", "name": "Timeout Device"},
        {"id": "dev-success", "name": "Success Device"},
    ]

    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data):
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(
                [
                    {"id": "dev-timeout", "name": "Timeout Device"},
                    {"id": "dev-success", "name": "Success Device"},
                ],
                force=True,
            )
        )
    finally:
        drain_loop(loop)

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.stats["timeouts"] == 1
    assert api.calls == ["dev-timeout", "dev-success"]
    assert "dev-success" in coordinator._device_location_data
    assert {entry["id"] for entry in coordinator.data} == {
        "dev-timeout",
        "dev-success",
    }

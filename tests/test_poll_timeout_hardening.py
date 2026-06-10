"""Regression tests for the poll-timeout hardening (B1-B3).

These pin the behaviour introduced to stop the noisy
"Poll timed out ... (FCM connected)" warnings:

* B2 - the outer per-device poll guard must be strictly larger than the inner
  FCM wait, so the inner request can return its clean empty result before the
  outer guard fires (Nygard: stagger nested timeout budgets).
* B3 - an empty location result is an expected idle outcome and must log at
  INFO, not WARNING.
* Behaviour preservation - a genuine FCM-disconnected timeout must still count
  as a failed cycle and advance ``_consecutive_timeouts``; the benign
  FCM-connected timeout must not.

The setup mirrors ``tests/test_coordinator_timeout.py`` (explicit event loop +
``_async_start_poll_cycle``), the established pattern for this code path.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy.const import (
    LOCATION_REQUEST_TIMEOUT_S,
    POLL_DEVICE_OUTER_TIMEOUT_S,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.stats import FcmStatus
from tests.helpers import drain_loop

_POLLING_LOGGER = "custom_components.googlefindmy.coordinator.polling"


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(
        self, _key: str, _value
    ):  # pragma: no cover - stub
        return None


class _DummyBus:
    """Provide async_listen/async_fire placeholders used by the coordinator."""

    def async_listen(self, *_args, **_kwargs):  # pragma: no cover - stub
        return lambda: None

    def async_fire(self, *_args, **_kwargs):  # pragma: no cover - stub
        return None


class _DummyHass:
    """Lightweight Home Assistant stub capturing created tasks."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401
        return self.loop.create_task(coro, name=name)


class _TimeoutAPI:
    """API stub that always times out during location requests."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        raise TimeoutError()


class _EmptyResultAPI:
    """API stub that returns an empty result (no reporter in range)."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        return []


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    api: object,
    devices: list[dict[str, str]],
) -> GoogleFindMyCoordinator:
    """Build a coordinator wired with stubs, mirroring test_coordinator_timeout."""

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    hass = _DummyHass(loop)
    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = api
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)

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
    return coordinator


def test_outer_poll_budget_exceeds_inner_wait() -> None:
    """B2: the outer per-device guard must be strictly larger than the inner wait."""

    assert POLL_DEVICE_OUTER_TIMEOUT_S > LOCATION_REQUEST_TIMEOUT_S


def test_empty_location_logs_info_not_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3: an empty location result is expected and must log at INFO, not WARNING."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-empty", "name": "Idle Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, _EmptyResultAPI(), devices)

    try:
        with caplog.at_level(logging.INFO, logger=_POLLING_LOGGER):
            loop.run_until_complete(
                coordinator._async_start_poll_cycle(devices, force=True)
            )
    finally:
        drain_loop(loop)

    idle_records = [
        rec
        for rec in caplog.records
        if rec.name == _POLLING_LOGGER
        and "No location data returned" in rec.getMessage()
    ]
    assert idle_records, "expected an idle 'No location data returned' log line"
    assert all(rec.levelno == logging.INFO for rec in idle_records)
    assert not any(
        rec.name == _POLLING_LOGGER
        and rec.levelno >= logging.WARNING
        and "No location data returned" in rec.getMessage()
        for rec in caplog.records
    )


def test_fcm_disconnected_timeout_still_counts_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour preservation: a disconnected-FCM timeout fails the cycle."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-1", "name": "Device"}]
    coordinator = _make_coordinator(monkeypatch, loop, _TimeoutAPI(), devices)
    coordinator._fcm_status_state = FcmStatus.DISCONNECTED
    coordinator._consecutive_timeouts = 0

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.stats["timeouts"] == 1
    assert coordinator._consecutive_timeouts == 1


def test_fcm_connected_timeout_is_benign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected-FCM timeout is healthy: counted as a timeout stat but it must
    not advance the consecutive-timeout failure counter."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-1", "name": "Device"}]
    coordinator = _make_coordinator(monkeypatch, loop, _TimeoutAPI(), devices)
    coordinator._fcm_status_state = FcmStatus.CONNECTED
    coordinator._consecutive_timeouts = 0

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)

    assert coordinator.stats["timeouts"] == 1
    assert coordinator._consecutive_timeouts == 0

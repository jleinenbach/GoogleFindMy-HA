import asyncio
import time
from collections import deque
from collections.abc import Coroutine
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import (
    _PREDICTION_BUFFER_S,
    GoogleFindMyCoordinator,
)


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str) -> None:
        return None

    async def async_set_cached_value(self, _key: str, _value: object) -> None:
        return None


class _DummyBus:
    """Provide async_listen placeholder used by the coordinator."""

    def async_listen(self, *_args, **_kwargs):
        return lambda: None


class _DummyHass:
    """Lightweight Home Assistant stub capturing created tasks."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()
        self.data: dict[str, dict] = {DOMAIN: {}}
        self.created: list[tuple[asyncio.Task[object], str | None]] = []

    def async_create_task(
        self,
        coro: asyncio.Future | Coroutine[object, object, object],
        *,
        name: str | None = None,
    ) -> asyncio.Task:
        task = self.loop.create_task(coro, name=name)
        self.created.append((task, name))
        return task


class _DummyAPI:
    """No-op API placeholder injected into the coordinator."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def is_push_ready(self) -> bool:
        return True


@pytest.fixture
async def coordinator(monkeypatch: pytest.MonkeyPatch):
    """Instantiate a coordinator with patched dependencies for predictive tests."""

    loop = asyncio.get_running_loop()

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.GoogleFindMyAPI",
        lambda *args, **kwargs: _DummyAPI(),
    )

    hass = _DummyHass(loop)
    coord = GoogleFindMyCoordinator(
        hass,
        cache=_DummyCache(),
        location_poll_interval=200,
        min_poll_interval=60,
    )
    coord.async_set_updated_data = lambda _data: None

    yield coord
    await asyncio.sleep(0)


def _prime_cached_devices(
    coordinator: GoogleFindMyCoordinator, wall_now: float
) -> None:
    coordinator._last_device_list = [{"id": "dev-id"}]
    coordinator._last_list_poll_mono = time.monotonic()
    coordinator._last_poll_mono = time.monotonic()
    coordinator._device_location_data["dev-id"] = {"last_seen": wall_now}


@pytest.mark.asyncio
async def test_predictive_polling_defers_until_predicted_window(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Imminent updates schedule a short retry with buffer instead of polling immediately."""

    wall_now = time.time()
    coordinator._device_update_history["dev-id"] = deque(
        [wall_now - 240, wall_now - 120, wall_now],
        maxlen=4,
    )
    _prime_cached_devices(coordinator, wall_now)
    baseline_polls = sum(
        1 for _, name in coordinator.hass.created if name == f"{DOMAIN}.poll_cycle"
    )

    scheduled: list[float] = []
    coordinator._schedule_short_retry = scheduled.append

    poll_cycle = AsyncMock()
    coordinator._async_start_poll_cycle = poll_cycle

    monkeypatch.setattr(coordinator, "_ensure_service_device_exists", lambda: None)
    monkeypatch.setattr(
        coordinator, "_ensure_registry_for_devices", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        coordinator, "_refresh_subentry_index", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        coordinator,
        "_async_build_device_snapshot_with_fallbacks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(coordinator, "_store_subentry_snapshots", lambda _snap: None)

    result = await coordinator._async_update_data()

    assert result == []
    assert scheduled, "Predictive retry should be scheduled when update is imminent"
    expected_delay = (
        coordinator._get_predicted_poll_time() - time.time()
    ) + _PREDICTION_BUFFER_S
    assert scheduled[0] == pytest.approx(expected_delay, rel=0.1)
    poll_names = [
        name for _, name in coordinator.hass.created if name == f"{DOMAIN}.poll_cycle"
    ]
    assert len(poll_names) == baseline_polls
    assert poll_cycle.await_count == 0


@pytest.mark.asyncio
async def test_predictive_overdue_does_not_poll_below_cadence_floor(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overdue prediction must not pull polling below the cadence floor.

    Stage 1 makes ``effective_interval`` govern the cadence: predictive
    pre-fetch no longer fires while ``elapsed < effective_interval``. Here
    ``elapsed == min_poll_interval`` (the API safety floor) but stays below
    ``effective_interval``, so no poll is scheduled even though the device's
    prediction is overdue.
    """

    wall_now = time.time()
    coordinator._device_update_history["dev-id"] = deque(
        [wall_now - 900, wall_now - 600, wall_now - 350],
        maxlen=4,
    )
    _prime_cached_devices(coordinator, wall_now)
    # elapsed == min_poll_interval < effective_interval → below the cadence floor.
    coordinator._last_poll_mono = time.monotonic() - coordinator.min_poll_interval
    baseline_polls = sum(
        1 for _, name in coordinator.hass.created if name == f"{DOMAIN}.poll_cycle"
    )

    monkeypatch.setattr(coordinator, "_ensure_service_device_exists", lambda: None)
    monkeypatch.setattr(
        coordinator, "_ensure_registry_for_devices", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        coordinator, "_refresh_subentry_index", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        coordinator,
        "_async_build_device_snapshot_with_fallbacks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(coordinator, "_store_subentry_snapshots", lambda _snap: None)

    poll_cycle = AsyncMock(return_value=None)
    coordinator._async_start_poll_cycle = poll_cycle

    short_retries: list[float] = []
    coordinator._schedule_short_retry = short_retries.append

    await coordinator._async_update_data()
    poll_tasks = [
        task
        for task, name in coordinator.hass.created
        if name == f"{DOMAIN}.poll_cycle"
    ]
    # Below the cadence floor the overdue prediction is suppressed: no poll,
    # and no short retry is scheduled (that only happens on predictive_block).
    assert len(poll_tasks) == baseline_polls
    assert not short_retries
    assert poll_cycle.await_count == 0


@pytest.mark.asyncio
async def test_predictive_overdue_polls_at_cadence_floor(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overdue prediction does not suppress the poll at the cadence floor.

    Guard companion to the below-floor case: with ``elapsed >= effective_interval``
    the regular cadence branch admits the poll, and this test proves the overdue
    prediction (``predictive_due``) neither blocks it nor triggers a short retry.
    The discriminating coverage of the new ``elapsed >= effective_interval`` term
    on the predictive branch itself lives in the pure-function unit tests
    (``test_coordinator_update.py``); at integration level ``predictive_block``
    is not freely settable, so the cadence branch is the only realistic path.
    Uses the same ``max(location_poll_interval, min_poll_interval)`` the
    coordinator computes (no magic value).
    """

    wall_now = time.time()
    coordinator._device_update_history["dev-id"] = deque(
        [wall_now - 900, wall_now - 600, wall_now - 350],
        maxlen=4,
    )
    _prime_cached_devices(coordinator, wall_now)
    effective_interval = max(
        coordinator.location_poll_interval, coordinator.min_poll_interval
    )
    # elapsed == effective_interval → the cadence floor is satisfied.
    coordinator._last_poll_mono = time.monotonic() - effective_interval
    baseline_polls = sum(
        1 for _, name in coordinator.hass.created if name == f"{DOMAIN}.poll_cycle"
    )

    monkeypatch.setattr(coordinator, "_ensure_service_device_exists", lambda: None)
    monkeypatch.setattr(
        coordinator, "_ensure_registry_for_devices", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(
        coordinator, "_refresh_subentry_index", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        coordinator,
        "_async_build_device_snapshot_with_fallbacks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(coordinator, "_store_subentry_snapshots", lambda _snap: None)

    poll_cycle = AsyncMock(return_value=None)
    coordinator._async_start_poll_cycle = poll_cycle

    short_retries: list[float] = []
    coordinator._schedule_short_retry = short_retries.append

    await coordinator._async_update_data()
    poll_tasks = [
        task
        for task, name in coordinator.hass.created
        if name == f"{DOMAIN}.poll_cycle"
    ]
    assert len(poll_tasks) == baseline_polls + 1
    await asyncio.wait_for(poll_tasks[-1], timeout=1)

    assert not short_retries
    assert poll_cycle.await_count == 1

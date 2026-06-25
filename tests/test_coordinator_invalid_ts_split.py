# tests/test_coordinator_invalid_ts_split.py
"""Coordinator wiring of the invalid_ts drop sub-buckets.

The cumulative ``invalid_ts_drop_count`` aggregate conflates two semantically
disjoint drop reasons that ``cache.py::_is_significant_update`` rejects:

* corrupt pre-Y2K timestamps (alarming) -> ``invalid_ts_drop_warn``
* regressed/out-of-order timestamps (benign) -> ``invalid_ts_drop_benign``

These tests pin:

* both sub-bucket keys are part of the real init/restore stat surface (the
  ``increment_stat`` guard silently drops unregistered keys, so registration in
  the production init dict is load-bearing),
* a pre-Y2K drop increments ``invalid_ts_drop_warn`` (and the aggregate) but
  not ``invalid_ts_drop_benign``,
* a regressed drop increments ``invalid_ts_drop_benign`` (and the aggregate)
  but not ``invalid_ts_drop_warn``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers import drain_loop


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(
        self, _key: str, _value
    ):  # pragma: no cover - stub
        return None


class _DummyBus:
    """Provide an async_listen placeholder used by the coordinator."""

    def async_listen(self, *_args, **_kwargs):  # pragma: no cover - stub
        return lambda: None

    def async_fire(self, *_args, **_kwargs):  # pragma: no cover - stub
        return None


class _DummyHass:
    """Lightweight Home Assistant stub satisfying the constructor."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        return self.loop.create_task(coro, name=name)


def _make_coordinator(existing: dict[str, Any]) -> GoogleFindMyCoordinator:
    """Create a coordinator instance with preloaded cache data for testing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {"device-1": dict(existing)}
    coordinator._device_names = {}
    coordinator._device_update_history = {}
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    return coordinator


def _stat_recorder() -> tuple[dict[str, int], Callable[[str], None]]:
    """Return a stat counter map and increment callback for assertions."""

    counts: dict[str, int] = {}

    def _increment(stat_name: str) -> None:
        counts[stat_name] = counts.get(stat_name, 0) + 1

    return counts, _increment


def test_init_registers_invalid_ts_sub_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sub-bucket keys default to 0 in the real init stat surface.

    Registration is load-bearing: ``increment_stat`` silently drops keys absent
    from ``self.stats``, so the production init dict must seed them.
    """

    loop = asyncio.new_event_loop()
    hass = _DummyHass(loop)
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    try:
        coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
        assert coordinator.stats["invalid_ts_drop_warn"] == 0
        assert coordinator.stats["invalid_ts_drop_benign"] == 0
    finally:
        drain_loop(loop)
        loop.close()


def test_pre_y2k_timestamp_increments_warn_bucket() -> None:
    """A corrupt pre-Y2K timestamp drop counts as warn, not benign."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "status": "coordinate",
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    corrupt = {
        "latitude": 37.4224,
        "longitude": -122.085,
        "accuracy": 30.0,
        "last_seen": 1000,  # well before the Y2K epoch boundary
        "status": "coordinate",
    }

    coordinator.update_device_cache("device-1", corrupt)

    assert stat_counts == {
        "invalid_ts_drop_count": 1,
        "drop_reason_invalid_ts": 1,
        "invalid_ts_drop_warn": 1,
    }


def test_regressed_timestamp_increments_benign_bucket() -> None:
    """A regressed/out-of-order timestamp drop counts as benign, not warn."""

    existing = {
        "latitude": 37.4219999,
        "longitude": -122.0840575,
        "accuracy": 25.0,
        "last_seen": 1_700_000_000,
        "status": "coordinate",
    }
    coordinator = _make_coordinator(existing)
    stat_counts, increment = _stat_recorder()
    coordinator.increment_stat = increment

    regressed = {
        "latitude": 37.4224,
        "longitude": -122.085,
        "accuracy": 30.0,
        "last_seen": existing["last_seen"] - 120,
        "status": "coordinate",
    }

    coordinator.update_device_cache("device-1", regressed)

    assert stat_counts == {
        "invalid_ts_drop_count": 1,
        "drop_reason_invalid_ts": 1,
        "invalid_ts_drop_benign": 1,
    }

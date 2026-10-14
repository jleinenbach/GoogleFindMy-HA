# tests/test_coordinator_cache_poll_status.py
"""A poll served from the cache must not touch the cached row.

The device tracker publishes from the coordinator cache
(``get_device_location_data``). The snapshot built on a cache-served poll
recomputes ``status`` from the age of the fix; that value belongs to the
snapshot copy only. If it were written back into the cache row, the tracker's
observation would change with the wall clock and ``last_updated`` would move
without a new fix.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.core_shutdown_state import seed_core_shutdown_state

_POLL_INTERVAL = 300
_FIX_TIME = 1_000.0
# Fresh, aging and stale relative to the poll interval, so the snapshot status
# really changes between the three polls.
_POLL_TIMES = (_FIX_TIME + 50.0, _FIX_TIME + 350.0, _FIX_TIME + 650.0)
_ROW: dict[str, Any] = {
    "latitude": 52.52,
    "longitude": 13.405,
    "accuracy": 12.0,
    "last_seen": _FIX_TIME,
    "last_updated": _FIX_TIME,
    "status": "Fused (Weighted)",
    "semantic_name": None,
}


def _coordinator() -> Any:
    coordinator = seed_core_shutdown_state(
        GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    )
    coordinator.location_poll_interval = _POLL_INTERVAL
    coordinator._device_location_data = {"dev": copy.deepcopy(_ROW)}
    return coordinator


_DEVICES = [{"id": "dev", "name": "Keys"}]


def test_snapshot_status_ages_but_the_cached_row_does_not() -> None:
    """The snapshot status follows the clock; the cache row stays as written."""
    coordinator = _coordinator()

    statuses = [
        coordinator._build_snapshot_from_cache(_DEVICES, wall_now=now)[0]["status"]
        for now in _POLL_TIMES
    ]

    # Precondition: the snapshot really recomputed the status per poll.
    assert len(set(statuses)) == len(_POLL_TIMES)
    assert coordinator._device_location_data["dev"] == _ROW
    assert coordinator.get_device_location_data("dev") == _ROW


@pytest.mark.asyncio
async def test_fallback_snapshot_served_from_cache_keeps_the_cached_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll path (snapshot with fallbacks) leaves the cache row untouched."""
    coordinator = _coordinator()
    clock = iter(_POLL_TIMES)
    # Replace only the module reference in ``main``; patching ``time.time``
    # itself would also feed logging and the event loop from this clock.
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.time",
        SimpleNamespace(time=lambda: next(clock)),
    )

    statuses = []
    for _ in _POLL_TIMES:
        snapshot = await coordinator._async_build_device_snapshot_with_fallbacks(
            _DEVICES
        )
        statuses.append(snapshot[0]["status"])

    assert len(set(statuses)) == len(_POLL_TIMES)
    assert coordinator._device_location_data["dev"] == _ROW
    assert coordinator.get_device_location_data("dev") == _ROW

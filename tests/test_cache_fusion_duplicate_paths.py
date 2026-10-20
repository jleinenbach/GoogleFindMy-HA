# tests/test_cache_fusion_duplicate_paths.py
"""A report that already entered the cache is not fused again, on every path.

The fusion averages an incoming fix with the cached position. When the same
report arrives again (the next poll returns it unchanged, a manual locate gets
it once more, the push channel delivers what a poll already brought in), the
cached position already IS the average of that report, and averaging again
moved the published position towards the raw fix on every delivery - without a
new location and with a fresh state write each time. ``person`` then treated
the tracker as freshly updated.

These tests drive the three real entry points into the fusion (poll cycle,
manual locate, ``update_device_cache`` for push), the propagation to shared
devices and the purge, without stubbing ``_apply_weighted_location_fusion``.
Unless a test says otherwise (the clear jump does not overlap on purpose), a
scenario starts from a cached fix that is OLDER than the report, overlaps it and
carries the same source rank: with an equal timestamp a higher cached rank
would block the merge, and without a cached timestamp the merge would judge by
distance, and either would hide the defect.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry

_DEVICE = "dev-1"
_SIBLING = "dev-2"
_SHARED_KEY = bytes(range(32))


class _DummyCache:
    async def async_get_cached_value(self, _key: str) -> None:  # pragma: no cover
        return None

    async def async_set_cached_value(
        self, _key: str, _value: object
    ) -> None:  # pragma: no cover
        return None


class _DummyBus:
    def async_listen(
        self, *_args: object, **_kwargs: object
    ) -> Any:  # pragma: no cover
        return lambda: None

    def async_fire(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
        return None


class _DummyHass:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()
        self.data: dict[str, Any] = {}

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        return self.loop.create_task(coro, name=name)


class _Api:
    """Returns the same report on every request, as a stuck server answer does."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report

    async def async_get_device_location(
        self, _dev_id: str, _dev_name: str
    ) -> dict[str, Any]:
        return dict(self.report)


def _coordinator(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, Any]
) -> tuple[GoogleFindMyCoordinator, dict[str, int]]:
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    coordinator = GoogleFindMyCoordinator(
        _DummyHass(asyncio.get_running_loop()), cache=_DummyCache()
    )
    coordinator.config_entry = make_config_entry(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = _Api(report)
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    devices = [{"id": _DEVICE, "name": "Tag"}]
    coordinator._last_device_list = list(devices)
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None
    counts: dict[str, int] = {}

    def _increment(stat_name: str) -> None:
        counts[stat_name] = counts.get(stat_name, 0) + 1

    coordinator.increment_stat = _increment  # type: ignore[method-assign]
    return coordinator, counts


async def _settle(coordinator: GoogleFindMyCoordinator) -> None:
    save_task = coordinator._stats_save_task
    if save_task is not None:
        save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await save_task


def _prior_fix(now: float) -> dict[str, Any]:
    return {
        "latitude": 49.866,
        "longitude": 10.839,
        "accuracy": 50.0,
        "last_seen": now - 600.0,
        "status": "coordinate",
    }


def _report(now: float, **overrides: Any) -> dict[str, Any]:
    """A report five minutes after the prior fix, about 33 m north of it."""

    report: dict[str, Any] = {
        "latitude": 49.8663,
        "longitude": 10.839,
        "accuracy": 50.0,
        "last_seen": now - 300.0,
        "status": "coordinate",
    }
    report.update(overrides)
    return report


def _position(coordinator: GoogleFindMyCoordinator, device_id: str) -> tuple[Any, Any]:
    cached = coordinator._device_location_data[device_id]
    return cached["latitude"], cached["longitude"]


async def _poll(coordinator: GoogleFindMyCoordinator) -> None:
    await coordinator._async_start_poll_cycle(
        [{"id": _DEVICE, "name": "Tag"}], force=True
    )
    await _settle(coordinator)


async def _locate(coordinator: GoogleFindMyCoordinator) -> None:
    # The cooldowns a successful locate sets are cleared through the fields
    # themselves; waiting them out would make the test slow and clock-bound.
    coordinator._locate_cooldown_until.pop(_DEVICE, None)
    coordinator._device_poll_cooldown_until.pop(_DEVICE, None)
    await coordinator.async_locate_device(_DEVICE)
    await _settle(coordinator)


@pytest.mark.asyncio
async def test_poll_path_repeat_is_fused_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second poll returning the same report leaves the position where it was."""

    now = time.time()
    coordinator, counts = _coordinator(monkeypatch, _report(now))
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))

    await _poll(coordinator)
    after_first = _position(coordinator, _DEVICE)
    await _poll(coordinator)

    assert _position(coordinator, _DEVICE) == after_first
    assert after_first[0] == pytest.approx(49.86615, abs=1e-9)
    assert counts.get("fused_updates") == 1


@pytest.mark.asyncio
async def test_locate_path_repeat_is_fused_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second manual locate with the same answer leaves the position alone."""

    now = time.time()
    coordinator, counts = _coordinator(monkeypatch, _report(now))
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))

    await _locate(coordinator)
    after_first = _position(coordinator, _DEVICE)
    await _locate(coordinator)

    assert _position(coordinator, _DEVICE) == after_first
    assert after_first[0] == pytest.approx(49.86615, abs=1e-9)
    assert counts.get("fused_updates") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_overrides",
    [
        # The poll path normalises the stamp before the fusion, the push path
        # only after it; the identity must not depend on which one ran.
        pytest.param({"last_seen_ms": True}, id="ms_timestamp"),
        # Poll and locate drop an error-code accuracy before the fusion, the
        # push path carries 0.0 into it.
        pytest.param({"accuracy": 0.0}, id="zero_accuracy"),
    ],
)
async def test_poll_then_push_same_report_is_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, raw_overrides: dict[str, Any]
) -> None:
    """The push channel delivering what a poll already brought in changes nothing."""

    now = time.time()
    raw = _report(now)
    if raw_overrides.pop("last_seen_ms", False):
        raw["last_seen"] = int(raw["last_seen"] * 1000)
    raw.update(raw_overrides)
    coordinator, counts = _coordinator(monkeypatch, raw)
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))

    await _poll(coordinator)
    after_poll = _position(coordinator, _DEVICE)
    fused_after_poll = counts.get("fused_updates")
    coordinator.update_device_cache(_DEVICE, dict(raw))

    assert _position(coordinator, _DEVICE) == after_poll
    assert fused_after_poll == 1
    assert counts.get("fused_updates") == 1


@pytest.mark.asyncio
async def test_clear_jump_is_booked_like_a_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report taken over unaveraged is remembered as well.

    About 1.1 km from the cached fix with 50 m radii on both sides: no overlap,
    so the first delivery replaces the position without averaging. The second
    delivery would overlap the new position exactly and was fused with itself.
    """

    now = time.time()
    jump = _report(now, latitude=49.876)
    coordinator, counts = _coordinator(monkeypatch, jump)
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))

    coordinator.update_device_cache(_DEVICE, dict(jump))
    coordinator.update_device_cache(_DEVICE, dict(jump))

    cached = coordinator._device_location_data[_DEVICE]
    assert cached["status"] == "coordinate"
    assert cached["latitude"] == pytest.approx(49.876, abs=1e-9)
    assert counts.get("fused_updates", 0) == 0


@pytest.mark.asyncio
async def test_no_transient_marker_reaches_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the fusion pencils onto a report stays out of the cached row."""

    now = time.time()
    coordinator, _counts = _coordinator(monkeypatch, _report(now))
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))

    await _poll(coordinator)
    await _locate(coordinator)
    coordinator.update_device_cache(_DEVICE, _report(now, last_seen=now - 200.0))
    coordinator.update_device_cache(_DEVICE, _report(now, last_seen=now - 200.0))

    leaked = [
        key for key in coordinator._device_location_data[_DEVICE] if key.startswith("_")
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_purge_forgets_fused_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device re-added under the same id does not inherit what was fused before."""

    now = time.time()
    coordinator, counts = _coordinator(monkeypatch, _report(now))
    coordinator.update_device_cache(_DEVICE, _prior_fix(now))
    coordinator.update_device_cache(_DEVICE, _report(now))

    coordinator.purge_device(_DEVICE)
    coordinator.update_device_cache(
        _DEVICE, _prior_fix(now) | {"latitude": 49.8657, "last_seen": now - 900.0}
    )
    coordinator.update_device_cache(_DEVICE, _report(now))

    assert counts.get("fused_updates") == 2


@pytest.mark.asyncio
async def test_shared_sibling_does_not_refuse_propagated_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy a shared device receives counts as the report having arrived there.

    Two accounts track one physical tag. A report committed on the first is
    propagated to the second; when the second account then receives the very
    same report itself, it is already in its cache and must not be averaged
    with the propagated (already averaged) position.
    """

    now = time.time()
    coordinator, _counts = _coordinator(monkeypatch, _report(now))
    for device_id in (_DEVICE, _SIBLING):
        coordinator.update_device_cache(
            device_id, _prior_fix(now) | {"identity_key": _SHARED_KEY}
        )

    coordinator.update_device_cache(
        _DEVICE, _report(now) | {"identity_key": _SHARED_KEY}
    )
    propagated = _position(coordinator, _SIBLING)
    coordinator.update_device_cache(
        _SIBLING, _report(now) | {"identity_key": _SHARED_KEY}
    )

    assert propagated[0] == pytest.approx(49.86615, abs=1e-9)
    assert _position(coordinator, _SIBLING) == propagated


@pytest.mark.asyncio
async def test_sibling_that_skipped_propagation_still_fuses_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared device that did not adopt the row has not received the report.

    The sibling already holds its own fix of the same second, so the
    propagation skips it. When the report then reaches the sibling directly,
    it is new there and is fused with the sibling's own fix.
    """

    now = time.time()
    coordinator, _counts = _coordinator(monkeypatch, _report(now))
    for device_id in (_DEVICE, _SIBLING):
        coordinator.update_device_cache(
            device_id, _prior_fix(now) | {"identity_key": _SHARED_KEY}
        )
    own_fix = _report(now, latitude=49.866, longitude=10.8394)
    coordinator.update_device_cache(_SIBLING, own_fix | {"identity_key": _SHARED_KEY})
    sibling_before = _position(coordinator, _SIBLING)

    coordinator.update_device_cache(
        _DEVICE, _report(now) | {"identity_key": _SHARED_KEY}
    )
    assert _position(coordinator, _SIBLING) == sibling_before
    coordinator.update_device_cache(
        _SIBLING, _report(now) | {"identity_key": _SHARED_KEY}
    )

    assert _position(coordinator, _SIBLING) != sibling_before
    assert coordinator._device_location_data[_SIBLING]["status"] == "Fused (Weighted)"

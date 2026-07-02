# tests/test_coordinator_per_device_poll_interval.py
"""Tests for the per-device ``location_poll_interval`` feature (Direction A only).

Direction A lets a user poll an individual device *less* often than the
coordinator-wide cadence. The effective per-device floor is
``max(per_device_interval, effective_interval)`` so a configured value below
the coordinator cadence is clamped rather than silently ignored. The global
poll gate (``is_poll_cycle_due``) and ``_last_poll_mono`` stay untouched.

The characterization tests in this module pin today's ``devices_to_poll``
behavior *before* the feature exists, so a later regression in the additive
filter is caught.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Coroutine
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.const import (
    DEVICE_POLL_INTERVAL_MAX_S,
    DEVICE_POLL_INTERVAL_MIN_S,
    DOMAIN,
    coerce_device_poll_interval_mapping,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.update import (
    device_due_by_interval,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from tests.helpers.config_entries_stub import make_config_entry


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


class _EmptyResultAPI:
    """API stub that returns an empty result so the real cycle reaches its end."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        return []


class _RaiseOnDeviceAPI:
    """API stub that raises on a target device to force an early loop exit.

    ``SpotApiEmptyResponseError`` escalates to reauth inside the device loop and
    returns immediately, so any device scheduled after the target is never
    reached this cycle.
    """

    def __init__(self, raise_on: str) -> None:
        self._raise_on = raise_on
        self.calls: list[str] = []

    async def async_get_device_location(self, dev_id: str, _dev_name: str):
        self.calls.append(dev_id)
        if dev_id == self._raise_on:
            raise SpotApiEmptyResponseError("forced early exit")
        return []


@pytest.fixture
async def coordinator(monkeypatch: pytest.MonkeyPatch):
    """Instantiate a coordinator with patched dependencies for polling tests."""

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


def _prime_devices(
    coordinator: GoogleFindMyCoordinator, device_ids: list[str], wall_now: float
) -> None:
    """Seed cached device list, location data and overdue histories."""

    coordinator._last_device_list = [{"id": dev_id} for dev_id in device_ids]
    coordinator._last_list_poll_mono = time.monotonic()
    for dev_id in device_ids:
        coordinator._device_location_data[dev_id] = {"last_seen": wall_now}
        coordinator._device_update_history[dev_id] = deque(
            [wall_now - 900, wall_now - 600, wall_now - 350],
            maxlen=4,
        )


def _patch_side_effects(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neutralize registry/snapshot side effects irrelevant to poll selection."""

    monkeypatch.setattr(coordinator, "_ensure_service_device_exists", lambda: None)
    monkeypatch.setattr(
        coordinator, "_ensure_registry_for_devices", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(coordinator, "_refresh_subentry_index", lambda *_a, **_k: None)
    monkeypatch.setattr(
        coordinator,
        "_async_build_device_snapshot_with_fallbacks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(coordinator, "_store_subentry_snapshots", lambda _snap: None)


async def _run_cycle_and_capture(
    coordinator: GoogleFindMyCoordinator,
) -> list[str]:
    """Drive one update cycle and return the ids passed to the poll cycle."""

    captured: list[str] = []

    async def _poll_cycle(devices, *, force=False):
        captured.extend(dev["id"] for dev in devices)

    coordinator._async_start_poll_cycle = _poll_cycle
    coordinator._schedule_short_retry = lambda *_a, **_k: None

    await coordinator._async_update_data()
    poll_tasks = [
        task
        for task, name in coordinator.hass.created
        if name == f"{DOMAIN}.poll_cycle"
    ]
    for task in poll_tasks:
        await asyncio.wait_for(task, timeout=1)
    return captured


@pytest.mark.asyncio
async def test_characterization_all_devices_polled_without_per_device_interval(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterization: with no per-device interval set, every enabled device polls.

    Pins today's ``devices_to_poll`` behavior before the additive filter exists:
    an empty per-device cooldown and no per-device interval means all non-ignored
    devices participate in the cycle.
    """

    wall_now = time.time()
    _prime_devices(coordinator, ["dev-a", "dev-b"], wall_now)
    effective_interval = max(
        coordinator.location_poll_interval, coordinator.min_poll_interval
    )
    coordinator._last_poll_mono = time.monotonic() - effective_interval
    _patch_side_effects(coordinator, monkeypatch)

    captured = await _run_cycle_and_capture(coordinator)

    assert sorted(captured) == ["dev-a", "dev-b"]


@pytest.mark.asyncio
async def test_characterization_is_poll_cycle_due_unaffected(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the global poll gate stays interval-only, not per-device.

    ``is_poll_cycle_due`` must remain a pure function of the coordinator-wide
    cadence. This pins that a cycle below the cadence floor schedules no poll,
    independent of any per-device interval configuration.
    """

    wall_now = time.time()
    _prime_devices(coordinator, ["dev-a"], wall_now)
    # elapsed == min_poll_interval < effective_interval → below cadence floor.
    coordinator._last_poll_mono = time.monotonic() - coordinator.min_poll_interval
    _patch_side_effects(coordinator, monkeypatch)

    captured = await _run_cycle_and_capture(coordinator)

    assert captured == []


# --------------------------------------------------------------------------
# Pure predicate unit tests (device_due_by_interval)
# --------------------------------------------------------------------------


def test_predicate_never_polled_is_due() -> None:
    """A device never polled (stamp 0.0) is due regardless of its interval."""
    assert device_due_by_interval(
        now_mono=1000.0,
        last_poll_mono=0.0,
        per_device_interval=3600.0,
        effective_interval=200.0,
    )


def test_predicate_below_own_interval_is_not_due() -> None:
    """Within its own (larger-than-cadence) interval the device is skipped."""
    # floor = max(600, 200) = 600; elapsed = 100 < 600 → not due.
    assert not device_due_by_interval(
        now_mono=1100.0,
        last_poll_mono=1000.0,
        per_device_interval=600.0,
        effective_interval=200.0,
    )


def test_predicate_past_own_interval_is_due() -> None:
    """Past its own interval the device becomes due again."""
    # floor = 600; elapsed = 700 >= 600 → due.
    assert device_due_by_interval(
        now_mono=1700.0,
        last_poll_mono=1000.0,
        per_device_interval=600.0,
        effective_interval=200.0,
    )


def test_predicate_clamps_below_cadence_floor() -> None:
    """A per-device value below the cadence is clamped to effective_interval.

    Discriminates the ``max(...)`` clamp: a naive floor of the raw 100 s value
    would call the device due at elapsed 150 s, but the cadence floor of 200 s
    must dominate, so it is NOT due.
    """
    assert not device_due_by_interval(
        now_mono=1150.0,
        last_poll_mono=1000.0,
        per_device_interval=100.0,
        effective_interval=200.0,
    )
    # And once the cadence floor itself elapses, it is due.
    assert device_due_by_interval(
        now_mono=1200.0,
        last_poll_mono=1000.0,
        per_device_interval=100.0,
        effective_interval=200.0,
    )


# --------------------------------------------------------------------------
# Coordinator-level filter + stamp behavior
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_skips_device_within_its_interval(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device recently polled and set to a long interval is dropped this cycle.

    The fast device (no override) still polls; the slow device (3600 s override,
    stamped as just polled) is filtered out even though the global cycle is due.
    """

    wall_now = time.time()
    _prime_devices(coordinator, ["dev-fast", "dev-slow"], wall_now)
    effective_interval = max(
        coordinator.location_poll_interval, coordinator.min_poll_interval
    )
    coordinator._last_poll_mono = time.monotonic() - effective_interval
    # Slow device was just polled and wants a long interval.
    coordinator._device_last_poll_mono["dev-slow"] = time.monotonic()
    monkeypatch.setattr(
        coordinator, "_get_device_poll_interval_map", lambda: {"dev-slow": 3600}
    )
    _patch_side_effects(coordinator, monkeypatch)

    captured = await _run_cycle_and_capture(coordinator)

    assert captured == ["dev-fast"]


@pytest.mark.asyncio
async def test_stamp_written_at_real_poll_end_for_polled_devices(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that runs the real cycle gets its per-device stamp advanced.

    Drives the real ``_async_start_poll_cycle`` (empty-result API) so the stamp
    in the ``finally`` block executes at the same instant as ``_last_poll_mono``.
    """

    coordinator.api = _EmptyResultAPI()
    coordinator.config_entry = make_config_entry(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = [{"id": "dev-a"}]
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None

    devices = [{"id": "dev-a", "name": "A"}]
    before = time.monotonic()
    await coordinator._async_start_poll_cycle(devices, force=True)

    assert coordinator._device_last_poll_mono.get("dev-a", 0.0) >= before
    # Symmetry: the global baseline advanced at the same point.
    assert coordinator._last_poll_mono >= before


@pytest.mark.asyncio
async def test_stamp_not_written_when_global_gate_skips_cycle(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device the global gate never started keeps its (absent) stamp.

    Below the cadence floor no poll cycle runs, so ``_device_last_poll_mono``
    stays empty and the device remains due next time.
    """

    wall_now = time.time()
    _prime_devices(coordinator, ["dev-a"], wall_now)
    _patch_side_effects(coordinator, monkeypatch)

    # Below the cadence floor: no poll runs, so no stamp is written.
    coordinator._last_poll_mono = time.monotonic() - coordinator.min_poll_interval
    await _run_cycle_and_capture(coordinator)
    assert "dev-a" not in coordinator._device_last_poll_mono


@pytest.mark.asyncio
async def test_stamp_skips_devices_not_reached_on_early_exit(
    coordinator: GoogleFindMyCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An early mid-loop exit must not stamp devices the loop never reached.

    The cycle exits via reauth on ``dev-b``; ``dev-c`` is scheduled after it and
    is never polled. Its per-device floor must stay unstamped so it remains due,
    while the attempted devices (``dev-a``, ``dev-b``) advance their floor.
    """

    api = _RaiseOnDeviceAPI(raise_on="dev-b")
    coordinator.api = api
    coordinator.config_entry = make_config_entry(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = [{"id": "dev-a"}, {"id": "dev-b"}, {"id": "dev-c"}]
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None
    monkeypatch.setattr(coordinator, "_request_poll_reauth", lambda *_a, **_k: None)
    monkeypatch.setattr(coordinator, "_set_auth_state", lambda *_a, **_k: None)

    devices = [
        {"id": "dev-a", "name": "A"},
        {"id": "dev-b", "name": "B"},
        {"id": "dev-c", "name": "C"},
    ]
    await coordinator._async_start_poll_cycle(devices, force=True)

    # dev-c was never reached (loop returned on dev-b): it must stay unstamped.
    assert "dev-c" not in coordinator._device_last_poll_mono
    assert api.calls == ["dev-a", "dev-b"]
    # Devices that actually entered the request path advanced their floor.
    assert "dev-a" in coordinator._device_last_poll_mono
    assert "dev-b" in coordinator._device_last_poll_mono


# --------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------


def test_coerce_drops_invalid_and_clamps() -> None:
    """Coercion keeps valid entries, clamps to bounds, drops junk."""
    raw = {
        "dev-ok": 600,
        "dev-str": "900",
        "dev-low": 10,  # below min → clamped up
        "dev-high": 99999,  # above max → clamped down
        "dev-bool": True,  # bool rejected
        "dev-none": None,  # non-numeric rejected
        123: 600,  # non-str key rejected
        "dev-bad": "abc",  # unparsable string rejected
    }
    result = coerce_device_poll_interval_mapping(raw)
    assert result == {
        "dev-ok": 600,
        "dev-str": 900,
        "dev-low": DEVICE_POLL_INTERVAL_MIN_S,
        "dev-high": DEVICE_POLL_INTERVAL_MAX_S,
    }


def test_coerce_non_mapping_returns_empty() -> None:
    """A non-mapping stored value coerces to an empty map (never raises)."""
    assert coerce_device_poll_interval_mapping(["dev-a", "dev-b"]) == {}
    assert coerce_device_poll_interval_mapping(None) == {}


def test_coerce_drops_non_finite_floats() -> None:
    """NaN and +/-inf are dropped, never raised (Codex #1157 hardening).

    ``int(float("nan"))`` raises ValueError and ``int(float("inf"))`` raises
    OverflowError. A corrupt ``.storage`` edit or a direct-options writer could
    store such a value; the coercer must honour its drop-invalid contract so one
    bad entry cannot disable every valid override or break the options screen.
    """
    raw = {
        "dev-ok": 300,
        "dev-nan": float("nan"),
        "dev-inf": float("inf"),
        "dev-ninf": float("-inf"),
    }
    result = coerce_device_poll_interval_mapping(raw)
    assert result == {"dev-ok": 300}


# --------------------------------------------------------------------------
# Coordinator live options resolution (_get_device_poll_interval_map)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_option_resolution_reads_and_coerces(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """The map is resolved live from ``entry.options`` and coerced/clamped."""
    from custom_components.googlefindmy.const import OPT_DEVICE_POLL_INTERVAL

    coordinator.config_entry = SimpleNamespace(
        options={OPT_DEVICE_POLL_INTERVAL: {"dev-a": 600, "dev-low": 10}}
    )
    resolved = coordinator._get_device_poll_interval_map()
    assert resolved == {"dev-a": 600, "dev-low": DEVICE_POLL_INTERVAL_MIN_S}

    # An options reload propagates without restart (live read).
    coordinator.config_entry.options = {OPT_DEVICE_POLL_INTERVAL: {"dev-a": 1200}}
    assert coordinator._get_device_poll_interval_map() == {"dev-a": 1200}


@pytest.mark.asyncio
async def test_live_option_resolution_defensive_on_bad_entry(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """A raising options access never breaks polling; returns an empty map."""

    class _BadOptions:
        def get(self, _key, _default=None):
            raise RuntimeError("boom")

    coordinator.config_entry = SimpleNamespace(options=_BadOptions())
    assert coordinator._get_device_poll_interval_map() == {}

# tests/test_eid_resolver_debounce.py
"""Trigger-task debounce gate for EID-resolver refresh (AP-B2).

Two distinct call sites each spawn an ``async_create_task`` that runs the
global EID resolver's ``async_refresh``:

* the central helper ``_schedule_eid_resolver_refresh`` in
  ``coordinator/identity.py`` (driven by four read-only callers), and
* the inline path ``_schedule_inline_eid_resolver_refresh`` in
  ``coordinator/cache.py`` (driven from ``_persist_anchor_metadata`` when an
  anchored payload carries an identity key).

Under an FCM burst these triggers arrive back-to-back. AP-B2 adds a per-site
task debounce that collapses a burst within ``_EID_REFRESH_DEBOUNCE_S`` to a
single created task, while still arming a fresh timer (and thus a fresh task)
for a trigger that arrives after the window. These tests pin three contracts
per site:

* **Burst coalescing** -- >= 3 triggers inside the window create <= 1 task.
* **No swallowed window** -- a trigger after the window's timer has fired
  creates exactly one further task.
* **Independent fire** -- once the timer callback runs the resolver's
  ``async_refresh`` coroutine is actually scheduled.

These assertions count *task creation* (the ``async_create_task`` /
``call_later`` seam), not refreshes; the resolver's own ``_pending_refresh``
coalescing happens only after a task has started and is out of scope here.
The byte-exact EID correctness lives in
``tests/test_eid_resolver_characterization.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import (
    _EID_REFRESH_DEBOUNCE_S,
    DATA_EID_RESOLVER,
    DOMAIN,
)


class _FakeLoop:
    """Minimal event-loop stand-in capturing ``call_later`` scheduling.

    ``call_later`` records the callback and returns a handle whose
    ``cancel`` marks it cancelled. ``fire_all`` invokes every still-armed
    callback once, mimicking the debounce window elapsing.
    """

    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Any, _FakeTimerHandle]] = []

    def call_later(self, delay: float, callback: Any) -> _FakeTimerHandle:
        handle = _FakeTimerHandle()
        self.scheduled.append((delay, callback, handle))
        return handle

    def fire_all(self) -> None:
        """Invoke all armed (non-cancelled) callbacks; drain the queue."""
        pending = list(self.scheduled)
        self.scheduled.clear()
        for _delay, callback, handle in pending:
            if not handle.cancelled:
                callback()


class _FakeTimerHandle:
    """Stand-in for ``asyncio.TimerHandle`` recording cancellation."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def _fake_hass(eid_resolver: Any) -> SimpleNamespace:
    """Return a hass stub with a fake loop and a task-counting create_task."""

    created: list[Any] = []

    def create_task(coro: Any, name: str | None = None) -> MagicMock:
        created.append(coro)
        if hasattr(coro, "close"):
            coro.close()  # avoid un-awaited coroutine warnings
        return MagicMock()

    return SimpleNamespace(
        async_create_task=create_task,
        async_create_background_task=create_task,
        loop=_FakeLoop(),
        data={DOMAIN: {DATA_EID_RESOLVER: eid_resolver}},
        _created_tasks=created,
    )


def _fake_resolver() -> SimpleNamespace:
    """Return a resolver stub whose ``async_refresh`` yields a closeable coro."""

    async def _async_refresh(payload: Any = None) -> None:
        return None

    return SimpleNamespace(async_refresh=_async_refresh)


def _make_coordinator() -> Any:
    """Create a minimal coordinator instance carrying both trigger sites."""

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = _fake_hass(_fake_resolver())
    coordinator._device_location_data = {}
    coordinator._eid_refresh_debounce_handle = None
    coordinator._eid_inline_refresh_debounce_handle = None
    return coordinator


# ---------------------------------------------------------------------------
# Central helper site: _schedule_eid_resolver_refresh (identity.py)
# ---------------------------------------------------------------------------


def test_central_helper_coalesces_burst_to_single_task() -> None:
    """>= 3 central-helper triggers inside the window create <= 1 task."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    for _ in range(5):
        coordinator._schedule_eid_resolver_refresh()

    # Burst armed exactly one timer and created no task yet.
    assert len(loop.scheduled) == 1
    assert loop.scheduled[0][0] == _EID_REFRESH_DEBOUNCE_S
    assert len(coordinator.hass._created_tasks) == 0

    # Window elapses -> exactly one task is created.
    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 1


def test_central_helper_does_not_swallow_post_window_trigger() -> None:
    """A central-helper trigger after the window fires creates one more task."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    coordinator._schedule_eid_resolver_refresh()
    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 1

    # New window: a fresh trigger must arm a fresh timer and create a task.
    coordinator._schedule_eid_resolver_refresh()
    assert len(loop.scheduled) == 1
    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 2


def test_central_helper_falls_back_without_loop() -> None:
    """Without a usable loop the helper creates the task immediately (legacy)."""

    coordinator = _make_coordinator()
    coordinator.hass.loop = None  # no call_later available

    coordinator._schedule_eid_resolver_refresh()
    assert len(coordinator.hass._created_tasks) == 1


# ---------------------------------------------------------------------------
# Inline site: _schedule_inline_eid_resolver_refresh (cache.py)
# ---------------------------------------------------------------------------


def test_inline_site_coalesces_burst_to_single_task() -> None:
    """>= 3 inline triggers inside the window create <= 1 task."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    for _ in range(4):
        coordinator._schedule_inline_eid_resolver_refresh("dev-1")

    assert len(loop.scheduled) == 1
    assert loop.scheduled[0][0] == _EID_REFRESH_DEBOUNCE_S
    assert len(coordinator.hass._created_tasks) == 0

    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 1


def test_inline_site_does_not_swallow_post_window_trigger() -> None:
    """An inline trigger after the window fires creates one more task."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    coordinator._schedule_inline_eid_resolver_refresh("dev-1")
    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 1

    coordinator._schedule_inline_eid_resolver_refresh("dev-1")
    assert len(loop.scheduled) == 1
    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 2


def test_inline_site_triggered_via_persist_anchor_metadata() -> None:
    """The public ``_persist_anchor_metadata`` path drives the inline debounce.

    This proves the debounce sits on the real call site, not just on the
    extracted helper: two anchored payloads carrying an identity key inside the
    window still collapse to a single created task.
    """

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    payload = {"identity_key": b"\x01" * 32, "pair_date": 1_765_910_348}
    coordinator._persist_anchor_metadata("dev-1", dict(payload))
    coordinator._persist_anchor_metadata("dev-1", dict(payload))

    assert len(loop.scheduled) == 1
    assert len(coordinator.hass._created_tasks) == 0

    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 1


def test_persist_anchor_metadata_without_identity_key_schedules_nothing() -> None:
    """An anchored payload without an identity key never arms the debounce."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    coordinator._persist_anchor_metadata("dev-1", {"pair_date": 1_765_910_348})

    assert len(loop.scheduled) == 0
    assert len(coordinator.hass._created_tasks) == 0


def test_inline_site_falls_back_without_loop() -> None:
    """Without a usable loop the inline path creates the task immediately."""

    coordinator = _make_coordinator()
    coordinator.hass.loop = None

    coordinator._schedule_inline_eid_resolver_refresh("dev-1")
    assert len(coordinator.hass._created_tasks) == 1


# ---------------------------------------------------------------------------
# Defensive guards: every early bail-out path must short-circuit cleanly
# ---------------------------------------------------------------------------


def _coordinator_without_resolver(**hass_overrides: Any) -> Any:
    """Coordinator whose hass.data lacks a usable resolver, for guard tests."""

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {}
    coordinator._eid_refresh_debounce_handle = None
    coordinator._eid_inline_refresh_debounce_handle = None

    created: list[Any] = []

    def create_task(coro: Any, name: str | None = None) -> MagicMock:
        created.append(coro)
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    base = {
        "async_create_task": create_task,
        "loop": _FakeLoop(),
        "data": {},
        "_created_tasks": created,
    }
    base.update(hass_overrides)
    coordinator.hass = SimpleNamespace(**base)
    return coordinator


@pytest.mark.parametrize(
    "scheduler",
    ["_schedule_eid_resolver_refresh", "_schedule_inline_eid_resolver_refresh"],
)
def test_no_hass_short_circuits(scheduler: str) -> None:
    """Either site bails out cleanly when ``self.hass`` is absent."""

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._device_location_data = {}
    coordinator._eid_refresh_debounce_handle = None
    coordinator._eid_inline_refresh_debounce_handle = None
    coordinator.hass = None  # type: ignore[assignment]

    fn = getattr(coordinator, scheduler)
    fn("dev-1") if "inline" in scheduler else fn()  # no raise = pass


def test_inline_guards_bail_without_resolver_or_bucket() -> None:
    """Inline site bails when the domain bucket / resolver is missing or unusable."""

    # Non-dict bucket -> early return.
    coord = _coordinator_without_resolver(data={DOMAIN: "not-a-dict"})
    coord._schedule_inline_eid_resolver_refresh("dev-1")
    assert len(coord.hass._created_tasks) == 0

    # Bucket present but no resolver entry -> early return.
    coord = _coordinator_without_resolver(data={DOMAIN: {}})
    coord._schedule_inline_eid_resolver_refresh("dev-1")
    assert len(coord.hass._created_tasks) == 0

    # Resolver present but without a callable async_refresh -> early return.
    coord = _coordinator_without_resolver(
        data={DOMAIN: {DATA_EID_RESOLVER: SimpleNamespace(async_refresh=None)}}
    )
    coord._schedule_inline_eid_resolver_refresh("dev-1")
    assert len(coord.hass._created_tasks) == 0

    # Resolver usable but hass has no create_task -> early return.
    coord = _coordinator_without_resolver(
        data={DOMAIN: {DATA_EID_RESOLVER: _fake_resolver()}}
    )
    del coord.hass.async_create_task
    coord._schedule_inline_eid_resolver_refresh("dev-1")
    assert coord._eid_inline_refresh_debounce_handle is None


def test_central_guards_bail_without_resolver_or_bucket() -> None:
    """Central helper bails when bucket / resolver / create_task is unusable."""

    coord = _coordinator_without_resolver(data={DOMAIN: "not-a-dict"})
    coord._schedule_eid_resolver_refresh()
    assert len(coord.hass._created_tasks) == 0

    coord = _coordinator_without_resolver(data={DOMAIN: {}})
    coord._schedule_eid_resolver_refresh()
    assert len(coord.hass._created_tasks) == 0

    coord = _coordinator_without_resolver(
        data={DOMAIN: {DATA_EID_RESOLVER: SimpleNamespace(async_refresh=None)}}
    )
    coord._schedule_eid_resolver_refresh()
    assert len(coord.hass._created_tasks) == 0

    coord = _coordinator_without_resolver(
        data={DOMAIN: {DATA_EID_RESOLVER: _fake_resolver()}}
    )
    del coord.hass.async_create_task
    coord._schedule_eid_resolver_refresh()
    assert len(coord.hass._created_tasks) == 0


# ---------------------------------------------------------------------------
# Cross-site isolation: each site keeps its own pending handle
# ---------------------------------------------------------------------------


def test_sites_have_independent_debounce_handles() -> None:
    """Central and inline sites coalesce independently (separate handles)."""

    coordinator = _make_coordinator()
    loop: _FakeLoop = coordinator.hass.loop

    coordinator._schedule_eid_resolver_refresh()
    coordinator._schedule_inline_eid_resolver_refresh("dev-1")

    # Two independent timers armed (one per site), still no task yet.
    assert len(loop.scheduled) == 2
    assert len(coordinator.hass._created_tasks) == 0

    loop.fire_all()
    assert len(coordinator.hass._created_tasks) == 2

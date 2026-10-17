# tests/test_coordinator_locate_request_clock.py
"""Per-device clock of sent location requests (``locate_requested_mono``).

The EID resolver reports how long before an observation the integration last
sent a location request for a device (``poll_age_s`` in
``FMDN_UWT_TRANSITION``). The transport calls a dispatch observer immediately
before each POST, and both send sites wrap their await in ``stamp_on_dispatch``
so that this call, and nothing earlier, writes the stamp. A reader that runs while an attempt is still in its push phase therefore
sees the previous value, and an attempt that stops before the hand-over leaves
nothing that would have to be taken back after a reader already logged it.
``_last_poll_mono`` cannot serve: it is a scheduling base that is back-dated by
the poll interval.

The fakes below stand in for ``api.async_get_device_location`` and call the
transport's own ``_notify_request_dispatch`` where the real flow would POST;
that the transport calls it exactly then is measured in
``test_nova_request.py``, and one test here drives the real
``GoogleFindMyAPI`` to show the observer survives that layer.

The join test at the end pins the second half of the contract: the resolver
looks the stamp up by ``EIDMatch.canonical_id``, which is produced by
``_extract_our_identifier`` from the device-registry identifier, while the
coordinator stamps by the snapshot ``device["id"]``. Both must be the same
string, with and without the entry-scoped prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers import locate_clock
from custom_components.googlefindmy.coordinator.helpers.locate_clock import (
    ensure_locate_stamps,
    stamp_on_dispatch,
)
from custom_components.googlefindmy.NovaApi import nova_request as nova_request_module
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)
from tests.helpers.api_stub import StubCache
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.locate_mixin_stub import LocateStub, make_hass_stub
from tests.helpers.registry_mixin_stub import RegistryStub


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

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        return self.loop.create_task(coro, name=name)


class _Clock:
    """``time.monotonic`` for ``locate_clock``, advanced by the fakes."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FlowAPI:
    """Stands in for ``api.async_get_device_location`` with a push phase.

    It reads the stamp during the push phase, then either stops there (raises
    without handing the request over) or hands it over, reads the stamp again,
    and ends with ``outcome``.
    """

    def __init__(
        self,
        *,
        clock: _Clock,
        hand_over: bool,
        outcome: BaseException | None,
        reader: Any,
        hand_over_at: float = 2000.0,
    ) -> None:
        self.clock = clock
        self.hand_over = hand_over
        self.outcome = outcome
        self.reader = reader
        self.hand_over_at = hand_over_at
        self.in_push_phase: list[float | None] = []
        self.after_hand_over: list[float | None] = []

    async def async_get_device_location(self, dev_id: str, _dev_name: str) -> Any:
        self.in_push_phase.append(self.reader(dev_id))
        await asyncio.sleep(0)
        if self.hand_over:
            self.clock.now = self.hand_over_at
            nova_request_module._notify_request_dispatch()
            self.after_hand_over.append(self.reader(dev_id))
        if self.outcome is not None:
            raise self.outcome
        return {}


# What can end an attempt. ``hand_over`` says whether the request layer reached
# the send call first; the stamp must follow that, never the kind of error.
_ENDINGS = [
    ("no-token", False, LocationRequestNotAcceptedError(stage="no_fcm_token")),
    (
        "registration",
        False,
        LocationRequestNotAcceptedError(stage="fcm_registration_failed"),
    ),
    ("timeout-in-push", False, TimeoutError()),
    ("config-error", False, None),
    ("server", True, LocationRequestNotAcceptedError(stage="server_error")),
    ("network", True, LocationRequestNotAcceptedError(stage="network_error")),
    ("timeout-after-send", True, TimeoutError()),
    ("empty-answer", True, None),
]


async def _cancel_stats_save(coordinator: GoogleFindMyCoordinator) -> None:
    save_task = coordinator._stats_save_task
    if save_task is not None:
        save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await save_task


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    api: object,
    devices: list[dict[str, str]],
) -> GoogleFindMyCoordinator:
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    coordinator = GoogleFindMyCoordinator(_DummyHass(loop), cache=_DummyCache())
    coordinator.config_entry = make_config_entry(
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
    coordinator.async_set_update_error = lambda exc: None
    coordinator.async_set_updated_data = lambda data: None
    return coordinator


@pytest.mark.asyncio
async def test_no_stamp_before_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    coordinator = _make_coordinator(monkeypatch, loop, object(), [])
    assert coordinator.locate_requested_mono("dev-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, 42.0], ids=["first", "earlier"])
@pytest.mark.parametrize(
    ("hand_over", "outcome"),
    [case[1:] for case in _ENDINGS],
    ids=[case[0] for case in _ENDINGS],
)
@pytest.mark.parametrize("built_by_init", [True, False], ids=["init", "lazy"])
async def test_poll_cycle_stamps_only_at_the_hand_over(
    monkeypatch: pytest.MonkeyPatch,
    built_by_init: bool,
    hand_over: bool,
    outcome: BaseException | None,
    previous: float | None,
) -> None:
    """The stamp is the hand-over time, and only a handed-over request writes it.

    During the push phase the reader sees the previous value. An attempt that
    ends before the hand-over leaves it unchanged; one that was handed over
    leaves the hand-over time (2000.0), not the start of the attempt (1000.0),
    whatever happens afterwards. A device the cycle never tried has no stamp.
    """
    clock = _Clock(1000.0)
    monkeypatch.setattr(locate_clock.time, "monotonic", clock)
    loop = asyncio.get_running_loop()
    devices = [{"id": "dev-1", "name": "Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, None, devices)
    api = _FlowAPI(
        clock=clock,
        hand_over=hand_over,
        outcome=outcome,
        reader=coordinator.locate_requested_mono,
    )
    coordinator.api = api
    if not built_by_init:
        # A coordinator assembled without __init__ (many test harnesses do
        # this) gets the map on the first request instead of an AttributeError.
        del coordinator._locate_requested_mono
    elif previous is not None:
        coordinator._locate_requested_mono["dev-1"] = previous
    expected_before = previous if built_by_init else None

    await coordinator._async_start_poll_cycle(devices, force=True)
    await _cancel_stats_save(coordinator)

    assert api.in_push_phase == [expected_before]
    if hand_over:
        assert api.after_hand_over == [2000.0]
        assert coordinator.locate_requested_mono("dev-1") == 2000.0
    else:
        assert coordinator.locate_requested_mono("dev-1") == expected_before
    assert coordinator.locate_requested_mono("dev-never-tried") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, 900.0], ids=["first", "earlier"])
@pytest.mark.parametrize(
    ("hand_over", "outcome"),
    [case[1:] for case in _ENDINGS],
    ids=[case[0] for case in _ENDINGS],
)
async def test_manual_locate_stamps_only_at_the_hand_over(
    monkeypatch: pytest.MonkeyPatch,
    hand_over: bool,
    outcome: BaseException | None,
    previous: float | None,
) -> None:
    clock = _Clock(1000.0)
    monkeypatch.setattr(locate_clock.time, "monotonic", clock)
    coord = LocateStub(config_entry=make_config_entry(entry_id="locate-clock"))
    if previous is not None:
        coord._locate_requested_mono = {"dev-1": previous}

    def _read(dev: str) -> float | None:
        # Resolved at call time: the stub may get its map on the first request.
        return getattr(coord, "_locate_requested_mono", {}).get(dev)

    api = _FlowAPI(clock=clock, hand_over=hand_over, outcome=outcome, reader=_read)
    coord.api.async_get_device_location = api.async_get_device_location

    with contextlib.suppress(Exception):
        await coord.async_locate_device("dev-1")

    assert api.in_push_phase == [previous]
    expected = 2000.0 if hand_over else previous
    assert coord._locate_requested_mono.get("dev-1") == expected


@pytest.mark.asyncio
async def test_reader_sees_no_stamp_while_the_attempt_is_in_its_push_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P1 on #1366: a reader in the push phase must not log a stamp that goes away.

    A reader (the EID resolver) runs while the manual locate waits in its push
    registration, and the registration then fails. With a stamp written before
    the await, the reader saw 1000.0 and logged it, and the later retraction
    could not take the logged value back. Now the reader sees the previous value
    both during and after the attempt.
    """
    clock = _Clock(1000.0)
    monkeypatch.setattr(locate_clock.time, "monotonic", clock)
    coord = LocateStub(config_entry=make_config_entry(entry_id="locate-clock"))
    coord._locate_requested_mono = {"dev-1": 500.0}
    in_push = asyncio.Event()
    release = asyncio.Event()

    async def _push_phase_then_fail(_dev: str, _name: str) -> Any:
        in_push.set()
        await release.wait()
        raise LocationRequestNotAcceptedError(stage="fcm_registration_failed")

    coord.api.async_get_device_location = AsyncMock(side_effect=_push_phase_then_fail)
    attempt = asyncio.create_task(coord.async_locate_device("dev-1"))
    await in_push.wait()

    # The reader runs in another task, as the resolver does.
    assert coord._locate_requested_mono.get("dev-1") == 500.0

    release.set()
    with contextlib.suppress(Exception):
        await attempt
    assert coord._locate_requested_mono.get("dev-1") == 500.0


@pytest.mark.asyncio
async def test_overlapping_attempts_each_stamp_at_their_own_hand_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tasks, one device: each observer belongs to its task.

    The poll hands over at 10.0 while the manual locate waits in its push
    phase; the manual locate then hands over at 20.0. A third attempt that fails
    in its push phase changes nothing.
    """
    clock = _Clock(0.0)
    monkeypatch.setattr(locate_clock.time, "monotonic", clock)
    stamps: dict[str, float] = {}
    manual_waiting = asyncio.Event()
    manual_go = asyncio.Event()

    async def _manual() -> None:
        with stamp_on_dispatch(stamps, "dev-1"):
            manual_waiting.set()
            await manual_go.wait()
            clock.now = 20.0
            nova_request_module._notify_request_dispatch()

    async def _poll() -> None:
        with stamp_on_dispatch(stamps, "dev-1"):
            clock.now = 10.0
            nova_request_module._notify_request_dispatch()

    async def _failing() -> None:
        with stamp_on_dispatch(stamps, "dev-1"):
            await asyncio.sleep(0)

    manual = asyncio.create_task(_manual())
    await manual_waiting.wait()
    await _poll()
    assert stamps == {"dev-1": 10.0}
    manual_go.set()
    await manual
    assert stamps == {"dev-1": 20.0}
    await _failing()
    assert stamps == {"dev-1": 20.0}


@pytest.mark.asyncio
async def test_a_task_started_outside_the_block_does_not_stamp() -> None:
    """The observer travels with the context of the awaiting task only."""
    stamps: dict[str, float] = {}
    gate = asyncio.Event()

    async def _elsewhere() -> None:
        await gate.wait()
        nova_request_module._notify_request_dispatch()

    other = asyncio.create_task(_elsewhere())
    with stamp_on_dispatch(stamps, "dev-1"):
        gate.set()
        await other
    assert stamps == {}


@pytest.mark.asyncio
async def test_a_task_started_inside_the_block_cannot_stamp_after_it() -> None:
    """A task created in the block inherits the observer, but it is closed on exit.

    The push receiver's supervisor is such a task: started on the first locate,
    it outlives the request. A POST it makes later must not stamp the device.
    """
    stamps: dict[str, float] = {}
    later = asyncio.Event()

    async def _outlives_the_block() -> None:
        await later.wait()
        nova_request_module._notify_request_dispatch()

    with stamp_on_dispatch(stamps, "dev-1"):
        child = asyncio.create_task(_outlives_the_block())
    later.set()
    await child
    assert stamps == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("posted", [True, False], ids=["posted", "stopped-early"])
async def test_the_observer_survives_the_real_api_layer(
    monkeypatch: pytest.MonkeyPatch, posted: bool
) -> None:
    """``GoogleFindMyAPI.async_get_device_location`` keeps the caller's context.

    Only the request layer below it is replaced; if the API ever ran the
    request in a fresh context (an executor job, ``Context().run``), the
    ``posted`` case would lose its stamp.
    """
    monkeypatch.setattr(locate_clock.time, "monotonic", _Clock(7.0))

    async def _request(*_a: Any, **_k: Any) -> list[Any]:
        if posted:
            nova_request_module._notify_request_dispatch()
        return []

    monkeypatch.setattr(api_module, "get_location_data_for_device", _request)
    api = GoogleFindMyAPI(cache=StubCache(entry_id="entry-1"), session=None)
    stamps: dict[str, float] = {}
    with stamp_on_dispatch(stamps, "dev-1"):
        await api.async_get_device_location("dev-1", "Tag")
    assert stamps == ({"dev-1": 7.0} if posted else {})


def test_ensure_locate_stamps_keeps_values_and_identity() -> None:
    existing = {"dev-1": 1.0}
    assert ensure_locate_stamps(existing) is existing
    fresh = ensure_locate_stamps(None)
    assert fresh == {}
    assert fresh is not ensure_locate_stamps(None)


@pytest.mark.asyncio
async def test_purge_device_drops_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted device re-added under the same id starts without a stamp."""
    loop = asyncio.get_running_loop()
    coordinator = _make_coordinator(monkeypatch, loop, object(), [])
    coordinator._locate_requested_mono["dev-gone"] = 5.0
    coordinator._locate_requested_mono["dev-kept"] = 6.0
    coordinator._is_on_hass_loop = lambda: True
    coordinator.purge_device("dev-gone")
    assert coordinator.locate_requested_mono("dev-gone") is None
    assert coordinator.locate_requested_mono("dev-kept") == 6.0


def test_reader_answers_none_on_a_coordinator_built_without_init() -> None:
    bare = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    assert bare.locate_requested_mono("dev-1") is None


@pytest.mark.parametrize(
    "registry_identifier",
    ["entry-xyz:dev-abc123", "dev-abc123"],
    ids=["entry_scoped", "legacy"],
)
def test_canonical_id_joins_the_snapshot_device_id(registry_identifier: str) -> None:
    """``EIDMatch.canonical_id`` equals the id the coordinator stamps by.

    The resolver derives ``canonical_id`` with ``_extract_our_identifier`` from
    the device-registry identifier (``coordinator/identity.py``), the poll and
    manual locate stamp by the snapshot ``device["id"]``.
    """
    snapshot_device = {"id": "dev-abc123", "name": "Tag"}
    coord = RegistryStub(
        hass=make_hass_stub(), config_entry=make_config_entry(entry_id="entry-xyz")
    )
    registry_device = SimpleNamespace(identifiers={(DOMAIN, registry_identifier)})
    assert coord._extract_our_identifier(registry_device) == snapshot_device["id"]

# tests/test_coordinator_locate_request_clock.py
"""Per-device clock of attempted location requests (``locate_requested_mono``).

The EID resolver reports how long before an observation the integration last
tried to ask Google for a device's location (``poll_age_s`` in
``FMDN_UWT_TRANSITION``). That needs a stamp per device that is set when a
request is attempted, by either send site, and that survives a request which
raises after it was sent. A request the request layer reports as never handed
to Google (``before_dispatch``) is taken back, and the value before it comes
back: otherwise a push outage would look like a request Google received.
``_last_poll_mono`` cannot serve: it is a scheduling base that is back-dated by
the poll interval.

The join test at the end pins the second half of the contract: the resolver
looks the stamp up by ``EIDMatch.canonical_id``, which is produced by
``_extract_our_identifier`` from the device-registry identifier, while the
coordinator stamps by the snapshot ``device["id"]``. Both must be the same
string, with and without the entry-scoped prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import pickle
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.locate_clock import (
    MAX_TRACKED_ATTEMPTS,
    LocateStamps,
    ensure_locate_stamps,
    retract_if_never_dispatched,
    stamp_locate_attempt,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)
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


class _RaisingAPI:
    """Records the stamp seen while the request is in flight, then fails."""

    def __init__(self) -> None:
        self.coordinator: GoogleFindMyCoordinator | None = None
        self.seen_in_flight: dict[str, float | None] = {}

    async def async_get_device_location(self, dev_id: str, _dev_name: str) -> Any:
        assert self.coordinator is not None
        self.seen_in_flight[dev_id] = self.coordinator.locate_requested_mono(dev_id)
        raise TimeoutError()


class _NotAcceptedAPI:
    """Records the stamp seen in flight, then reports a request not accepted."""

    def __init__(self, stage: str, dispatched: bool) -> None:
        self.stage = stage
        self.dispatched = dispatched
        self.coordinator: GoogleFindMyCoordinator | None = None
        self.seen_in_flight: dict[str, float | None] = {}

    async def async_get_device_location(self, dev_id: str, _dev_name: str) -> Any:
        assert self.coordinator is not None
        self.seen_in_flight[dev_id] = self.coordinator.locate_requested_mono(dev_id)
        raise LocationRequestNotAcceptedError(
            stage=self.stage, dispatched=self.dispatched
        )


# The coordinator decides on ``dispatched`` alone, never on the stage name.
# ``surfacing_before_accept`` is on both lists for that reason: the request
# layer raises it before and after the send (measured in
# ``test_location_request_not_accepted.py``), and the stamp must follow the flag.
_NEVER_SENT = ("no_fcm_token", "fcm_registration_failed", "surfacing_before_accept")
_MAYBE_SENT = (
    "nova_request_failed",
    "network_error",
    "rate_limited",
    "server_error",
    "surfacing_before_accept",
)


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
    coordinator = _make_coordinator(monkeypatch, loop, _RaisingAPI(), [])
    assert coordinator.locate_requested_mono("dev-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("built_by_init", [True, False], ids=["init", "lazy"])
async def test_poll_cycle_stamps_before_the_await_even_when_it_raises(
    monkeypatch: pytest.MonkeyPatch, built_by_init: bool
) -> None:
    """The stamp is set before the request, not in a ``finally``.

    The API reads the stamp while the request is in flight and then raises;
    the stamp must already be there inside the call and must still be there
    after the failed cycle. A device the cycle never tried has no stamp.
    """
    loop = asyncio.get_running_loop()
    api = _RaisingAPI()
    devices = [{"id": "dev-tried", "name": "Tried Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, api, devices)
    if not built_by_init:
        # A coordinator assembled without __init__ (many test harnesses do
        # this) gets the map on the first request instead of an AttributeError.
        del coordinator._locate_requested_mono
    api.coordinator = coordinator
    await coordinator._async_start_poll_cycle(devices, force=True)
    # The cycle schedules a debounced stats write; cancel it so no task
    # outlives the test.
    save_task = coordinator._stats_save_task
    if save_task is not None:
        save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await save_task

    in_flight = api.seen_in_flight["dev-tried"]
    assert isinstance(in_flight, float)
    assert coordinator.locate_requested_mono("dev-tried") == in_flight
    assert coordinator.locate_requested_mono("dev-never-tried") is None


@pytest.mark.asyncio
async def test_manual_locate_stamps_before_the_await_even_when_it_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.locate.time.monotonic",
        lambda: 1000.0,
    )
    coord = LocateStub(config_entry=make_config_entry(entry_id="locate-clock"))
    seen: list[float | None] = []

    async def _fail(device_id: str, _name: str) -> Any:
        seen.append(coord._locate_requested_mono.get(device_id))
        raise RuntimeError("request attempt failed")

    coord.api.async_get_device_location = AsyncMock(side_effect=_fail)

    with pytest.raises(Exception):
        await coord.async_locate_device("dev-1")

    assert seen == [1000.0]
    assert coord._locate_requested_mono == {"dev-1": 1000.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, 42.0], ids=["first", "earlier"])
@pytest.mark.parametrize("stage", _NEVER_SENT)
async def test_poll_cycle_takes_back_a_request_that_was_never_sent(
    monkeypatch: pytest.MonkeyPatch, stage: str, previous: float | None
) -> None:
    """A request never handed to Google must not count.

    The stamp is still set while the request is in flight (the request layer
    only finds out inside the call), and afterwards the value it replaced is
    back: none at all for a first attempt, the earlier stamp otherwise.
    """
    loop = asyncio.get_running_loop()
    api = _NotAcceptedAPI(stage, dispatched=False)
    devices = [{"id": "dev-1", "name": "Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, api, devices)
    if previous is not None:
        coordinator._locate_requested_mono["dev-1"] = previous
    api.coordinator = coordinator
    await coordinator._async_start_poll_cycle(devices, force=True)
    await _cancel_stats_save(coordinator)

    in_flight = api.seen_in_flight["dev-1"]
    assert isinstance(in_flight, float)
    assert in_flight != previous
    assert coordinator.locate_requested_mono("dev-1") == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", _MAYBE_SENT)
async def test_poll_cycle_keeps_a_request_that_may_have_been_sent(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A request that may have reached Google still counts, whatever the stage."""
    loop = asyncio.get_running_loop()
    api = _NotAcceptedAPI(stage, dispatched=True)
    devices = [{"id": "dev-1", "name": "Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, api, devices)
    coordinator._locate_requested_mono["dev-1"] = 42.0
    api.coordinator = coordinator
    await coordinator._async_start_poll_cycle(devices, force=True)
    await _cancel_stats_save(coordinator)

    in_flight = api.seen_in_flight["dev-1"]
    assert isinstance(in_flight, float)
    assert coordinator.locate_requested_mono("dev-1") == in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, 900.0], ids=["first", "earlier"])
@pytest.mark.parametrize(
    ("stage", "never_sent"),
    [(stage, True) for stage in _NEVER_SENT]
    + [(stage, False) for stage in _MAYBE_SENT],
)
async def test_manual_locate_takes_back_only_a_request_that_was_never_sent(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    never_sent: bool,
    previous: float | None,
) -> None:
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.locate.time.monotonic",
        lambda: 1000.0,
    )
    coord = LocateStub(config_entry=make_config_entry(entry_id="locate-clock"))
    if previous is not None:
        coord._locate_requested_mono = {"dev-1": previous}
    seen: list[float | None] = []

    async def _not_accepted(device_id: str, _name: str) -> Any:
        seen.append(coord._locate_requested_mono.get(device_id))
        raise LocationRequestNotAcceptedError(stage=stage, dispatched=not never_sent)

    coord.api.async_get_device_location = AsyncMock(side_effect=_not_accepted)

    assert await coord.async_locate_device("dev-1") == {}

    assert seen == [1000.0]
    expected = previous if never_sent else 1000.0
    assert coord._locate_requested_mono.get("dev-1") == expected


_NEVER = LocationRequestNotAcceptedError(stage="no_fcm_token", dispatched=False)
_MAYBE = LocationRequestNotAcceptedError(stage="server_error", dispatched=True)


def test_retraction_ignores_a_request_that_may_have_been_sent() -> None:
    stamps = LocateStamps([("dev-1", 1.0)])
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    assert retract_if_never_dispatched(stamps, token, _MAYBE) is False
    assert stamps == {"dev-1": 2.0}


def test_retraction_leaves_a_stamp_it_did_not_set() -> None:
    """An outside write or a purge owns the slot now; the old token must not undo it."""
    stamps = LocateStamps([("dev-1", 1.0)])
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    stamps["dev-1"] = 3.0  # an outside write
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False
    assert stamps == {"dev-1": 3.0}

    stamps = LocateStamps()
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    del stamps["dev-1"]
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False
    assert stamps == {}

    stamps = LocateStamps()
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    stamps.pop("dev-1", None)  # purge_device
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False
    assert stamps == {}

    stamps = LocateStamps([("dev-2", 7.0)])
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    assert retract_if_never_dispatched(stamps, token, _NEVER) is True
    assert stamps == {"dev-2": 7.0}

    stamps = LocateStamps()
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    stamps.clear()
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False
    assert stamps == {}


def test_overlapping_attempts_never_restore_an_attempt_taken_back() -> None:
    """A manual locate in its push phase while the poll cycle asks again (R2-4).

    Both fail before the send, the older one first. The value before both
    must come back, not the older attempt's stamp.
    """
    stamps = LocateStamps([("dev-1", 5.0)])
    manual = stamp_locate_attempt(stamps, "dev-1", 10.0)
    poll = stamp_locate_attempt(stamps, "dev-1", 20.0)
    assert retract_if_never_dispatched(stamps, manual, _NEVER) is True
    assert stamps == {"dev-1": 20.0}
    assert retract_if_never_dispatched(stamps, poll, _NEVER) is True
    assert stamps == {"dev-1": 5.0}


def test_overlapping_attempts_keep_the_one_that_may_have_been_sent() -> None:
    stamps = LocateStamps()
    manual = stamp_locate_attempt(stamps, "dev-1", 10.0)
    poll = stamp_locate_attempt(stamps, "dev-1", 20.0)
    assert retract_if_never_dispatched(stamps, poll, _NEVER) is True
    assert stamps == {"dev-1": 10.0}
    assert retract_if_never_dispatched(stamps, manual, _MAYBE) is False
    assert stamps == {"dev-1": 10.0}


def test_taking_back_the_oldest_of_three_keeps_the_newest_visible() -> None:
    stamps = LocateStamps()
    oldest = stamp_locate_attempt(stamps, "dev-1", 10.0)
    stamp_locate_attempt(stamps, "dev-1", 20.0)
    stamp_locate_attempt(stamps, "dev-1", 30.0)
    assert retract_if_never_dispatched(stamps, oldest, _NEVER) is True
    assert stamps == {"dev-1": 30.0}


def test_two_attempts_on_the_same_clock_reading_stay_apart() -> None:
    """A frozen or coarse clock gives two attempts the same stamp (R2-5).

    Taking back the older one must not remove the newer one.
    """
    stamps = LocateStamps()
    older = stamp_locate_attempt(stamps, "dev-1", 1000.0)
    newer = stamp_locate_attempt(stamps, "dev-1", 1000.0)
    assert retract_if_never_dispatched(stamps, older, _NEVER) is True
    assert stamps == {"dev-1": 1000.0}
    assert retract_if_never_dispatched(stamps, older, _NEVER) is False
    assert retract_if_never_dispatched(stamps, newer, _NEVER) is True
    assert stamps == {}


def test_an_attempt_beyond_the_kept_window_is_folded_into_the_base() -> None:
    """Only the newest attempts can be taken back; the list stays bounded."""
    stamps = LocateStamps()
    tokens = [
        stamp_locate_attempt(stamps, "dev-1", float(index))
        for index in range(MAX_TRACKED_ATTEMPTS + 1)
    ]
    assert retract_if_never_dispatched(stamps, tokens[0], _NEVER) is False
    for token in reversed(tokens[1:]):
        assert retract_if_never_dispatched(stamps, token, _NEVER) is True
    assert stamps == {"dev-1": 0.0}


@pytest.mark.parametrize(
    "write",
    [
        lambda stamps: stamps.update({"dev-1": 9.0}),
        lambda stamps: stamps.update([("dev-1", 9.0)]),
        lambda stamps: stamps.__ior__({"dev-1": 9.0}),
        lambda stamps: stamps.popitem(),
    ],
    ids=["update", "update-pairs", "ior", "popitem"],
)
def test_every_outside_write_forgets_the_attempts(write: Any) -> None:
    """A value written from outside is not this attempt's to take back."""
    stamps = LocateStamps()
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    write(stamps)
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False


def test_setdefault_on_a_missing_device_forgets_stale_attempts() -> None:
    stamps = LocateStamps()
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    dict.pop(stamps, "dev-1")  # bypasses the bookkeeping on purpose
    assert stamps.setdefault("dev-1", 9.0) == 9.0
    assert retract_if_never_dispatched(stamps, token, _NEVER) is False
    assert stamps == {"dev-1": 9.0}


@pytest.mark.parametrize(
    "duplicate",
    [copy.copy, copy.deepcopy, lambda stamps: pickle.loads(pickle.dumps(stamps))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_a_copy_carries_the_values_but_no_attempts(duplicate: Any) -> None:
    stamps = LocateStamps([("dev-2", 7.0)])
    token = stamp_locate_attempt(stamps, "dev-1", 2.0)
    duplicated = duplicate(stamps)
    assert isinstance(duplicated, LocateStamps)
    assert duplicated == {"dev-1": 2.0, "dev-2": 7.0}
    assert retract_if_never_dispatched(duplicated, token, _NEVER) is False
    # The original keeps its attempts: a shallow copy must not share them.
    duplicated["dev-1"] = 3.0
    assert retract_if_never_dispatched(stamps, token, _NEVER) is True
    assert stamps == {"dev-2": 7.0}


def test_ensure_locate_stamps_keeps_values_and_identity() -> None:
    existing = LocateStamps([("dev-1", 1.0)])
    assert ensure_locate_stamps(existing) is existing
    converted = ensure_locate_stamps({"dev-1": 2.0})
    assert isinstance(converted, LocateStamps)
    assert converted == {"dev-1": 2.0}
    fresh = ensure_locate_stamps(None)
    assert isinstance(fresh, LocateStamps)
    assert fresh == {}


@pytest.mark.asyncio
async def test_purge_device_drops_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted device re-added under the same id starts without a stamp."""
    loop = asyncio.get_running_loop()
    coordinator = _make_coordinator(monkeypatch, loop, _RaisingAPI(), [])
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

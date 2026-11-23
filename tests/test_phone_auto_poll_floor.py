# tests/test_phone_auto_poll_floor.py
"""Floor for automatic requests to phones without a canonical ID.

A phone row (``locate_route == "phone"``) is asked automatically at most every
``max(max(location_poll_interval, min_poll_interval),
PHONE_MIN_AUTO_POLL_INTERVAL_S)`` seconds, measured from its POST stamp in
``_locate_requested_mono``. Trackers in the same account keep the account
interval. The switch "automatic location requests" is on without a choice and
skips the phone when it is off.

The clock is steered, never slept (C-12): ``polling.time`` is replaced by a
shifted ``time`` module while the POST stamps keep the real monotonic clock, so
each cycle sees the phone's stamp exactly ``shift`` seconds old.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import patch

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy import const
from custom_components.googlefindmy.api import GoogleFindMyAPI
from tests.test_coordinator_automatic_polling import (
    _DispatchingAPI,
    _make_coordinator,
    _ShiftedTime,
    polling_module,
)
from tests.test_phone_locate_flow import (
    _ANDROID_ID,
    _ENTRY_A,
    _PHONE_A1,
    _answering_nova,
    _Cache,
    _drain_background,
    _install_nova,
    _raw_action_numbers,
    _receiver,
)

pytestmark = pytest.mark.asyncio

_PHONE = {"id": _PHONE_A1, "name": "Phone A1", "locate_route": "phone"}
_TRACKER = {"id": "tracker-1", "name": "Tag 1"}
_OPT_DISABLED = "automatic_polling_disabled"
_OPT_ENABLED = "automatic_polling_enabled"


def _floor() -> int:
    value = getattr(const, "PHONE_MIN_AUTO_POLL_INTERVAL_S", None)
    if value is None:
        pytest.fail("const.PHONE_MIN_AUTO_POLL_INTERVAL_S is missing")
    return cast(int, value)


def _coordinator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval: int,
    options: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    api: Any = None,
) -> tuple[Any, Any]:
    api = api if api is not None else _DispatchingAPI()
    devices = devices or [_PHONE, _TRACKER]
    # Every device here is turned on unless the test stores its own choice.
    options = {_OPT_ENABLED: [d["id"] for d in devices], **(options or {})}
    coord = _make_coordinator(monkeypatch, options=options, api=api)
    coord.location_poll_interval = interval
    coord.min_poll_interval = const.DEFAULT_MIN_POLL_INTERVAL
    coord._last_device_list = [dict(d) for d in devices]
    return coord, api


async def _cycle(coord: Any, shift: float) -> None:
    """One cycle ``shift`` seconds after the first; always due for the account."""
    with patch.object(polling_module, "time", _ShiftedTime(shift)):
        coord._last_poll_mono = (
            time.monotonic() + shift - float(coord.location_poll_interval) - 1.0
        )
        # The device list stays fresh on the shifted clock (not under test here).
        coord._last_list_poll_mono = time.monotonic() + shift
        for dev in coord._last_device_list:
            coord._device_location_data[dev["id"]] = {"last_seen": time.time()}
        await coord._async_update_data()
        await _drain_background(coord)


def _calls_of(api: Any, dev_id: str) -> int:
    return sum(1 for call in api.calls if call == dev_id)


async def test_constant_is_the_single_source_and_others_are_unchanged() -> None:
    assert _floor() == 900
    assert "PHONE_MIN_AUTO_POLL_INTERVAL_S" in const.__all__
    # Regression check: the general bounds stay as they are.
    assert const.DEFAULT_MIN_POLL_INTERVAL == 60
    assert const.DEFAULT_LOCATION_POLL_INTERVAL == 300


async def test_phone_turned_on_is_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, api = _coordinator(monkeypatch, interval=300)
    await _cycle(coord, 0.0)
    assert _calls_of(api, _PHONE_A1) == 1
    assert _calls_of(api, _TRACKER["id"]) == 1


async def test_phone_without_a_choice_is_not_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone is off by default like every tracker; the tracker turned on is polled."""
    coord, api = _coordinator(
        monkeypatch, interval=300, options={_OPT_ENABLED: [_TRACKER["id"]]}
    )
    await _cycle(coord, 0.0)
    assert _calls_of(api, _PHONE_A1) == 0
    assert _calls_of(api, _TRACKER["id"]) == 1


async def test_phone_switched_off_is_not_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, api = _coordinator(
        monkeypatch, interval=300, options={_OPT_DISABLED: [_PHONE_A1]}
    )
    await _cycle(coord, 0.0)
    await _cycle(coord, 5000.0)
    assert _calls_of(api, _PHONE_A1) == 0
    assert _calls_of(api, _TRACKER["id"]) == 2


@pytest.mark.parametrize("interval", [60, 300])
async def test_short_account_interval_phone_waits_for_the_floor(
    monkeypatch: pytest.MonkeyPatch, interval: int
) -> None:
    """Account 60 s or 300 s: tracker every cycle, phone only after 900 s."""
    floor = _floor()
    coord, api = _coordinator(monkeypatch, interval=interval)

    await _cycle(coord, 0.0)
    assert (_calls_of(api, _PHONE_A1), _calls_of(api, _TRACKER["id"])) == (1, 1)

    await _cycle(coord, float(interval))
    assert (_calls_of(api, _PHONE_A1), _calls_of(api, _TRACKER["id"])) == (1, 2)

    await _cycle(coord, float(floor - 1))
    assert (_calls_of(api, _PHONE_A1), _calls_of(api, _TRACKER["id"])) == (1, 3)

    await _cycle(coord, float(floor))
    assert (_calls_of(api, _PHONE_A1), _calls_of(api, _TRACKER["id"])) == (2, 4)


async def test_long_account_interval_is_not_stretched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Account 1800 s: the phone follows the account interval (max wins)."""
    coord, api = _coordinator(monkeypatch, interval=1800)

    await _cycle(coord, 0.0)
    await _cycle(coord, 1799.0)
    assert _calls_of(api, _PHONE_A1) == 1
    await _cycle(coord, 1800.0)
    assert _calls_of(api, _PHONE_A1) == 2
    assert _calls_of(api, _TRACKER["id"]) == 3


async def test_manual_locate_stamp_moves_the_next_automatic_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "Locate now" writes the same POST stamp and moves the next one."""
    coord, api = _coordinator(monkeypatch, interval=300, devices=[_PHONE])
    coord._locate_requested_mono = {_PHONE_A1: time.monotonic()}

    await _cycle(coord, 600.0)
    assert _calls_of(api, _PHONE_A1) == 0
    await _cycle(coord, float(_floor()))
    assert _calls_of(api, _PHONE_A1) == 1


async def test_without_a_stamp_the_phone_is_due_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp lives in memory; after a restart the phone is due once."""
    coord, api = _coordinator(monkeypatch, interval=60, devices=[_PHONE])
    coord._locate_requested_mono = {}
    await _cycle(coord, 0.0)
    assert _calls_of(api, _PHONE_A1) == 1


async def test_tracker_with_a_fresh_stamp_is_not_held_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the phone route is gated; a tracker row ignores the floor."""
    coord, api = _coordinator(monkeypatch, interval=60, devices=[_TRACKER])
    coord._locate_requested_mono = {_TRACKER["id"]: time.monotonic()}
    await _cycle(coord, 60.0)
    assert _calls_of(api, _TRACKER["id"]) == 1


async def test_skip_line_names_id_and_floor_but_never_the_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _api = _coordinator(monkeypatch, interval=300, devices=[_PHONE])
    coord._locate_requested_mono = {_PHONE_A1: time.monotonic()}
    caplog.set_level("DEBUG", logger=polling_module.__name__)
    await _cycle(coord, 300.0)
    lines = [
        r.getMessage() for r in caplog.records if "skipping phone" in r.getMessage()
    ]
    assert len(lines) == 1
    assert _PHONE_A1 in lines[0]
    assert "floor 900s" in lines[0]
    assert "Phone A1" not in lines[0]


async def test_av_cycle_sends_a_phone_row_only_over_the_phone_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Action whitelist: the cycle reaches the phone only with action 4 on the wire."""
    rx = _receiver({_ENTRY_A: "push-token-of-a"})
    nova = _answering_nova(rx)
    _install_nova(monkeypatch, nova)
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    real_api = GoogleFindMyAPI(cache=_Cache(_ENTRY_A))
    real_api._phone_targets[_PHONE_A1] = _ANDROID_ID

    tracker_posts: list[str] = []

    async def _no_tracker_path(*args: Any, **_kwargs: Any) -> Any:
        tracker_posts.append(str(args[:1]))
        raise AssertionError("the tracker path must not run for a phone row")

    monkeypatch.setattr(
        api_module, "get_location_data_for_device", _no_tracker_path, raising=False
    )
    coord, _ = _coordinator(monkeypatch, interval=300, devices=[_PHONE], api=real_api)
    coord.api = real_api

    await _cycle(coord, 0.0)

    assert [_raw_action_numbers(p) for p, _ in nova.calls] == [{4}]
    assert tracker_posts == []

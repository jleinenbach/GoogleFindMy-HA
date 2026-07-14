# tests/test_bermuda_listener_contracts.py
"""Contract tests for the Bermuda area-debounce listener.

Covers ``custom_components/googlefindmy/fmdn_finder/bermuda_listener.py``:

* ``_async_debounced_area_upload`` — the five debounce exits (state cleared,
  area changed, superseded, already-scheduled, success) plus the H3 second
  line of defence: when the float-equality supersede check
  (``first_seen != debounce_start_time``, line 274) fails to discriminate two
  cycles that share an identical ``time.time()`` value, the
  ``upload_scheduled`` guard (line 284) still admits exactly one upload.
* ``_bermuda_state_changed`` (via the registered listener) — entity/area
  filters, the T-SAME branch (same area only refreshes ``last_seen``, no new
  task) and the T-NEW branch (area change spawns a debounce task), and the
  outer try/except swallowing inner errors.
* ``async_setup_bermuda_listener`` / ``async_unload_bermuda_listener`` —
  registration, cache init, and the no-listener unload branch.

``asyncio.sleep`` is patched to a no-op so the 30 s stabilization window never
actually waits.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.fmdn_finder import bermuda_listener as bl
from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
    ATTR_AREA,
    DATA_AREA_DEBOUNCE,
    DATA_BERMUDA_UNSUBSCRIBE,
    AreaDebounceState,
    async_setup_bermuda_listener,
    async_unload_bermuda_listener,
)

_ENTITY = "device_tracker.tag_bermuda_tracker_2"


def _hass(debounce: dict[str, AreaDebounceState] | None = None) -> SimpleNamespace:
    """Fake hass with a domain bucket and a task-swallowing create_task."""

    def _create_task(coro, name=None):  # noqa: ANN001, ANN202
        coro.close()  # avoid "coroutine never awaited" — we drive it directly
        return Mock()

    data = {DOMAIN: {DATA_AREA_DEBOUNCE: debounce if debounce is not None else {}}}
    return SimpleNamespace(
        data=data,
        bus=SimpleNamespace(async_listen=Mock(return_value=Mock(name="unsub"))),
        async_create_task=Mock(side_effect=_create_task),
    )


# --------------------------------------------------------------------------- #
# _async_debounced_area_upload — five exits + H3 guard                         #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually wait the 30 s stabilization window."""
    monkeypatch.setattr(bl.asyncio, "sleep", AsyncMock())


@pytest.fixture
def handle_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the terminal upload orchestrator to observe invocations."""
    mock = AsyncMock()
    monkeypatch.setattr(bl, "_async_handle_area_change", mock)
    return mock


async def _run_debounce(hass: SimpleNamespace, area: str, start: float) -> None:
    await bl._async_debounced_area_upload(hass, _ENTITY, area, {ATTR_AREA: area}, start)


@pytest.mark.asyncio
async def test_debounce_skips_when_state_cleared(handle_mock: AsyncMock) -> None:
    hass = _hass({})  # no state for the entity
    await _run_debounce(hass, "Kitchen", start=100.0)
    handle_mock.assert_not_called()


@pytest.mark.asyncio
async def test_debounce_skips_when_area_changed(handle_mock: AsyncMock) -> None:
    state = AreaDebounceState(_ENTITY, "Hallway", first_seen=100.0, last_seen=100.0)
    hass = _hass({_ENTITY: state})
    await _run_debounce(hass, "Kitchen", start=100.0)  # expected Kitchen, state Hallway
    handle_mock.assert_not_called()


@pytest.mark.asyncio
async def test_debounce_skips_when_superseded(handle_mock: AsyncMock) -> None:
    state = AreaDebounceState(_ENTITY, "Kitchen", first_seen=200.0, last_seen=200.0)
    hass = _hass({_ENTITY: state})
    await _run_debounce(hass, "Kitchen", start=100.0)  # start != state.first_seen
    handle_mock.assert_not_called()


@pytest.mark.asyncio
async def test_debounce_skips_when_already_scheduled(handle_mock: AsyncMock) -> None:
    state = AreaDebounceState(
        _ENTITY, "Kitchen", first_seen=100.0, last_seen=100.0, upload_scheduled=True
    )
    hass = _hass({_ENTITY: state})
    await _run_debounce(hass, "Kitchen", start=100.0)
    handle_mock.assert_not_called()


@pytest.mark.asyncio
async def test_debounce_success_uploads_and_marks_scheduled(
    handle_mock: AsyncMock,
) -> None:
    state = AreaDebounceState(_ENTITY, "Kitchen", first_seen=100.0, last_seen=100.0)
    hass = _hass({_ENTITY: state})
    await _run_debounce(hass, "Kitchen", start=100.0)
    handle_mock.assert_awaited_once()
    assert state.upload_scheduled is True


@pytest.mark.asyncio
async def test_h3_identical_timestamp_still_uploads_once(
    handle_mock: AsyncMock,
) -> None:
    """H3: two cycles sharing the same first_seen float upload only once.

    The supersede check compares floats (``first_seen != debounce_start_time``);
    with an identical ``time.time()`` value it cannot tell the two cycles apart,
    so both pass that check. The ``upload_scheduled`` guard is the second line
    of defence: the first cycle sets it, the second sees it and bails.
    """
    state = AreaDebounceState(_ENTITY, "Kitchen", first_seen=100.0, last_seen=100.0)
    hass = _hass({_ENTITY: state})
    await _run_debounce(hass, "Kitchen", start=100.0)  # cycle 1 -> uploads
    await _run_debounce(hass, "Kitchen", start=100.0)  # cycle 2 -> guard bails
    handle_mock.assert_awaited_once()  # exactly one upload despite equal timestamps


# --------------------------------------------------------------------------- #
# _bermuda_state_changed — filters and T-SAME / T-NEW transitions              #
# --------------------------------------------------------------------------- #


async def _capture_listener(hass: SimpleNamespace):  # noqa: ANN202
    """Run setup and return the registered outer state-change callback."""
    await async_setup_bermuda_listener(hass)
    return hass.bus.async_listen.call_args[0][1]


def _event(
    entity_id: str | None, new_area: str | None, old_area: str | None = None
) -> SimpleNamespace:
    new_state = (
        SimpleNamespace(attributes={ATTR_AREA: new_area} if new_area else {})
        if entity_id is not None
        else None
    )
    old_state = SimpleNamespace(attributes={ATTR_AREA: old_area}) if old_area else None
    return SimpleNamespace(
        data={"entity_id": entity_id, "new_state": new_state, "old_state": old_state}
    )


@pytest.mark.asyncio
async def test_setup_registers_listener_and_inits_caches() -> None:
    hass = _hass()
    hass.data = {}  # start empty to verify setdefault init
    await async_setup_bermuda_listener(hass)
    hass_bucket = hass.data[DOMAIN]
    assert DATA_AREA_DEBOUNCE in hass_bucket
    assert DATA_BERMUDA_UNSUBSCRIBE in hass_bucket


@pytest.mark.asyncio
async def test_listener_ignores_non_bermuda_entity() -> None:
    hass = _hass()
    listener = await _capture_listener(hass)
    listener(_event("device_tracker.some_phone", "Kitchen", old_area="Hall"))
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_listener_ignores_missing_area() -> None:
    hass = _hass()
    listener = await _capture_listener(hass)
    listener(_event(_ENTITY, None, old_area="Hall"))
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_listener_ignores_unchanged_area() -> None:
    hass = _hass()
    listener = await _capture_listener(hass)
    listener(_event(_ENTITY, "Kitchen", old_area="Kitchen"))
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_listener_same_area_refreshes_without_new_task() -> None:
    """T-SAME: an existing debounce for the same area only bumps last_seen."""
    state = AreaDebounceState(_ENTITY, "Kitchen", first_seen=1.0, last_seen=1.0)
    hass = _hass({_ENTITY: state})
    listener = await _capture_listener(hass)
    listener(_event(_ENTITY, "Kitchen", old_area="Hall"))
    hass.async_create_task.assert_not_called()  # no new debounce task
    assert state.last_seen > 1.0  # last_seen refreshed


@pytest.mark.asyncio
async def test_listener_area_change_spawns_debounce_task() -> None:
    """T-NEW: an area change writes a fresh state and schedules a task."""
    hass = _hass()
    listener = await _capture_listener(hass)
    listener(_event(_ENTITY, "Kitchen", old_area="Hall"))
    hass.async_create_task.assert_called_once()
    new_state = hass.data[DOMAIN][DATA_AREA_DEBOUNCE][_ENTITY]
    assert new_state.area == "Kitchen"
    assert new_state.upload_scheduled is False


@pytest.mark.asyncio
async def test_listener_missing_new_state_returns() -> None:
    hass = _hass()
    listener = await _capture_listener(hass)
    listener(_event(None, None))  # entity_id None, new_state None
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_listener_swallows_inner_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer callback must not propagate inner handler exceptions."""
    hass = _hass()
    listener = await _capture_listener(hass)
    # A new_state whose ``attributes`` has no ``.get`` makes the inner handler
    # raise AttributeError; the outer try/except must swallow it (event.data is
    # still a real mapping so the error logger itself does not crash).
    bad_event = SimpleNamespace(
        data={
            "entity_id": _ENTITY,
            "new_state": SimpleNamespace(attributes=object()),
            "old_state": None,
        }
    )
    listener(bad_event)
    hass.async_create_task.assert_not_called()


# --------------------------------------------------------------------------- #
# async_unload_bermuda_listener                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unload_calls_unsubscribe() -> None:
    unsub = Mock()
    hass = _hass()
    hass.data[DOMAIN][DATA_BERMUDA_UNSUBSCRIBE] = unsub
    await async_unload_bermuda_listener(hass)
    unsub.assert_called_once()
    assert DATA_BERMUDA_UNSUBSCRIBE not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_unload_without_listener_is_noop() -> None:
    hass = _hass()  # no DATA_BERMUDA_UNSUBSCRIBE registered
    await async_unload_bermuda_listener(hass)  # must not raise (line 759 branch)

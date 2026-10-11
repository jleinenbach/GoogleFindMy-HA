# tests/test_device_tracker_tick_writes.py
"""Ticks without a new fix must not make a tracker look fresher (#1321).

Home Assistant's ``person`` integration picks the tracker with the newest
``last_updated``. The tracker therefore (a) publishes ``location_age`` and
``location_status`` as of the last observation, so a coordinator tick writes
identical attributes, and (b) reports ``force_update`` as ``False``, so Core
treats an identical write as a report (``state_reported``, only
``last_reported`` moves) instead of a change. Every tick still writes, so Core
re-evaluates zones and ``last_reported`` keeps working as a heartbeat.

Two layers, because ``tests/conftest.py`` binds the integration to stubbed
entity base classes (re-importing it against the real ones is ruled out in
``tests/AGENTS.md``): the entity tests pin what the tracker publishes and that
it writes on every tick; the contract test feeds exactly that output through
the real state machine with the tracker's own ``force_update`` value. Time is
driven by ``freezer``, never by sleeping.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from tests.helpers.config_entries_stub import make_config_entry

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant


class _CoordinatorStub:
    """Coordinator serving one mutable device row."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.hass = SimpleNamespace()
        self.config_entry = make_config_entry(
            entry_id="entry-ticks", options={}, runtime_data=None
        )
        self.row = row

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def is_device_visible_in_subentry(self, key: str, device_id: str) -> bool:
        return True

    def is_device_present(self, device_id: str) -> bool:
        return True

    def get_device_location_data_for_subentry(
        self, key: str | None, device_id: str
    ) -> dict[str, Any] | None:
        return self.row

    def get_display_location_data_for_subentry(
        self, key: str | None, device_id: str
    ) -> dict[str, Any] | None:
        return self.row

    def get_subentry_snapshot(
        self, key: str | None = None, feature: str | None = None
    ) -> list[dict[str, Any]]:
        return [self.row] if self.row else []

    def stable_subentry_identifier(
        self, *, key: str | None = None, feature: str | None = None
    ) -> str | None:
        return key

    def get_subentry_metadata(
        self, *, key: str | None = None, feature: str | None = None
    ) -> Any:
        return SimpleNamespace(
            config_subentry_id=key,
            visible_device_ids=["dev-1"],
            enabled_device_ids=["dev-1"],
        )


def _row(last_seen: float, *, lat: float = 48.1) -> dict[str, Any]:
    return {
        "id": "dev-1",
        "device_id": "dev-1",
        "name": "Pixel",
        "latitude": lat,
        "longitude": 11.5,
        "accuracy": 10.0,
        "last_seen": last_seen,
    }


def _tracker(row: dict[str, Any], *, last_location: bool = False) -> Any:
    from custom_components.googlefindmy import device_tracker

    cls = (
        device_tracker.GoogleFindMyLastLocationTracker
        if last_location
        else device_tracker.GoogleFindMyDeviceTracker
    )
    entity = cls(
        _CoordinatorStub(row),
        {"id": "dev-1", "name": "Pixel"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    entity.entity_id = "device_tracker.googlefindmy_pixel"
    return entity


def _published(entity: Any) -> dict[str, Any]:
    entity._sync_location_attrs()
    return dict(entity._attr_extra_state_attributes)


@pytest.mark.parametrize("last_location", [False, True])
def test_force_update_is_false(last_location: bool) -> None:
    """Both tracker classes report identical writes instead of changing state."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()), last_location=last_location)
    assert entity.force_update is False


def test_tf02_main_tracker_aging_without_fix_publishes_identical_attributes(
    freezer: FrozenDateTimeFactory,
) -> None:
    """``current`` -> ``aging`` without a new fix leaves the attributes alone."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()))
    first = _published(entity)
    assert first["location_status"] == "current"

    freezer.tick(entity._get_stale_threshold() * 0.75)
    # Control: without holding, the status would now read "aging".
    assert entity._get_location_status() == "aging"
    assert _published(entity) == first, "last_updated would move: attributes changed"


def test_tf03_last_location_tracker_aging_without_fix_publishes_identical_attributes(
    freezer: FrozenDateTimeFactory,
) -> None:
    """The last-location tracker holds ``current`` until a new observation."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()), last_location=True)
    first = _published(entity)

    freezer.tick(entity._get_stale_threshold() * 2)
    assert entity._get_location_status() == "last_known"
    assert _published(entity) == first, "last_updated would move: attributes changed"


def test_tf12_location_age_tick_alone_publishes_identical_attributes(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sixty seconds later only ``location_age`` would change; it is held."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()))
    first = _published(entity)
    assert "location_age" in first

    freezer.tick(60)
    assert entity._get_location_status() == "current"
    assert _published(entity) == first, "last_updated would move: attributes changed"


def test_new_fix_publishes_fresh_age_and_status(freezer: FrozenDateTimeFactory) -> None:
    """A new observation replaces the held values."""

    from homeassistant.util import dt as dt_util

    start = dt_util.utcnow().timestamp()
    entity = _tracker(_row(start))
    _published(entity)
    freezer.tick(entity._get_stale_threshold() * 0.75)
    aged = _published(entity)
    assert aged["location_status"] == "current"

    entity.coordinator.row = _row(dt_util.utcnow().timestamp(), lat=48.2)
    fresh = _published(entity)
    assert fresh["location_status"] == "current"
    assert fresh["location_age"] == 0
    assert fresh != aged


def test_stale_transition_publishes_stale(freezer: FrozenDateTimeFactory) -> None:
    """Going stale withholds the coordinates, so it is a new observation."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()))
    _published(entity)
    freezer.tick(entity._get_stale_threshold() + 60)
    stale = _published(entity)
    assert stale["location_status"] == "stale"
    assert entity._attr_latitude is None


@pytest.mark.parametrize("last_location", [False, True])
def test_tf08_tf11_every_coordinator_tick_writes(
    freezer: FrozenDateTimeFactory, last_location: bool
) -> None:
    """No tick is suppressed: Core must re-evaluate zones and ``last_reported``.

    An unchanged observation still produces one write per tick; with
    ``force_update`` false Core turns it into a report (contract test below)
    and still recomputes the zone of the unchanged position.
    """

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()), last_location=last_location)
    writes: list[dict[str, Any]] = []
    entity.async_write_ha_state = lambda: writes.append(
        dict(entity._attr_extra_state_attributes)
    )

    for _ in range(3):
        entity._handle_coordinator_update()
        freezer.tick(60)

    assert len(writes) == 3
    assert writes[0] == writes[1] == writes[2]


@pytest.mark.asyncio
async def test_contract_identical_write_is_a_report_not_a_change(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Real state machine: the tracker's output plus its ``force_update``.

    Publishing the attributes of an unchanged observation again must not move
    ``last_updated`` (the value ``person`` ranks by); a new fix must.
    """

    from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED
    from homeassistant.core import callback
    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()))
    events: list[str] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event.event_type)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    hass.bus.async_listen(
        EVENT_STATE_REPORTED, _record, event_filter=callback(lambda _data: True)
    )

    def _write() -> None:
        hass.states.async_set(
            entity.entity_id,
            "not_home",
            _published(entity),
            force_update=entity.force_update,
        )

    _write()
    await hass.async_block_till_done()
    first = hass.states.get(entity.entity_id)
    assert first is not None
    first_updated, first_reported = first.last_updated, first.last_reported
    events.clear()

    freezer.tick(entity._get_stale_threshold() * 0.75)
    _write()
    await hass.async_block_till_done()
    ticked = hass.states.get(entity.entity_id)
    assert ticked is not None
    assert events == [EVENT_STATE_REPORTED]
    assert ticked.last_updated == first_updated, "last_updated moved on a tick"
    assert ticked.last_reported > first_reported

    events.clear()
    entity.coordinator.row = _row(dt_util.utcnow().timestamp(), lat=48.2)
    _write()
    await hass.async_block_till_done()
    moved = hass.states.get(entity.entity_id)
    assert moved is not None
    assert events == [EVENT_STATE_CHANGED]
    assert moved.last_updated > first_updated


def test_show_location_age_switched_on_is_held_from_then_on(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching the option on publishes the age once, then holds it."""

    from homeassistant.util import dt as dt_util

    entity = _tracker(_row(dt_util.utcnow().timestamp()))
    entity.coordinator.config_entry.options["show_location_age"] = False
    assert "location_age" not in _published(entity)

    entity.coordinator.config_entry.options["show_location_age"] = True
    switched_on = _published(entity)
    assert "location_age" in switched_on

    freezer.tick(120)
    assert _published(entity) == switched_on

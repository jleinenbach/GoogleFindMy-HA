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
        self.visible = True

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def is_device_visible_in_subentry(self, key: str, device_id: str) -> bool:
        return self.visible

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


# ---------------------------------------------------------------------------
# Codex review of #1321 at ``ceb5e70`` (three threads on the restamp path).
#
# The findings concern writes that bypass the entity's own write path:
# ``hass.states.async_set(..., timestamp=<old fix time>)``. The tests below
# therefore drive ticks through ``_handle_coordinator_update()`` against the
# real state machine. The conftest stubs give the tracker a no-op
# ``async_write_ha_state`` and none of Entity's private surface, so
# ``_wire_entity_write_path`` supplies both: a write that does what Core's
# ``Entity._async_write_ha_state`` does with the timestamp (none, so Core
# stamps "now") and with ``force_update``, plus the TrackerEntity zone logic
# for the state string. Code that writes through ``async_write_ha_state`` is
# measured by the assertions; code that writes around it is measured too,
# because the assertions read the state machine, not the double.
#
# ``freezer`` comes first among the fixtures: ``use_real_homeassistant_modules``
# re-imports Home Assistant, and a ``dt_util`` imported after the freeze
# started keeps reading the real clock while ``time.time()`` and the state
# machine follow the frozen one (measured). Timestamps are compared with an
# absolute tolerance; ``pytest.approx``'s relative default is about half an
# hour at today's epoch values and would hide a restamp to the old fix.
# ---------------------------------------------------------------------------


def _core_state(entity: Any, hass: HomeAssistant) -> tuple[str, list[str] | None]:
    """State string and ``in_zones`` as ``TrackerEntity`` derives them.

    ``zone.async_in_zones`` and the ``in_zones`` attribute are newer than the
    declared Home Assistant floor; 2025.9.1 and 2026.1.3 derive the state from
    ``zone.async_active_zone`` and publish no ``in_zones``. Follow whichever
    API the installed core provides and return ``None`` for ``in_zones`` on
    the older line.
    """

    from homeassistant.components import zone
    from homeassistant.const import STATE_HOME, STATE_NOT_HOME

    lat, lon = entity._attr_latitude, entity._attr_longitude
    if lat is None or lon is None:
        return "unknown", []
    radius = entity._attr_location_accuracy or 0
    in_zones_fn = getattr(zone, "async_in_zones", None)
    in_zones: list[str] | None
    if in_zones_fn is None:
        active = zone.async_active_zone(hass, lat, lon, radius)
        in_zones = None
    else:
        active, in_zones = in_zones_fn(hass, lat, lon, radius)
    if active is None:
        return STATE_NOT_HOME, in_zones
    if active.entity_id == zone.ENTITY_ID_HOME:
        return STATE_HOME, in_zones
    return active.name, in_zones


def _wire_entity_write_path(entity: Any, hass: HomeAssistant) -> None:
    """Give a stub-bound tracker Core's write path on a real state machine."""

    entity.hass = hass
    entity._context = None
    entity._state_info = None

    def _calculate() -> SimpleNamespace:
        state, in_zones = _core_state(entity, hass)
        attributes = dict(entity._attr_extra_state_attributes)
        if in_zones is not None:
            attributes["in_zones"] = in_zones
        return SimpleNamespace(state=state, attributes=attributes)

    def _write() -> None:
        calculated = _calculate()
        hass.states.async_set(
            entity.entity_id,
            calculated.state,
            calculated.attributes,
            # Core's TrackerEntity default for a push entity is ``True``
            # (``not should_poll``); the stub base defines no value.
            force_update=getattr(entity, "force_update", True),
            context=entity._context,
            state_info=entity._state_info,
        )

    entity._async_calculate_state = _calculate
    entity.async_write_ha_state = _write


def _now() -> float:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow().timestamp()


@pytest.mark.parametrize(
    "branch",
    [
        "device_not_visible",
        "no_row",
        "reliable_fix",
        "first_unreliable_fix",
        "unreliable_fix_after_reliable",
    ],
)
@pytest.mark.asyncio
async def test_every_coordinator_update_branch_ends_in_one_plain_write(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    branch: str,
) -> None:
    """Contract of ``custom_components/googlefindmy/AGENTS.md`` (Codex P1, #1321).

    Every branch of ``_handle_coordinator_update`` ends in exactly one
    ``async_write_ha_state`` and never writes ``hass.states`` around it. A
    state of the entity is already in the state machine, so a write path that
    restamps an existing state is reached in every branch.
    """

    entity = _tracker(_row(_now() - 30))
    _wire_entity_write_path(entity, hass)
    hass.states.async_set(entity.entity_id, "not_home", {})
    await hass.async_block_till_done()

    estimated = {**_row(_now() - 30), "accuracy_estimated": True}
    if branch == "device_not_visible":
        entity.coordinator.visible = False
    elif branch == "no_row":
        entity.coordinator.row = None
    elif branch == "first_unreliable_fix":
        entity.coordinator.row = estimated
    elif branch == "unreliable_fix_after_reliable":
        entity._last_good_accuracy_data = _row(_now() - 60)
        entity.coordinator.row = estimated

    writes: list[None] = []
    direct: list[tuple[Any, ...]] = []
    # A pure counter: passing the call on would itself write hass.states and
    # hide a direct write behind it.
    entity.async_write_ha_state = lambda: writes.append(None)

    class _RecordingStates:
        """``hass.states`` as the entity sees it; counts writes around it."""

        def __getattr__(self, name: str) -> Any:
            return getattr(hass.states, name)

        def async_set(self, *args: Any, **kwargs: Any) -> None:
            direct.append(args)
            hass.states.async_set(*args, **kwargs)

    class _HassView:
        """The real ``hass`` with the recording ``states`` in its place."""

        states = _RecordingStates()

        def __getattr__(self, name: str) -> Any:
            return getattr(hass, name)

    entity.hass = _HassView()
    freezer.tick(120)
    entity._handle_coordinator_update()

    assert len(writes) == 1, f"{branch}: {len(writes)} writes"
    assert direct == [], f"{branch}: wrote hass.states directly"


@pytest.mark.asyncio
async def test_last_reported_never_moves_backward(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
) -> None:
    """``last_reported`` is a heartbeat and never jumps back (Codex P2, #1321).

    Ticks go through the entity: an identical tick, a tick in which the age
    of the unchanged fix enters a new minute, and a new fix. After each one
    ``last_reported`` is the time of that tick.
    """

    entity = _tracker(_row(_now() - 30))
    entity.coordinator.config_entry.options["show_location_age"] = True
    _wire_entity_write_path(entity, hass)

    seen: list[float] = []

    async def _tick(seconds: float) -> None:
        freezer.tick(seconds)
        entity._handle_coordinator_update()
        await hass.async_block_till_done()
        state = hass.states.get(entity.entity_id)
        assert state is not None
        now = _now()
        assert state.last_reported.timestamp() == pytest.approx(now, abs=1e-3), (
            "last_reported is not the time of the tick"
        )
        assert state.last_updated <= state.last_reported
        seen.append(state.last_reported.timestamp())

    await _tick(0)
    await _tick(5)  # identical observation
    await _tick(120)  # age of the same fix enters a new minute
    entity.coordinator.row = _row(_now() + 10, lat=48.2)
    await _tick(10)  # new fix

    assert seen == sorted(seen)


@pytest.mark.asyncio
async def test_zone_change_without_new_fix_stamps_last_changed_now(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
) -> None:
    """A new zone of an unchanged fix is a change at tick time (Codex P2, #1321).

    The zone moves away from the unchanged position; the next tick carries
    the same observation and a new age minute. The derived state changes, so
    ``last_changed`` must be the time of that tick, not of the old fix.
    """

    from homeassistant.components import zone

    zone.async_setup_track_zone_entity_ids(hass)
    zone_attrs = {"latitude": 48.1, "longitude": 11.5, "radius": 200.0}
    hass.states.async_set("zone.office", "0", {**zone_attrs, "friendly_name": "Office"})
    await hass.async_block_till_done()

    entity = _tracker(_row(_now() - 30))
    entity.coordinator.config_entry.options["show_location_age"] = True
    _wire_entity_write_path(entity, hass)

    entity._handle_coordinator_update()
    await hass.async_block_till_done()
    before = hass.states.get(entity.entity_id)
    assert before is not None
    assert before.state == "Office"

    hass.states.async_set(
        "zone.office",
        "0",
        {**zone_attrs, "latitude": 48.3, "friendly_name": "Office"},
    )
    freezer.tick(120)
    entity._handle_coordinator_update()
    await hass.async_block_till_done()

    after = hass.states.get(entity.entity_id)
    assert after is not None
    assert after.state == "not_home"
    assert after.last_changed.timestamp() == pytest.approx(_now(), abs=1e-3), (
        "last_changed carries the old fix time instead of the tick time"
    )

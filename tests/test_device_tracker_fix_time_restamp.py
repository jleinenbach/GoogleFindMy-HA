# tests/test_device_tracker_fix_time_restamp.py
"""Coverage for restamping ``last_updated`` to the real GPS fix time.

``_write_state_if_changed()`` makes a three-way decision on every non-no-op
write: (1) fix time strictly newer than the current ``last_updated`` ->
restamp to it via ``_write_at()``. (2) fix not newer but the state string is
unchanged (only ``location_age`` ticked, or a status attribute aged with no
new fix) -> write via ``_write_at()`` but keep the previous timestamp. (3) fix
not newer AND the state string genuinely changed (restart, stale/unknown
transition, recovery from unavailable) -> plain "now"-stamped
``async_write_ha_state()``. This guards against a frequently-polled-but-
unchanged tracker bumping its own ``last_updated`` and outranking a genuinely
fresher one in HA's ``person`` integration (which picks by newest
``last_updated``, not ``last_seen``).

Test doubles: ``tests/conftest.py`` installs lightweight fakes for
``homeassistant.helpers.entity``/``device_tracker`` that production code binds
to at import time and that don't implement the real Entity surface
(``state``, ``_context``, ``_state_info``, a real ``async_write_ha_state()``,
...). Tests below either exercise the pure helpers (``_fix_time_epoch()``,
``_state_signature()``) directly, or supply that missing Entity surface as
plain instance attributes so ``_write_state_if_changed()``/``_write_at()`` can
run end-to-end against a *real* state machine (``use_real_homeassistant_modules``
+ the real ``hass`` fixture) for real timestamps and ``state_changed`` events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from tests.helpers.config_entries_stub import make_config_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _RestampCoordinatorStub:
    """Minimal coordinator serving a single, mutable device row."""

    def __init__(self, hass: Any, row: dict[str, Any] | None) -> None:
        self.hass = hass
        self.config_entry = make_config_entry(
            entry_id="entry-restamp", options={}, runtime_data=None
        )
        self.row: dict[str, Any] | None = row
        self.present = True
        self.visible = True

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def is_device_visible_in_subentry(self, key: str, device_id: str) -> bool:
        return self.visible

    def is_device_present(self, device_id: str) -> bool:
        return self.present

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


def _row(last_seen: float, *, lat: float = 48.1, lon: float = 11.5) -> dict[str, Any]:
    return {
        "id": "dev-1",
        "device_id": "dev-1",
        "name": "Pixel",
        "latitude": lat,
        "longitude": lon,
        "accuracy": 10.0,
        "last_seen": last_seen,
    }


def _make_simple_tracker(row: dict[str, Any] | None) -> Any:
    """Build a tracker with a bare ``SimpleNamespace`` hass (no real HA needed).

    Suitable for the pure helpers (``_fix_time_epoch``, ``_state_signature``,
    ``_sync_location_attrs``) that don't touch Entity/TrackerEntity behaviour.
    """
    from custom_components.googlefindmy import device_tracker

    coordinator = _RestampCoordinatorStub(SimpleNamespace(), row)
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "dev-1", "name": "Pixel"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    entity.entity_id = "device_tracker.googlefindmy_pixel"
    return entity


# ---------------------------------------------------------------------------
# Pure-helper coverage: no real Home Assistant object graph required.
# ---------------------------------------------------------------------------


def test_future_last_seen_is_clamped_to_now() -> None:
    """A future/corrupt last_seen must never push the fix epoch into the future.

    Without the clamp, a clock-skewed ``last_seen`` would make the tracker
    artificially "freshest" forever -- the exact hijack this feature prevents.
    """
    future_fix = time.time() + 10_000
    entity = _make_simple_tracker(_row(future_fix))

    before = time.time()
    fix_epoch = entity._fix_time_epoch()
    after = time.time()

    assert fix_epoch is not None
    assert before <= fix_epoch <= after


def test_fix_time_epoch_passes_through_a_sane_last_seen() -> None:
    """A normal past last_seen is returned unmodified (no spurious clamping)."""
    fix_time = time.time() - 120
    entity = _make_simple_tracker(_row(fix_time))
    assert entity._fix_time_epoch() == pytest.approx(fix_time)


def test_coarse_fix_change_is_not_swallowed_by_signature() -> None:
    """A coarse-fix-only change (same reliable fix) still changes the signature.

    ``_state_signature()`` is derived from the published extra attributes, so
    a new/aged-out coarse fix is never invisible to the no-op guard.
    """
    fix_time = time.time() - 60
    entity = _make_simple_tracker(_row(fix_time))
    entity.coordinator.get_fresh_coarse_fix = lambda device_id: None  # type: ignore[attr-defined]
    entity._sync_location_attrs()
    baseline_signature = entity._state_signature()

    entity.coordinator.get_fresh_coarse_fix = lambda device_id: {  # type: ignore[attr-defined]
        "latitude": 48.0,
        "longitude": 11.0,
        "accuracy": 500.0,
        "last_seen": time.time() - 5,
    }
    entity._sync_location_attrs()
    assert entity._attr_extra_state_attributes.get("coarse_latitude") == 48.0
    changed_signature = entity._state_signature()

    assert changed_signature != baseline_signature


def test_location_age_stays_excluded_from_signature() -> None:
    """``location_age`` must stay excluded: it ticks every minute on its own."""
    fix_time = time.time() - 3600
    entity = _make_simple_tracker(_row(fix_time))
    entity._sync_location_attrs()
    assert "location_age" in entity._attr_extra_state_attributes

    sig_1 = entity._state_signature()
    # Bump only the published location_age; signature must be unaffected.
    entity._attr_extra_state_attributes = dict(entity._attr_extra_state_attributes)
    entity._attr_extra_state_attributes["location_age"] += 60
    sig_2 = entity._state_signature()
    assert sig_1 == sig_2


# ---------------------------------------------------------------------------
# End-to-end write coverage against a real Home Assistant state machine.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Activate real Home Assistant modules from the central conftest fixture."""


def _wire_missing_entity_surface(entity: Any, *, state: str = "not_home") -> None:
    """Supply the Entity-base surface this repo's fast test stubs don't provide.

    The module is bound at import time to this repo's lightweight
    ``Entity``/``TrackerEntity`` fakes, which implement none of ``state``,
    ``capability_attributes``, ``_context``, ``_state_info``,
    ``_async_calculate_state``, etc. Plain instance attributes mirroring what
    real Entity would compute are enough to drive ``_write_state_if_changed()``
    end-to-end against a *real* state machine (see
    ``test_registry_override_reaches_the_written_state`` for registry-override
    coverage against a real Entity subclass).
    """
    entity.state = state
    entity.capability_attributes = None
    entity.state_attributes = {}
    entity.extra_state_attributes = entity._attr_extra_state_attributes
    entity.unit_of_measurement = None
    entity.assumed_state = False
    entity.attribution = entity._attr_attribution
    entity.device_class = None
    entity.entity_picture = None
    entity.icon = None
    entity.supported_features = None
    entity._context = None
    entity._state_info = None
    entity._friendly_name_internal = lambda: entity._device.get("name")

    def _calculate_state() -> SimpleNamespace:
        """Stand-in for real Entity._async_calculate_state().

        Assembles state + extra attributes + attribution/friendly_name from
        the plain instance attributes above -- close enough for these
        write-decision tests, which don't depend on the exact attribute set.
        """
        attrs: dict[str, Any] = {}
        if entity.state_attributes:
            attrs.update(entity.state_attributes)
        if entity.extra_state_attributes:
            attrs.update(entity.extra_state_attributes)
        if entity.attribution is not None:
            attrs["attribution"] = entity.attribution
        name = entity._friendly_name_internal()
        if name is not None:
            attrs["friendly_name"] = name
        return SimpleNamespace(state=entity.state, attributes=attrs)

    entity._async_calculate_state = _calculate_state


def _make_tracker(hass: HomeAssistant, row: dict[str, Any] | None) -> Any:
    from custom_components.googlefindmy import device_tracker

    coordinator = _RestampCoordinatorStub(hass, row)
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "dev-1", "name": "Pixel"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = hass
    entity.entity_id = "device_tracker.googlefindmy_pixel"
    # async_write_ha_state() is a no-op on the CoordinatorEntity test fake;
    # replace it with a real "now"-timestamped write so the plain-write path
    # is observable against the real state machine.
    def _plain_write() -> None:
        calculated = entity._async_calculate_state()
        hass.states.async_set(entity.entity_id, calculated.state, calculated.attributes)

    entity.async_write_ha_state = MagicMock(side_effect=_plain_write)  # type: ignore[method-assign]
    return entity


def _update(entity: Any, *, state: str = "not_home") -> None:
    """Simulate one coordinator tick against the real write-decision logic."""
    entity._sync_location_attrs()
    _wire_missing_entity_surface(entity, state=state)
    entity._write_state_if_changed()


def test_force_update_is_never_passed() -> None:
    """Grep-style guard: this write path must never pass force_update=True.

    force_update=True would reset last_changed even when the state string is
    unchanged, corrupting `for:` trigger timing.
    """
    import inspect

    from custom_components.googlefindmy import device_tracker

    source = inspect.getsource(device_tracker.GoogleFindMyDeviceTracker._write_at)
    assert "force_update=True" not in source
    assert "force_update=False" in source


@pytest.mark.asyncio
async def test_noop_update_does_not_restamp_or_double_write(
    hass: HomeAssistant,
) -> None:
    """An unchanged fix is a pure no-op: no write at all, real or stubbed."""
    fix_time = time.time() - 120
    entity = _make_tracker(hass, _row(fix_time))

    # Prime the entity with its first write (unavoidably a plain write: no
    # previous state exists yet for this entity_id).
    _update(entity)
    await hass.async_block_till_done()
    entity.async_write_ha_state.reset_mock()

    events: list[Any] = []
    hass.bus.async_listen("state_changed", events.append)

    # Re-run with the identical row (same signature): must be a pure no-op --
    # no new state_changed event, and the plain write path is not used either.
    _update(entity)
    await hass.async_block_till_done()
    assert len(events) == 0
    assert entity.async_write_ha_state.call_count == 0

    # And again, to prove it stays suppressed, not just once.
    _update(entity)
    await hass.async_block_till_done()
    assert len(events) == 0
    assert entity.async_write_ha_state.call_count == 0


@pytest.mark.asyncio
async def test_genuine_new_fix_restamps_to_fix_time(hass: HomeAssistant) -> None:
    """A fix strictly newer than the current last_updated restamps to it.

    The state STRING is unchanged ("not_home" both times), only the
    attributes differ, so ``last_changed`` must stay put while
    ``last_updated`` advances to the fix time.
    """
    older_fix = time.time() - 300
    entity = _make_tracker(hass, _row(older_fix))
    _update(entity)
    entity.async_write_ha_state.reset_mock()

    first_state = hass.states.get(entity.entity_id)
    assert first_state is not None
    original_last_changed = first_state.last_changed
    first_last_updated = first_state.last_updated.timestamp()

    events: list[Any] = []
    hass.bus.async_listen("state_changed", events.append)

    # The first write was necessarily a plain, "now"-stamped write (no
    # previous state existed yet), so a genuinely newer fix must itself be
    # close to "now" -- a brief real sleep guarantees the next fix time is
    # strictly newer than that first last_updated.
    time.sleep(0.05)
    newer_fix = time.time()
    assert newer_fix > first_last_updated
    entity.coordinator.row = _row(newer_fix, lat=48.2, lon=11.6)
    _update(entity)
    await hass.async_block_till_done()

    # Exactly one state_changed event for this entity_id for the new fix.
    own_events = [e for e in events if e.data.get("entity_id") == entity.entity_id]
    assert len(own_events) == 1
    # The plain write path must not also have fired for this update.
    assert entity.async_write_ha_state.call_count == 0

    state = hass.states.get(entity.entity_id)
    assert state is not None
    # last_updated reflects the fix time (the whole point of this feature).
    assert abs(state.last_updated.timestamp() - newer_fix) < 1.0
    # last_changed must NOT be reset to the fix time: the state string did
    # not change, only the attributes did.
    assert state.last_changed == original_last_changed


@pytest.mark.asyncio
async def test_state_string_change_does_move_last_changed(
    hass: HomeAssistant,
) -> None:
    """A genuine state-string change (not_home -> home) still moves last_changed.

    force_update=False does not mean last_changed can never move on a
    restamped write -- ``hass.states.async_set_internal()`` still updates it
    whenever the state string itself actually changes, exactly like a normal
    (non-restamped) write would.
    """
    fix_time = time.time() - 120
    entity = _make_tracker(hass, _row(fix_time))
    _update(entity, state="not_home")
    first_state = hass.states.get(entity.entity_id)
    assert first_state is not None
    first_last_updated = first_state.last_updated.timestamp()

    # The first write was a plain, "now"-stamped write; a brief real sleep
    # guarantees the next fix time is strictly newer than that.
    time.sleep(0.05)
    newer_fix = time.time()
    assert newer_fix > first_last_updated
    entity.coordinator.row = _row(newer_fix, lat=48.3, lon=11.7)
    _update(entity, state="home")
    await hass.async_block_till_done()

    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state == "home"
    assert abs(state.last_changed.timestamp() - newer_fix) < 1.0
    assert abs(state.last_updated.timestamp() - newer_fix) < 1.0


@pytest.mark.asyncio
async def test_first_write_after_restart_is_plain(hass: HomeAssistant) -> None:
    """The very first write for an entity_id (no previous state) is a plain write.

    Right after a restart/reload the state machine has no entry for this
    entity_id yet -- that absence is the signal, instead of any private
    platform-state introspection.
    """
    fix_time = time.time() - 600
    entity = _make_tracker(hass, _row(fix_time))

    assert hass.states.get(entity.entity_id) is None

    before = time.time()
    _update(entity)
    after = time.time()

    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert before - 1.0 <= state.last_updated.timestamp() <= after + 1.0
    # The plain write path (not a direct states.async_set() restamp) produced
    # this row.
    assert entity.async_write_ha_state.call_count == 1


@pytest.mark.asyncio
async def test_recovery_from_unavailable_with_old_fix_is_plain_write_and_orders_after(
    hass: HomeAssistant,
) -> None:
    """Recovering from "unavailable" with the SAME old fix is a plain "now" write.

    No special-casing of recovery: the old fix is never newer than the
    "unavailable" row's last_updated, so it naturally falls through to a plain
    write, keeping recorder history in chronological order (no backdating).
    """
    fix_time = time.time() - 600
    entity = _make_tracker(hass, _row(fix_time))

    _update(entity, state="not_home")

    entity.coordinator.present = False
    before_gap = time.time()
    _update(entity, state="unavailable")
    unavailable_state = hass.states.get(entity.entity_id)
    assert unavailable_state is not None
    unavailable_last_updated = unavailable_state.last_updated.timestamp()
    assert unavailable_last_updated >= before_gap

    entity.coordinator.present = True
    _update(entity, state="not_home")

    state = hass.states.get(entity.entity_id)
    assert state is not None
    # Chronological order preserved: the recovery write is not earlier than
    # the unavailable marker.
    assert state.last_updated.timestamp() >= unavailable_last_updated


@pytest.mark.asyncio
async def test_stale_transition_is_plain_now_write(hass: HomeAssistant) -> None:
    """A stale-triggered transition to "unknown" stamps "now", not the old fix time.

    No new fix arrives, but lowering the stale threshold simulates enough
    wall-clock time passing to trip staleness on the same fix, so the fix
    epoch is never newer than last_updated and the write falls through to a
    plain "now"-stamped write.
    """
    fix_time = time.time() - 60
    entity = _make_tracker(hass, _row(fix_time))

    _update(entity, state="home")

    first_state = hass.states.get(entity.entity_id)
    assert first_state is not None
    assert first_state.attributes.get("latitude") is not None

    entity._get_stale_threshold = lambda: 1  # type: ignore[method-assign]

    before = time.time()
    _update(entity, state="unknown")
    after = time.time()

    second_state = hass.states.get(entity.entity_id)
    assert second_state is not None
    assert second_state.state == "unknown"
    assert second_state.attributes.get("latitude") is None

    last_updated = second_state.last_updated.timestamp()
    last_changed = second_state.last_changed.timestamp()
    assert before - 1.0 <= last_updated <= after + 1.0
    assert before - 1.0 <= last_changed <= after + 1.0
    assert last_updated - fix_time > 30


@pytest.mark.asyncio
async def test_aging_transition_with_same_state_keeps_previous_timestamp(
    hass: HomeAssistant,
) -> None:
    """A fix aging from "current" to "aging" status keeps the old timestamp.

    No new fix arrives, but lowering the stale threshold flips
    ``location_status`` from "current" to "aging", which changes
    ``_state_signature()`` -- yet the published state STRING ("home") stays
    the same, so this must NOT get a plain "now" write (that would let a
    merely-aging tracker outrank a genuinely fresher one). It keeps the
    previous ``last_updated``, same as a pure ``location_age`` tick.
    """
    fix_time = time.time() - 60
    entity = _make_tracker(hass, _row(fix_time))

    # Large threshold: age (60s) is well under it, so the first write
    # publishes location_status == "current".
    entity._get_stale_threshold = lambda: 1000  # type: ignore[method-assign]
    _update(entity, state="home")

    first_state = hass.states.get(entity.entity_id)
    assert first_state is not None
    assert first_state.state == "home"
    assert first_state.attributes.get("location_status") == "current"
    assert first_state.attributes.get("latitude") is not None
    first_last_updated = first_state.last_updated

    # Lower the threshold so the SAME age (60s, no new fix) now falls between
    # threshold/2 (50) and threshold (100): "aging", not "stale". The state
    # string stays "home" -- real coordinates are still shown.
    entity._get_stale_threshold = lambda: 100  # type: ignore[method-assign]

    _update(entity, state="home")

    second_state = hass.states.get(entity.entity_id)
    assert second_state is not None
    assert second_state.state == "home"
    assert second_state.attributes.get("location_status") == "aging"
    assert second_state.attributes.get("latitude") is not None

    # The whole point: last_updated must NOT jump to "now" just because the
    # tracker aged -- it must keep the fix-derived timestamp from the first
    # write, same as a pure location_age tick.
    assert second_state.last_updated == first_last_updated


@pytest.mark.asyncio
async def test_registry_override_reaches_the_written_state(
    hass: HomeAssistant,
) -> None:
    """Entity-registry name/icon overrides must survive a restamped write.

    ``_write_at()`` gets state/attributes from ``_async_calculate_state()``
    rather than reconstructing them, because manual reconstruction would skip
    entity-registry overrides and cause a visible name/icon flicker between
    restamped and plain writes. Proven in two parts, since this repo's fast
    Entity stubs can't exercise registry resolution:

    1. Against a *real* Entity subclass with a registry entry, real
       ``_async_calculate_state()`` returns the overridden name/icon.
    2. ``_write_at()`` writes whatever it gets back verbatim -- checked here
       by wiring the tracker's ``_async_calculate_state`` to (1)'s result.
    """
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity import Entity

    class _ProbeEntity(Entity):
        """Minimal real Entity whose own name/icon differ from the override."""

        @property
        def name(self) -> str:
            return "Entity Default Name"

        @property
        def icon(self) -> str:
            return "mdi:entity-default"

        @property
        def available(self) -> bool:
            return True

    registry = er.async_get(hass)
    entry = registry.async_get_or_create("device_tracker", "googlefindmy", "probe-1")
    entry = registry.async_update_entity(
        entry.entity_id, name="User Renamed Tracker", icon="mdi:user-custom-icon"
    )

    probe = _ProbeEntity()
    probe.hass = hass
    probe.entity_id = entry.entity_id
    probe.registry_entry = entry

    calculated = probe._async_calculate_state()
    assert calculated.attributes["friendly_name"] == "User Renamed Tracker"
    assert calculated.attributes["icon"] == "mdi:user-custom-icon"
    # Sanity: the override really differs from the entity's own properties,
    # otherwise this test could pass for the wrong reason.
    assert probe.name != calculated.attributes["friendly_name"]
    assert probe.icon != calculated.attributes["icon"]

    # Part 2: our tracker's restamp path must carry (1)'s result through
    # verbatim.
    fix_time = time.time() - 45
    entity = _make_tracker(hass, _row(fix_time))
    entity._sync_location_attrs()
    _wire_missing_entity_surface(entity, state="not_home")
    entity._async_calculate_state = lambda: calculated  # type: ignore[method-assign]

    assert entity._write_at(fix_time) is True

    written = hass.states.get(entity.entity_id)
    assert written is not None
    assert written.attributes["friendly_name"] == "User Renamed Tracker"
    assert written.attributes["icon"] == "mdi:user-custom-icon"


@pytest.mark.asyncio
async def test_calculate_state_failure_falls_back_to_plain_write(
    hass: HomeAssistant,
) -> None:
    """If _async_calculate_state() is missing/raises, the write falls back cleanly.

    A future core release could change or remove the private
    ``_async_calculate_state()`` surface this path depends on; ``_write_at()``
    must report failure (``False``) rather than raise, so the caller falls
    back to a plain write.
    """
    fix_time = time.time() - 45
    entity = _make_tracker(hass, _row(fix_time))
    entity._sync_location_attrs()
    _wire_missing_entity_surface(entity, state="not_home")

    def _broken_calculate_state() -> Any:
        raise AttributeError("_async_calculate_state unavailable")

    entity._async_calculate_state = _broken_calculate_state  # type: ignore[method-assign]

    assert entity._write_at(fix_time) is False

    # Also exercise the caller end-to-end: a bare counting mock (not the
    # calculate-state-dependent stand-in) since a broken calculator would
    # make any real write fail too -- this only checks the fallback fires.
    entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    entity._write_state_if_changed()
    assert entity.async_write_ha_state.call_count == 1


@pytest.mark.asyncio
async def test_customize_override_survives_second_restamp_write(
    hass: HomeAssistant,
) -> None:
    """A customize.yaml-style override must survive every restamp-path write.

    ``_write_at()`` replicates core's ``DATA_CUSTOMIZE`` overlay; without that
    a user's override would be silently stripped on a restamped write. Drives
    TWO restamp-path writes and checks the override survives both.
    """
    from homeassistant.core_config import DATA_CUSTOMIZE

    fix_time = time.time() - 120
    entity = _make_tracker(hass, _row(fix_time))

    # Mirror core's configuration.yaml customize block: entity_id -> overrides.
    hass.data[DATA_CUSTOMIZE] = {
        entity.entity_id: {"icon": "mdi:customize-override"}
    }

    # Prime the entity (first write is necessarily plain: no previous state).
    _update(entity)
    first_written = hass.states.get(entity.entity_id)
    assert first_written is not None
    first_last_updated = first_written.last_updated.timestamp()

    # A brief real sleep guarantees the next fix time is strictly newer than
    # the first (plain, "now"-stamped) write, so this update takes the
    # restamp path.
    time.sleep(0.05)
    newer_fix = time.time()
    assert newer_fix > first_last_updated
    entity.coordinator.row = _row(newer_fix, lat=48.4, lon=11.8)
    _update(entity)

    second_written = hass.states.get(entity.entity_id)
    assert second_written is not None
    assert second_written.attributes.get("icon") == "mdi:customize-override"
    assert abs(second_written.last_updated.timestamp() - newer_fix) < 1.0

    # And a SECOND restamp-path write, not just the first.
    time.sleep(0.05)
    newest_fix = time.time()
    entity.coordinator.row = _row(newest_fix, lat=48.5, lon=11.9)
    _update(entity)
    third_written = hass.states.get(entity.entity_id)
    assert third_written is not None
    assert third_written.attributes.get("icon") == "mdi:customize-override"


@pytest.mark.asyncio
async def test_location_age_ticks_without_moving_last_updated(
    hass: HomeAssistant,
) -> None:
    """``show_location_age`` users still see a ticking value without
    ``last_updated`` moving, since no new fix arrived.

    ``location_age`` is excluded from ``_state_signature()`` so a stationary
    device doesn't bump ``last_updated`` every poll; ``_write_state_if_changed()``
    tracks it separately so a write still happens, keeping the old timestamp.
    """
    fix_time = time.time() - 600
    entity = _make_tracker(hass, _row(fix_time))
    entity.coordinator.config_entry.options = {"show_location_age": True}

    _update(entity)
    first = hass.states.get(entity.entity_id)
    assert first is not None
    first_age = first.attributes.get("location_age")
    assert first_age is not None
    first_last_updated = first.last_updated

    # Simulate pure time passing: same row (no new fix), but location_age
    # (derived from wall-clock time) has ticked up past the next 60s bucket.
    entity._get_location_age = lambda row=None: 660.0  # type: ignore[method-assign]
    _update(entity)

    second = hass.states.get(entity.entity_id)
    assert second is not None
    second_age = second.attributes.get("location_age")
    assert second_age is not None
    assert second_age != first_age
    # The whole point: last_updated must NOT move, since no new fix arrived --
    # only the ticking location_age attribute did.
    assert second.last_updated == first_last_updated

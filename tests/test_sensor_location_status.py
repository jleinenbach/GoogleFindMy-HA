# tests/test_sensor_location_status.py
"""Behavior tests for ``GoogleFindMyLocationStatusSensor``.

The sensor takes over the live age class that the device_tracker
``location_status`` attribute no longer updates between observations. Both read
``classify_location_status`` (one definition) on the coordinator display row,
so the tests pin three things:

- the class boundaries at ``T/2`` and ``T`` (both exclusive) of the configured
  stale threshold, on the shared function and through the sensor;
- the entity contract (enum with three options, ``None`` for an unknown age,
  enabled by default, no entity category, one more entity per device, rebuilt
  by the recovery);
- that sensor and tracker publish the same class for the same coordinator data
  (``test_sensor_same_status_as_tracker``), with both entities set up through
  their platform and the coordinator's own display-row and last-good code.

Time is driven by ``freezer``, listed first so the clock is frozen before any
other fixture imports a time source. Coroutines are awaited directly in
pytest-asyncio's managed loop (``tests/AGENTS.md`` "Async tests").
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_STALE_THRESHOLD,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import (
    LOCATION_STATUS_OPTIONS,
    classify_location_status,
)
from tests.helpers.config_entries_stub import make_config_entry

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory

# Stale threshold used throughout; boundaries sit at 300 and 600 seconds.
_T = 600


def _row(last_seen: float | None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "dev-1",
        "device_id": "dev-1",
        "name": "Pixel",
        "latitude": 52.52,
        "longitude": 13.405,
        "accuracy": 12.0,
        "last_seen": last_seen,
    }
    row.update(extra)
    return row


def _coordinator_class() -> type[Any]:
    """Return a coordinator that inherits every location read path.

    Only the snapshot, visibility and listener plumbing are stubbed; the
    current-row, display-row and last-good code is the production coordinator
    code that the tracker and the sensor read in the integration.
    """

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )

    class _Coordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self, devices: Iterable[dict[str, Any]], threshold: int) -> None:
            self._snapshot = list(devices)
            self._listeners: list[Callable[[], None]] = []
            self.hass = SimpleNamespace()
            self.config_entry = make_config_entry(
                entry_id="entry-status", options={OPT_STALE_THRESHOLD: threshold}
            )
            self.stats: dict[str, int] = {}
            self._device_names: dict[str, str] = {}
            self._device_location_data: dict[str, Any] = {}
            self._device_last_good_location: dict[str, Any] = {}
            self._device_caps: dict[str, Any] = {}
            self._present_last_seen: dict[str, float] = {}
            self.visible = True

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            assert key is not None
            return f"{key}-identifier"

        def get_subentry_metadata(
            self, *, key: str | None = None, feature: str | None = None
        ) -> Any:
            if key is not None:
                resolved = key
            elif feature == "binary_sensor":
                resolved = SERVICE_SUBENTRY_KEY
            else:
                resolved = TRACKER_SUBENTRY_KEY
            return SimpleNamespace(key=resolved)

        def get_subentry_snapshot(
            self, key: str | None = None, *, feature: str | None = None
        ) -> list[dict[str, Any]]:
            return list(self._snapshot)

        def is_device_visible_in_subentry(
            self, subentry_key: str, device_id: str
        ) -> bool:
            return self.visible

        def ingest(self, device_id: str, row: dict[str, Any]) -> None:
            """Commit ``row`` the way ``update_device_cache`` ends: slot + last-good."""
            self._device_location_data[device_id] = dict(row)
            self._record_last_good_location(device_id, dict(row))

    return _Coordinator


def _entry(runtime_data: Any, *, entry_id: str = "entry-status") -> Any:
    """Platform config entry; the setup registers unload callbacks on it."""

    return make_config_entry(
        entry_id=entry_id,
        runtime_data=runtime_data,
        async_on_unload=lambda _callback: None,
    )


async def _setup_sensors(
    devices: list[dict[str, Any]], *, threshold: int = _T
) -> tuple[Any, list[Any]]:
    """Run ``sensor.async_setup_entry`` and return coordinator and entities."""

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator = _coordinator_class()(devices, threshold)
    added: list[Any] = []

    def _capture(entities: Iterable[Any], update_before_add: bool = False) -> None:
        for entity in entities:
            # HA assigns the entity_id when it adds the entity.
            entity.entity_id = f"sensor.{entity.unique_id}"
            added.append(entity)

    await sensor.async_setup_entry(SimpleNamespace(), _entry(coordinator), _capture)
    return coordinator, added


def _status_sensors(entities: list[Any]) -> list[Any]:
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    return [
        e for e in entities if isinstance(e, sensor.GoogleFindMyLocationStatusSensor)
    ]


# ---------------------------------------------------------------------------
# Shared classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (None, "unknown"),
        (0.0, "current"),
        (_T / 2, "current"),
        (_T / 2 + 1, "aging"),
        (float(_T), "aging"),
        (_T + 1.0, "stale"),
    ],
)
def test_classify_location_status_boundaries(age: float | None, expected: str) -> None:
    """Both bounds are exclusive: ``T/2`` is still current, ``T`` still aging."""

    assert classify_location_status(age, _T) == expected


def test_options_are_the_three_known_classes_in_age_order() -> None:
    """``unknown`` is not an option; it is published as ``None``."""

    assert LOCATION_STATUS_OPTIONS == ("current", "aging", "stale")


# ---------------------------------------------------------------------------
# Sensor through the platform setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (_T / 2, "current"),
        (_T / 2 + 1, "aging"),
        (float(_T), "aging"),
        (_T + 1.0, "stale"),
    ],
)
async def test_sensor_classifies_against_the_configured_threshold(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    age: float,
    expected: str,
) -> None:
    """The sensor reads the threshold from the options, not the default."""

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    coordinator.ingest("dev-1", _row(time.time() - age))
    (entity,) = _status_sensors(entities)
    assert entity.native_value == expected


@pytest.mark.asyncio
async def test_sensor_ages_without_a_new_fix(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Same row, time passes: ``current`` -> ``aging`` -> ``stale``."""

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    coordinator.ingest("dev-1", _row(time.time()))
    (entity,) = _status_sensors(entities)

    seen = [entity.native_value]
    freezer.tick(_T * 0.75)
    seen.append(entity.native_value)
    freezer.tick(_T * 0.5)
    seen.append(entity.native_value)
    assert seen == ["current", "aging", "stale"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, _row(None), _row("not-a-timestamp")],
    ids=["no_row", "no_last_seen", "unparsable_last_seen"],
)
async def test_sensor_unknown_age_is_none(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    row: dict[str, Any] | None,
) -> None:
    """No usable age -> ``None`` (HA state ``unknown``), never a string."""

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    if row is not None:
        coordinator.ingest("dev-1", row)
    (entity,) = _status_sensors(entities)
    assert entity.native_value is None


@pytest.mark.asyncio
async def test_sensor_entity_contract(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Enum with three options, enabled by default, visible, translated."""

    del deterministic_config_subentry_id
    from homeassistant.components.sensor import SensorDeviceClass

    _, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    (entity,) = _status_sensors(entities)
    description = entity.entity_description

    assert description.device_class == SensorDeviceClass.ENUM
    assert list(description.options) == ["current", "aging", "stale"]
    assert description.translation_key == "location_status"
    assert entity._attr_has_entity_name is True
    assert entity._attr_entity_registry_enabled_default is True
    assert getattr(entity, "_attr_entity_category", None) is None
    assert entity.unique_id == (
        f"{DOMAIN}_entry-status_{TRACKER_SUBENTRY_KEY}-identifier_dev-1_location_status"
    )


@pytest.mark.asyncio
async def test_setup_adds_exactly_one_status_sensor_per_device(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Per device: LastSeen, Plus Code and now one location status sensor."""

    del deterministic_config_subentry_id
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    per_device = (
        sensor.GoogleFindMyLastSeenSensor,
        sensor.GoogleFindMyPlusCodeSensor,
        sensor.GoogleFindMyLocationStatusSensor,
    )
    _, entities = await _setup_sensors(
        [{"id": "dev-1", "name": "Pixel"}, {"id": "dev-2", "name": "Tag"}]
    )
    device_entities = [e for e in entities if isinstance(e, per_device)]
    status = _status_sensors(entities)

    assert len(device_entities) == 6
    assert len(status) == 2
    assert {e.unique_id.rsplit("_", 3)[-3] for e in status} == {"dev-1", "dev-2"}


@pytest.mark.asyncio
async def test_sensor_follows_device_presence(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Unavailable and unknown while the device is hidden in the subentry."""

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    coordinator.ingest("dev-1", _row(time.time()))
    (entity,) = _status_sensors(entities)
    entity.coordinator.last_update_success = True
    assert entity.available is True

    assert entity.native_value == "current"

    coordinator.visible = False
    writes: list[None] = []
    entity.async_write_ha_state = lambda: writes.append(None)
    entity._handle_coordinator_update()
    assert entity.available is False
    assert entity.native_value is None
    assert writes == [None]


@pytest.mark.asyncio
async def test_sensor_without_device_id_is_unknown_and_unavailable(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A device row without ``id`` never reads a location row."""

    del deterministic_config_subentry_id
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator = _coordinator_class()([{"id": "dev-1", "name": "Pixel"}], _T)
    coordinator.last_update_success = True
    entity = sensor.GoogleFindMyLocationStatusSensor(
        coordinator,
        {"name": "Nameless"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=f"{TRACKER_SUBENTRY_KEY}-identifier",
    )
    assert entity.native_value is None
    assert entity.available is False
    assert entity.unique_id.endswith("_unknown_location_status")


@pytest.mark.asyncio
async def test_coordinator_update_writes_once_and_syncs_the_label(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Every tick writes once and forwards the device label to the registry sync.

    Whether the label itself changes is the shared helper's job
    (``refresh_device_label_from_coordinator``); this pins that the sensor
    calls the registry sync with the current label, as its neighbours do.
    """

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    coordinator.ingest("dev-1", _row(time.time()))
    (entity,) = _status_sensors(entities)
    writes: list[None] = []
    renames: list[str] = []
    entity.async_write_ha_state = lambda: writes.append(None)
    entity.maybe_update_device_registry_name = renames.append

    entity._handle_coordinator_update()

    assert writes == [None]
    assert renames == ["Pixel"]


@pytest.mark.asyncio
async def test_device_info_comes_from_the_shared_helper(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The sensor joins the device of the tracker and the other sensors."""

    del deterministic_config_subentry_id
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    _, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    (status,) = _status_sensors(entities)
    (plus_code,) = [
        e for e in entities if isinstance(e, sensor.GoogleFindMyPlusCodeSensor)
    ]
    # The conftest DeviceInfo stub has no __eq__; compare the content.
    assert vars(status.device_info) == vars(plus_code.device_info)


@pytest.mark.asyncio
async def test_sensor_unavailable_while_the_coordinator_is(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A failed coordinator update makes the sensor unavailable first."""

    del deterministic_config_subentry_id
    coordinator, entities = await _setup_sensors([{"id": "dev-1", "name": "Pixel"}])
    coordinator.ingest("dev-1", _row(time.time()))
    (entity,) = _status_sensors(entities)
    coordinator.last_update_success = True
    assert entity.available is True

    coordinator.last_update_success = False
    assert entity.available is False


@pytest.mark.asyncio
async def test_tick_without_a_device_name_still_writes(
    freezer: FrozenDateTimeFactory,
) -> None:
    """No label to forward: no registry sync, but the state is still written."""

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator = _coordinator_class()([{"id": "dev-1"}], _T)
    coordinator.ingest("dev-1", _row(time.time()))
    entity = sensor.GoogleFindMyLocationStatusSensor(
        coordinator,
        {"id": "dev-1"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=f"{TRACKER_SUBENTRY_KEY}-identifier",
    )
    entity.entity_id = f"sensor.{entity.unique_id}"
    writes: list[None] = []
    renames: list[str] = []
    entity.async_write_ha_state = lambda: writes.append(None)
    entity.maybe_update_device_registry_name = renames.append

    entity._handle_coordinator_update()

    assert writes == [None]
    assert renames == []
    assert entity.native_value == "current"


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lost", "expected_types"),
    [
        ("location_status", ["GoogleFindMyLocationStatusSensor"]),
        ("last_seen", ["GoogleFindMyLastSeenSensor"]),
        ("coarse_fix", ["GoogleFindMyCoarseFixSensor"]),
    ],
)
async def test_recovery_rebuilds_exactly_the_lost_sensor(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Any,
    lost: str,
    expected_types: list[str],
) -> None:
    """Remove one registry entry, run the recovery, find exactly that entity.

    The registry holds every expected tracker sensor except ``lost``; the
    recovery manager compares it with the expected unique IDs and adds what is
    missing through the sensor platform's factory. Losing the status sensor
    rebuilds it; losing LastSeen rebuilds LastSeen and not the status sensor.
    """

    import asyncio

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant

    import custom_components.googlefindmy as integration
    from custom_components.googlefindmy import EntityRecoveryManager, sensor

    loop = asyncio.get_running_loop()
    hass = HomeAssistant()
    hass.loop = loop
    hass.data = {"core.uuid": "test-instance"}
    hass.bus = SimpleNamespace(
        async_listen=lambda *_args, **_kwargs: lambda: None,
        async_listen_once=lambda *_args, **_kwargs: lambda: None,
    )
    hass.async_create_task = lambda coro, *, name=None: loop.create_task(
        coro, name=name
    )
    hass.async_run_hass_job = lambda job, *args: getattr(
        job, "target", lambda *_: None
    )(*args)
    hass.verify_event_loop_thread = lambda *_args, **_kwargs: None

    entry = _entry(None, entry_id="entry-recovery")
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}
    coordinator = stub_coordinator_factory(data=[{"id": "dev-1", "name": "Pixel"}])(
        hass, cache=SimpleNamespace(entry_id=entry.entry_id)
    )
    coordinator.config_entry = entry
    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=manager
    )

    added: list[Any] = []
    pending: list[asyncio.Task[Any]] = []

    def _add(entities: Iterable[Any], **_kwargs: Any) -> None:
        for entity in entities:
            entity.hass = hass
            entity.entity_id = f"sensor.{entity.unique_id}"
            added.append(entity)
            pending.append(loop.create_task(entity.async_added_to_hass()))

    await sensor.async_setup_entry(hass, entry, _add)
    await asyncio.gather(*pending)

    registration = manager._platforms.get(str(Platform.SENSOR))
    assert registration is not None
    prefix = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_dev-1_"
    expected = registration.expected_unique_ids()
    assert f"{prefix}location_status" in expected

    registry_entries = [
        SimpleNamespace(domain="sensor", platform=DOMAIN, unique_id=uid)
        for uid in sorted(expected)
        if uid != f"{prefix}{lost}"
    ]
    monkeypatch.setattr(
        integration,
        "_iter_config_entry_entities",
        lambda _registry, _entry_id: registry_entries,
    )
    added.clear()
    pending.clear()

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    assert [type(e).__name__ for e in added] == expected_types
    assert [e.unique_id for e in added] == [f"{prefix}{lost}"]
    # The manager drops anything that is not missing; the factory itself must
    # not build more either.
    assert [
        type(e).__name__ for e in registration.entity_factory({f"{prefix}{lost}"})
    ] == expected_types
    assert all(isinstance(e, sensor.SensorEntity) for e in added)


# ---------------------------------------------------------------------------
# Sensor and tracker agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_same_status_as_tracker(
    freezer: FrozenDateTimeFactory,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Same coordinator data, same class on sensor and both tracker classes.

    Both platforms are set up against one coordinator; every tick commits a row
    through the coordinator's last-good writer and then runs each entity's own
    coordinator update. Ticks: a reliable fix, an estimated fix, a fix without
    accuracy (both read paths fall back to the last reliable fix, which has
    aged), a tick without new data past the threshold, a fresh fix, and a long
    tick without new data that makes the main tracker withhold its coordinates.

    On every tick the live class of both trackers (their own display row, not
    the held value) equals the sensor; the Last Location tracker renders
    ``stale`` as ``last_known``. Where a tracker publishes a new observation,
    its published value equals the sensor too. Where it does not, it holds the
    value of its last observation (#1321) and the sensor moves on: on tick 4
    both trackers hold ``aging``, because the main tracker's liveness gate reads
    the fresh accuracy-less row (age 0.5 T) while its status reads the display
    row (age 1.25 T); on tick 6 the main tracker withholds its coordinates,
    which is a new observation, and publishes ``stale``, while the Last Location
    tracker never withholds and holds ``current``.
    """

    del deterministic_config_subentry_id
    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")

    coordinator = _coordinator_class()([{"id": "dev-1", "name": "Pixel"}], _T)
    coordinator.last_update_success = True
    entry = _entry(coordinator)

    sensors: list[Any] = []
    trackers: list[Any] = []

    def _capture_sensors(entities: Iterable[Any], update_before_add: bool = False):
        sensors.extend(entities)

    def _capture_trackers(entities: Iterable[Any], update_before_add: bool = False):
        trackers.extend(entities)

    hass = SimpleNamespace(data={}, async_create_task=lambda coro, **_: coro.close())
    await sensor.async_setup_entry(hass, entry, _capture_sensors)
    await device_tracker.async_setup_entry(hass, entry, _capture_trackers)

    (status,) = _status_sensors(sensors)
    (main,) = [
        t for t in trackers if type(t) is device_tracker.GoogleFindMyDeviceTracker
    ]
    (last,) = [
        t
        for t in trackers
        if isinstance(t, device_tracker.GoogleFindMyLastLocationTracker)
    ]
    for entity in (status, main, last):
        # HA assigns the entity_id when it adds the entity.
        entity.entity_id = f"test.{entity.unique_id}"
        entity.async_write_ha_state = lambda: None

    def _tick(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is not None:
            coordinator.ingest("dev-1", row)
        for entity in (status, main, last):
            entity._handle_coordinator_update()
        live = status.native_value
        return {
            "sensor": "unknown" if live is None else live,
            "main_live": main._get_location_status(main._select_display_row()),
            "main_published": main._attr_extra_state_attributes["location_status"],
            "last_live": last._get_location_status(last._select_display_row()),
            "last_published": last._attr_extra_state_attributes["location_status"],
        }

    def _last_known(value: str) -> str:
        return "last_known" if value == "stale" else value

    observations = []
    start = time.time()
    observations.append(_tick(_row(start)))
    freezer.tick(60)
    observations.append(
        _tick(
            _row(time.time(), latitude=52.53, accuracy=200.0, accuracy_estimated=True)
        )
    )
    freezer.tick(_T * 0.75 - 60)
    observations.append(_tick(_row(time.time(), latitude=52.54, accuracy=None)))
    freezer.tick(_T * 0.5)
    observations.append(_tick(None))
    freezer.tick(30)
    observations.append(_tick(_row(time.time(), latitude=52.55)))
    freezer.tick(_T * 1.5)
    observations.append(_tick(None))

    # The sequence must actually walk through every class, else it proves little.
    assert [o["sensor"] for o in observations] == [
        "current",
        "current",
        "aging",
        "stale",
        "current",
        "stale",
    ]
    for o in observations:
        assert o["main_live"] == o["sensor"], o
        assert o["last_live"] == _last_known(o["sensor"]), o
    assert [o["main_published"] for o in observations] == [
        "current",
        "current",
        "aging",
        "aging",
        "current",
        "stale",
    ]
    assert [o["last_published"] for o in observations] == [
        "current",
        "current",
        "aging",
        "aging",
        "current",
        "current",
    ]

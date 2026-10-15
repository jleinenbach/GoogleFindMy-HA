# tests/test_sensor_coarse_fix.py
"""Behavior tests for ``GoogleFindMyCoarseFixSensor`` (#216).

The main device_tracker holds its ``coarse_*`` attributes with the observation
and the ``_last_location`` tracker carries none, so this sensor is the live
carrier of a coarse fix the accuracy gate discarded. The tests pin the
three rules of the sensor:

- (1) the state is the time of the fresh coarse fix and ``None`` (HA state
  ``unknown``) without one or after it expired; a coarse fix exists only while
  it is newer than the adopted fix, so the state is then later than
  ``sensor.*_last_seen``;
- (2) ``coarse_accuracy`` and the coordinates only under the ``coarse_*``
  names, never ``latitude``/``longitude`` (no map marker, no zone logic);
- (3) enabled by default, in the diagnostic category, a TIMESTAMP without
  ``state_class``.

The coarse fix enters through the coordinator's own producer
(``_record_coarse_fix``) and leaves through its freshness accessor
(``get_fresh_coarse_fix``), so the sensor is tested against the real rule,
not one invented here. Time is driven by ``freezer``, listed first.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.googlefindmy.const import (
    OPT_STALE_THRESHOLD,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory

_T = 600


def _row(last_seen: float) -> dict[str, Any]:
    return {
        "id": "dev-1",
        "device_id": "dev-1",
        "name": "Pixel",
        "latitude": 52.52,
        "longitude": 13.405,
        "accuracy": 12.0,
        "last_seen": last_seen,
    }


def _coarse(last_seen: float) -> dict[str, Any]:
    return {
        "latitude": 52.9,
        "longitude": 13.9,
        "accuracy": 1600.0,
        "last_seen": last_seen,
    }


def _coordinator(devices: Iterable[dict[str, Any]]) -> Any:
    """A coordinator inheriting the production cache and coarse-fix code."""

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )

    class _Coordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self._snapshot = list(devices)
            self.hass = SimpleNamespace()
            self.config_entry = make_config_entry(
                entry_id="entry-coarse", options={OPT_STALE_THRESHOLD: _T}
            )
            self.stats: dict[str, int] = {}
            self._device_names: dict[str, str] = {}
            self._device_location_data: dict[str, Any] = {}
            self._device_last_good_location: dict[str, Any] = {}
            self._device_caps: dict[str, Any] = {}
            self._present_last_seen: dict[str, float] = {}

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
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
            return True

    return _Coordinator()


async def _setup() -> tuple[Any, Any, Any]:
    """Run ``sensor.async_setup_entry``; return coordinator, coarse and LastSeen."""

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator = _coordinator([{"id": "dev-1", "name": "Pixel"}])
    added: list[Any] = []

    def _capture(entities: Iterable[Any], update_before_add: bool = False) -> None:
        for entity in entities:
            entity.entity_id = f"sensor.{entity.unique_id}"
            added.append(entity)

    entry = make_config_entry(
        entry_id="entry-coarse",
        runtime_data=coordinator,
        async_on_unload=lambda _callback: None,
    )
    await sensor.async_setup_entry(SimpleNamespace(), entry, _capture)
    coarse = [e for e in added if isinstance(e, sensor.GoogleFindMyCoarseFixSensor)]
    last_seen = [e for e in added if isinstance(e, sensor.GoogleFindMyLastSeenSensor)]
    assert len(coarse) == 1
    assert len(last_seen) == 1
    return coordinator, coarse[0], last_seen[0]


def _now() -> float:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow().timestamp()


@pytest.mark.asyncio
async def test_no_coarse_fix_is_unknown(freezer: FrozenDateTimeFactory) -> None:
    """Rule (1): without a discarded fix the state is ``None``, no attributes."""

    coordinator, entity, _last_seen = await _setup()
    coordinator._device_location_data["dev-1"] = _row(_now() - 60)

    assert entity.native_value is None
    assert entity.extra_state_attributes is None


@pytest.mark.asyncio
async def test_coarse_fix_newer_than_the_adopted_fix_is_the_state(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Rules (1) and (2): the coarse fix time is the state, later than LastSeen."""

    from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE

    coordinator, entity, _last_seen = await _setup()
    adopted_seen = _now() - 120
    coordinator._device_location_data["dev-1"] = _row(adopted_seen)
    coarse_seen = _now() - 30
    coordinator._record_coarse_fix("dev-1", _coarse(coarse_seen))

    stored = coordinator.get_coarse_fix("dev-1")["last_seen"]  # producer's unit
    assert abs(stored - coarse_seen) < 1
    assert entity.native_value == datetime.fromtimestamp(stored, tz=UTC)
    # sensor.*_last_seen shows the adopted fix: the coarse state is later.
    assert entity.native_value > datetime.fromtimestamp(adopted_seen, tz=UTC)
    attrs = entity.extra_state_attributes
    assert attrs == {
        "coarse_latitude": 52.9,
        "coarse_longitude": 13.9,
        "coarse_accuracy": 1600.0,
    }
    assert ATTR_LATITUDE not in attrs
    assert ATTR_LONGITUDE not in attrs


@pytest.mark.asyncio
async def test_expired_coarse_fix_is_unknown(freezer: FrozenDateTimeFactory) -> None:
    """Rule (1): past the stale threshold the accessor drops it, state ``None``."""

    coordinator, entity, _last_seen = await _setup()
    coordinator._device_location_data["dev-1"] = _row(_now() - 120)
    coordinator._record_coarse_fix("dev-1", _coarse(_now() - 30))
    assert entity.native_value is not None

    freezer.tick(_T)
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


@pytest.mark.asyncio
async def test_only_the_coarse_fields_it_has_are_published(
    freezer: FrozenDateTimeFactory,
) -> None:
    """A coarse fix without coordinates publishes only its accuracy."""

    coordinator, entity, _last_seen = await _setup()
    coordinator._device_coarse_fix = {
        "dev-1": {"latitude": None, "longitude": None, "accuracy": 900.0}
        | {"last_seen": _now() - 10}
    }
    assert entity.extra_state_attributes == {"coarse_accuracy": 900.0}

    coordinator._device_coarse_fix["dev-1"].update(
        latitude=52.9, longitude=13.9, accuracy=None
    )
    assert entity.extra_state_attributes == {
        "coarse_latitude": 52.9,
        "coarse_longitude": 13.9,
    }
    coordinator._device_coarse_fix["dev-1"]["latitude"] = None
    assert entity.extra_state_attributes is None


@pytest.mark.asyncio
async def test_unusable_coarse_stamp_or_missing_accessor_is_unknown(
    freezer: FrozenDateTimeFactory,
) -> None:
    """Defensive paths stay ``None`` instead of raising."""

    coordinator, entity, _last_seen = await _setup()
    coordinator.get_fresh_coarse_fix = lambda _dev: {"latitude": 1.0}
    assert entity.native_value is None

    coordinator.get_fresh_coarse_fix = None
    assert entity.native_value is None
    entity._device_id = None
    assert entity.extra_state_attributes is None


@pytest.mark.asyncio
async def test_entity_contract(freezer: FrozenDateTimeFactory) -> None:
    """Rule (3): TIMESTAMP, no state class, enabled, diagnostic, tracker scope, own key."""

    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.helpers.entity import EntityCategory

    _coordinator_obj, entity, _last_seen = await _setup()
    description = entity.entity_description
    assert description.key == "coarse_fix"
    assert description.translation_key == "coarse_fix"
    assert description.device_class == SensorDeviceClass.TIMESTAMP
    # The test stub's description has no ``state_class`` field at all; the real
    # one defaults to ``None``. Either way no state class is set.
    assert getattr(description, "state_class", None) is None
    assert entity._attr_entity_registry_enabled_default is True
    # The category comes from the description (the entity property falls back
    # to it), so assert it there; ``_attr_entity_category`` stays unset.
    assert description.entity_category == EntityCategory.DIAGNOSTIC
    assert entity.unique_id.endswith("_dev-1_coarse_fix")


@pytest.mark.asyncio
async def test_availability_follows_the_device(freezer: FrozenDateTimeFactory) -> None:
    """Available while the coordinator knows the device, not without an id."""

    coordinator, entity, _last_seen = await _setup()
    entity.coordinator_has_device = lambda: True
    coordinator.last_update_success = True
    assert entity.available is True
    # A failed coordinator update makes the sensor unavailable first.
    coordinator.last_update_success = False
    assert entity.available is False
    coordinator.last_update_success = True
    entity.coordinator_has_device = lambda: False
    assert entity.available is False
    entity._device_id = None
    assert entity.available is False


@pytest.mark.asyncio
async def test_coordinator_update_writes_once(freezer: FrozenDateTimeFactory) -> None:
    """Every tick writes once; a known device also syncs its label."""

    _coordinator_obj, entity, _last_seen = await _setup()
    writes: list[None] = []
    entity.async_write_ha_state = lambda: writes.append(None)

    entity.coordinator_has_device = lambda: False
    entity._handle_coordinator_update()
    assert writes == [None]

    renames: list[str] = []
    entity.coordinator_has_device = lambda: True
    entity.refresh_device_label_from_coordinator = lambda **_kwargs: None
    entity.maybe_update_device_registry_name = renames.append
    entity._handle_coordinator_update()
    assert writes == [None, None]
    assert renames == ["Pixel"]

    entity._device = {"id": "dev-1"}
    entity._handle_coordinator_update()
    assert writes == [None, None, None]
    assert renames == ["Pixel"]
    assert entity.device_info is not None


@pytest.mark.asyncio
async def test_one_write_reads_the_coarse_fix_once(
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
) -> None:
    """State and attributes of one write come from one read of the coarse fix.

    The accessor answers with a fix on the first call and with nothing on every
    later one, as if the fix expired between two reads. The written state and
    attributes must still belong together, and outside a write the sensor reads
    afresh again. The write is driven through ``_async_write_ha_state``, the
    method every Core write path ends in.
    """

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator, entity, _last_seen = await _setup()
    seen = _now() - 30
    answers: list[dict[str, Any] | None] = [
        {"last_seen": seen, "latitude": 1.5, "longitude": 2.5, "accuracy": 900.0}
    ]
    calls: list[str] = []

    def _fresh(dev_id: str) -> dict[str, Any] | None:
        calls.append(dev_id)
        return answers.pop(0) if answers else None

    coordinator.get_fresh_coarse_fix = _fresh
    written: list[tuple[Any, Any]] = []

    def _base_write(self: Any) -> None:
        written.append((self.native_value, self.extra_state_attributes))

    # The test stub of Entity has no ``_async_write_ha_state``; real Core does.
    monkeypatch.setattr(
        sensor.GoogleFindMyDeviceEntity,
        "_async_write_ha_state",
        _base_write,
        raising=False,
    )

    entity._async_write_ha_state()

    assert calls == ["dev-1"]
    assert written == [
        (
            datetime.fromtimestamp(seen, tz=UTC),
            {"coarse_latitude": 1.5, "coarse_longitude": 2.5, "coarse_accuracy": 900.0},
        )
    ]
    # The snapshot ends with the write: the next read sees the expired fix.
    assert entity.native_value is None
    assert entity.extra_state_attributes is None
    assert calls == ["dev-1", "dev-1", "dev-1"]


@pytest.mark.asyncio
async def test_a_failing_write_still_ends_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
) -> None:
    """An exception from the base write does not leave a stale snapshot behind."""

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    coordinator, entity, _last_seen = await _setup()
    answers: list[dict[str, Any] | None] = [{"last_seen": _now() - 30}]
    coordinator.get_fresh_coarse_fix = lambda _dev: answers.pop(0) if answers else None

    def _boom(self: Any) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(
        sensor.GoogleFindMyDeviceEntity, "_async_write_ha_state", _boom, raising=False
    )

    with pytest.raises(RuntimeError):
        entity._async_write_ha_state()
    assert entity.native_value is None


def test_the_sensor_hooks_the_write_every_core_path_ends_in() -> None:
    """The snapshot sits on ``_async_write_ha_state``, not on the public method.

    Core's ``async_write_ha_state``, ``async_update_ha_state`` (the
    ``update_entity`` service) and ``schedule_update_ha_state`` all end in
    ``_async_write_ha_state`` on every Core line the CI tests. From Core 2026.5 on
    ``async_write_ha_state`` is ``@final`` and Core names the private method as
    the one to override. An override of the public method would miss the other
    two paths.
    """

    sensor = importlib.import_module("custom_components.googlefindmy.sensor")
    own = sensor.GoogleFindMyCoarseFixSensor.__dict__
    assert "_async_write_ha_state" in own
    assert "async_write_ha_state" not in own

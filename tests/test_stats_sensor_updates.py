# tests/test_stats_sensor_updates.py
"""Regression tests for stats sensor updates after coordinator increments."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from types import ModuleType
from typing import Callable

import pytest

if "homeassistant.components.sensor" not in sys.modules:
    sensor_module = ModuleType("homeassistant.components.sensor")

    class _BaseSensorEntity:
        """Base stub matching Home Assistant SensorEntity API."""

        _attr_native_value: int | None = None

        async def async_added_to_hass(self) -> None:  # pragma: no cover - stub signature
            return None

        def async_write_ha_state(self) -> None:  # pragma: no cover - stub signature
            return None

    class RestoreSensor(_BaseSensorEntity):
        """Stub RestoreSensor inheriting the basic sensor behavior."""

    class SensorEntity(_BaseSensorEntity):
        """Stub SensorEntity mirroring RestoreSensor hierarchy."""

    class SensorDeviceClass:
        """Enum-like container for device class constants."""

        TIMESTAMP = "timestamp"

    class SensorStateClass:
        """Enum-like container for sensor state class constants."""

        TOTAL_INCREASING = "total_increasing"

    class SensorEntityDescription:
        """Lightweight stub for entity descriptions used in tests."""

        def __init__(self, key: str, **kwargs) -> None:  # noqa: D401 - stub signature
            self.key = key
            for name, value in kwargs.items():
                setattr(self, name, value)

    sensor_module.RestoreSensor = RestoreSensor
    sensor_module.SensorEntity = SensorEntity
    sensor_module.SensorDeviceClass = SensorDeviceClass
    sensor_module.SensorEntityDescription = SensorEntityDescription
    sensor_module.SensorStateClass = SensorStateClass
    sys.modules["homeassistant.components.sensor"] = sensor_module

if "homeassistant.helpers.entity" not in sys.modules:
    entity_module = ModuleType("homeassistant.helpers.entity")

    class DeviceInfo:  # noqa: D401 - stub signature
        def __init__(self, **kwargs) -> None:
            for name, value in kwargs.items():
                setattr(self, name, value)

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    entity_module.DeviceInfo = DeviceInfo
    entity_module.EntityCategory = EntityCategory
    sys.modules["homeassistant.helpers.entity"] = entity_module

if "homeassistant.helpers.entity_platform" not in sys.modules:
    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")

    class AddEntitiesCallback:  # noqa: D401 - stub signature
        def __call__(self, entities, update_before_add: bool = False) -> None:
            return None

    entity_platform_module.AddEntitiesCallback = AddEntitiesCallback
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform_module

if "homeassistant.helpers.network" not in sys.modules:
    network_module = ModuleType("homeassistant.helpers.network")
    network_module.get_url = lambda *args, **kwargs: "https://example.invalid"
    sys.modules["homeassistant.helpers.network"] = network_module

update_module = sys.modules.get("homeassistant.helpers.update_coordinator")
if update_module is not None and not hasattr(update_module, "CoordinatorEntity"):

    class CoordinatorEntity:
        """Minimal CoordinatorEntity stub storing the coordinator reference."""

        def __init__(self, coordinator) -> None:  # noqa: D401 - stub signature
            self.coordinator = coordinator

        def async_write_ha_state(self) -> None:  # pragma: no cover - stub signature
            return None

        def _handle_coordinator_update(self) -> None:
            self.async_write_ha_state()

    update_module.CoordinatorEntity = CoordinatorEntity

if update_module is not None:
    data_coordinator = getattr(update_module, "DataUpdateCoordinator", None)
    if data_coordinator is not None and not hasattr(data_coordinator, "async_add_listener"):
        original_init = data_coordinator.__init__

        def _init(self, *args, **kwargs):  # type: ignore[override]
            original_init(self, *args, **kwargs)
            self._listeners: list[Callable[[], None]] = []

        def _async_add_listener(self, update_callback):  # type: ignore[override]
            self._listeners.append(update_callback)

            def _remove() -> None:
                if update_callback in self._listeners:
                    self._listeners.remove(update_callback)

            return _remove

        def _async_update_listeners(self) -> None:  # type: ignore[override]
            for callback in list(getattr(self, "_listeners", [])):
                callback()

        data_coordinator.__init__ = _init  # type: ignore[assignment]
        data_coordinator.async_add_listener = _async_add_listener  # type: ignore[assignment]
        data_coordinator.async_update_listeners = _async_update_listeners  # type: ignore[assignment]

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import CONF_GOOGLE_EMAIL, DOMAIN
from custom_components.googlefindmy.sensor import STATS_DESCRIPTIONS, GoogleFindMyStatsSensor


class _StubHass:
    """Minimal Home Assistant stub exposing loop/create_task helpers."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict[str, dict] = {DOMAIN: {}}

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        return self.loop.create_task(coro, name=name)


class _StubConfigEntry:
    """Minimal ConfigEntry-like stub with deterministic identifiers."""

    def __init__(self) -> None:
        self.entry_id = "entry-stats"
        self.data = {CONF_GOOGLE_EMAIL: "user@example.com"}


class _StubCache:
    """No-op cache implementation satisfying coordinator expectations."""

    async def async_get_cached_value(self, key: str) -> None:  # noqa: D401 - stub signature
        return None

    async def async_set_cached_value(self, key: str, value: dict[str, int]) -> None:  # noqa: D401 - stub signature
        return None


def test_increment_stat_notifies_registered_stats_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stats increments must notify listeners so CoordinatorEntity state updates."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        hass = _StubHass(loop)
        coordinator = GoogleFindMyCoordinator(hass, cache=_StubCache())
        coordinator.config_entry = _StubConfigEntry()

        # Prevent background tasks from interfering with the test cleanup.
        monkeypatch.setattr(
            coordinator,
            "_schedule_stats_persist",
            lambda: None,
        )

        sensor = GoogleFindMyStatsSensor(
            coordinator,
            "background_updates",
            STATS_DESCRIPTIONS["background_updates"],
        )

        async def _exercise() -> None:
            notified = asyncio.Event()

            def _mark_notified() -> None:
                notified.set()

            sensor.async_write_ha_state = _mark_notified  # type: ignore[assignment]

            remove_listener = coordinator.async_add_listener(sensor._handle_coordinator_update)
            try:
                assert sensor.native_value == 0
                coordinator.increment_stat("background_updates")
                await asyncio.wait_for(notified.wait(), timeout=0.1)
                assert sensor.native_value == 1
            finally:
                remove_listener()

        loop.run_until_complete(_exercise())
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)

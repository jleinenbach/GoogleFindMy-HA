# tests/test_stats_sensor_updates.py
"""Regression tests for stats sensor updates after coordinator increments."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from collections.abc import Callable

import pytest

from tests.helpers import drain_loop

if "homeassistant.components.sensor" not in sys.modules:
    sensor_module = ModuleType("homeassistant.components.sensor")

    class _BaseSensorEntity:
        """Base stub matching Home Assistant SensorEntity API."""

        _attr_native_value: int | None = None

        async def async_added_to_hass(
            self,
        ) -> None:  # pragma: no cover - stub signature
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

device_registry_module = sys.modules.get("homeassistant.helpers.device_registry")
if device_registry_module is None:
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    sys.modules["homeassistant.helpers.device_registry"] = device_registry_module

if not hasattr(device_registry_module, "DeviceEntryType"):
    class DeviceEntryType:  # noqa: D401 - stub enum container
        SERVICE = "service"

    device_registry_module.DeviceEntryType = DeviceEntryType

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
    if data_coordinator is not None and not hasattr(
        data_coordinator, "async_add_listener"
    ):
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

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator  # noqa: E402
from custom_components.googlefindmy.const import (  # noqa: E402
    CONF_GOOGLE_EMAIL,
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    service_device_identifier,
)
from custom_components.googlefindmy.sensor import (  # noqa: E402
    STATS_DESCRIPTIONS,
    GoogleFindMyStatsSensor,
)


class _StubHass:
    """Minimal Home Assistant stub exposing loop/create_task helpers."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict[str, dict] = {DOMAIN: {}}
        self.states: SimpleNamespace = SimpleNamespace(get=lambda _eid: None)

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        return self.loop.create_task(coro, name=name)


class _StubConfigEntry:
    """Minimal ConfigEntry-like stub with deterministic identifiers."""

    def __init__(self) -> None:
        self.entry_id = "entry-stats"
        self.data = {CONF_GOOGLE_EMAIL: "user@example.com"}


class _StubCache:
    """No-op cache implementation satisfying coordinator expectations."""

    def __init__(self) -> None:
        self.saved_calls: list[tuple[str, dict[str, int]]] = []

    async def async_get_cached_value(self, key: str) -> None:  # noqa: D401 - stub signature
        return None

    async def async_set_cached_value(self, key: str, value: dict[str, int]) -> None:  # noqa: D401 - stub signature
        self.saved_calls.append((key, value))
        event = getattr(self, "event", None)
        if event is not None:
            event.set()


class _EntityRegistryStub:
    """Minimal entity registry supporting async_get_entity_id lookups."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], str] = {}

    def add(self, platform: str, domain: str, unique_id: str, entity_id: str) -> None:
        self._entries[(platform, domain, unique_id)] = entity_id

    def async_get_entity_id(
        self, platform: str, domain: str, unique_id: str
    ) -> str | None:
        return self._entries.get((platform, domain, unique_id))


def test_increment_stat_notifies_registered_stats_sensor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            subentry_key=SERVICE_SUBENTRY_KEY,
            subentry_identifier=coordinator.stable_subentry_identifier(
                key=SERVICE_SUBENTRY_KEY
            ),
        )

        assert sensor.subentry_key == SERVICE_SUBENTRY_KEY

        async def _exercise() -> None:
            notified = asyncio.Event()

            def _mark_notified() -> None:
                notified.set()

            sensor.async_write_ha_state = _mark_notified  # type: ignore[assignment]

            remove_listener = coordinator.async_add_listener(
                sensor._handle_coordinator_update
            )
            try:
                assert sensor.native_value == 0
                coordinator.increment_stat("background_updates")
                await asyncio.wait_for(notified.wait(), timeout=0.1)
                assert sensor.native_value == 1
            finally:
                remove_listener()

        loop.run_until_complete(_exercise())
    finally:
        drain_loop(loop)


def test_increment_stat_persists_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stats increments must trigger persistence via the debounced writer."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        hass = _StubHass(loop)
        cache = _StubCache()
        coordinator = GoogleFindMyCoordinator(hass, cache=cache)
        coordinator.config_entry = _StubConfigEntry()
        coordinator._stats_debounce_seconds = 0

        # Ensure debounced writes run immediately without scheduling real tasks.
        def _noop_schedule() -> None:
            return None

        monkeypatch.setattr(coordinator, "async_update_listeners", _noop_schedule)

        async def _exercise() -> None:
            stats_persisted = asyncio.Event()
            cache.event = stats_persisted
            coordinator.increment_stat("background_updates")
            await asyncio.wait_for(stats_persisted.wait(), timeout=0.1)
            assert cache.saved_calls
            key, value = cache.saved_calls[-1]
            assert key == "integration_stats"
            assert value["background_updates"] == 1

        loop.run_until_complete(_exercise())
    finally:
        drain_loop(loop)


def test_history_fallback_increments_history_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorder fallback should increment the history counter and surface via sensors."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        hass = _StubHass(loop)
        cache = _StubCache()
        coordinator = GoogleFindMyCoordinator(
            hass,
            cache=cache,
            allow_history_fallback=True,
        )
        coordinator.config_entry = _StubConfigEntry()

        # Disable persistence side effects for deterministic assertions.
        monkeypatch.setattr(coordinator, "_schedule_stats_persist", lambda: None)

        registry = _EntityRegistryStub()
        entry_id = coordinator.config_entry.entry_id
        registry.add(
            "device_tracker",
            DOMAIN,
            f"{entry_id}:device-1",
            "device_tracker.googlefindmy_device_1",
        )
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.er.async_get",
            lambda _hass: registry,
        )

        # No live state available -> force history fallback.
        hass.states = SimpleNamespace(get=lambda _eid: None)

        history_state = SimpleNamespace(
            attributes={
                "latitude": 48.137154,
                "longitude": 11.576124,
                "gps_accuracy": 25.0,
                "last_seen": "2024-02-05T08:00:00Z",
            },
            last_updated=datetime(2024, 2, 6, tzinfo=timezone.utc),
        )

        def _fake_get_last_state_changes(_hass, _limit, entity_ids):
            return {entity_ids[0]: [history_state]}

        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.recorder_history.get_last_state_changes",
            _fake_get_last_state_changes,
            raising=False,
        )

        class _RecorderStub:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.get_recorder",
            lambda _hass: _RecorderStub(),
            raising=False,
        )

        sensor = GoogleFindMyStatsSensor(
            coordinator,
            "history_fallback_used",
            STATS_DESCRIPTIONS["history_fallback_used"],
            subentry_key=SERVICE_SUBENTRY_KEY,
            subentry_identifier=coordinator.stable_subentry_identifier(
                key=SERVICE_SUBENTRY_KEY
            ),
        )

        async def _exercise() -> None:
            result = await coordinator._async_build_device_snapshot_with_fallbacks(
                [{"id": "device-1", "name": "Pixel"}]
            )
            assert result
            assert result[0]["status"] == "Using historical data"
            assert coordinator.stats["history_fallback_used"] == 1
            assert sensor.native_value == 1

        loop.run_until_complete(_exercise())
    finally:
        drain_loop(loop)


def test_stats_sensor_device_info_uses_service_identifiers() -> None:
    """Stats sensors attach the hub device identifier set with subentry metadata."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        hass = _StubHass(loop)
        coordinator = GoogleFindMyCoordinator(hass, cache=_StubCache())
        coordinator.config_entry = _StubConfigEntry()
        subentry_identifier = coordinator.stable_subentry_identifier(
            key=SERVICE_SUBENTRY_KEY
        )

        sensor = GoogleFindMyStatsSensor(
            coordinator,
            "background_updates",
            STATS_DESCRIPTIONS["background_updates"],
            subentry_key=SERVICE_SUBENTRY_KEY,
            subentry_identifier=subentry_identifier,
        )

        expected = {
            service_device_identifier("entry-stats"),
            (DOMAIN, f"entry-stats:{subentry_identifier}:service"),
        }

        assert sensor.subentry_key == SERVICE_SUBENTRY_KEY

        info = sensor.device_info
        assert info.identifiers == expected
        assert getattr(info, "config_entry_id", None) is None

        service_info = sensor.service_device_info(
            include_subentry_identifier=True
        )
        assert getattr(service_info, "config_entry_id", None) is None
    finally:
        drain_loop(loop)

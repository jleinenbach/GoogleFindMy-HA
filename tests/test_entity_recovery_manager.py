"""Tests for the EntityRecoveryManager recovery flows."""

from __future__ import annotations

import importlib.util
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if importlib.util.find_spec("pytest_homeassistant_custom_component") is None:  # pragma: no cover - optional dependency
    pytest.skip(
        "pytest-homeassistant-custom-component is required for the recovery manager tests",
        allow_module_level=True,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er


@pytest.fixture(autouse=True)
def use_real_homeassistant_modules() -> Iterator[None]:
    """Temporarily replace the stubbed Home Assistant modules with the real ones."""

    import sys

    saved_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("homeassistant")
    }
    for name in list(sys.modules):
        if name.startswith("homeassistant"):
            del sys.modules[name]

    import homeassistant  # noqa: F401  # ensure the real package is loaded
    from homeassistant.helpers import aiohttp_client as _aiohttp_client

    if not hasattr(_aiohttp_client, "_async_make_resolver"):

        async def _async_make_resolver(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - plugin shim
            return None

        _aiohttp_client._async_make_resolver = _async_make_resolver  # type: ignore[attr-defined]

    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.startswith("homeassistant"):
                del sys.modules[name]
        sys.modules.update(saved_modules)


@pytest.mark.asyncio
async def test_entity_recovery_manager_recovers_missing_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """The manager should recover missing entities across all registered platforms."""

    from custom_components.googlefindmy import EntityRecoveryManager
    from custom_components.googlefindmy.binary_sensor import (
        GoogleFindMyAuthStatusSensor,
        GoogleFindMyPollingSensor,
    )
    from custom_components.googlefindmy.button import (
        GoogleFindMyButtonEntity,
        GoogleFindMyLocateButton,
        GoogleFindMyPlaySoundButton,
        GoogleFindMyStopSoundButton,
    )
    from custom_components.googlefindmy.const import (
        DOMAIN,
        OPT_ENABLE_STATS_ENTITIES,
        SERVICE_SUBENTRY_KEY,
        TRACKER_SUBENTRY_KEY,
    )
    from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker
    from custom_components.googlefindmy.sensor import (
        STATS_DESCRIPTIONS,
        GoogleFindMyLastSeenSensor,
        GoogleFindMyStatsSensor,
    )

    tracker_devices = [
        {
            "id": "tracker-1",
            "name": "Keys",
            "last_seen": "2024-06-01T10:15:00Z",
        },
        {
            "id": "tracker-2",
            "name": "Backpack",
            "last_seen": "2024-06-01T10:16:00Z",
        },
    ]

    class _StubCoordinator:
        def __init__(self, hass_obj: HomeAssistant, entry: MockConfigEntry) -> None:
            self.hass = hass_obj
            self.config_entry = entry
            self.stats = {
                "background_updates": 5,
                "timeouts": 1,
            }
            self._snapshots = {
                TRACKER_SUBENTRY_KEY: list(tracker_devices),
            }
            self._visible = {
                TRACKER_SUBENTRY_KEY: {device["id"] for device in tracker_devices},
            }
            self._present = set(self._visible[TRACKER_SUBENTRY_KEY])
            self._stable_ids = {
                TRACKER_SUBENTRY_KEY: "tracker-stable",
                SERVICE_SUBENTRY_KEY: "service-stable",
            }
            self._listeners: list[Callable[[], None]] = []

        def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
            self._listeners.append(listener)
            return lambda: None

        def get_subentry_metadata(
            self, *, feature: str | None = None, key: str | None = None
        ) -> SimpleNamespace:
            mapping = {
                "device_tracker": TRACKER_SUBENTRY_KEY,
                "button": TRACKER_SUBENTRY_KEY,
                "sensor": TRACKER_SUBENTRY_KEY,
                "binary_sensor": SERVICE_SUBENTRY_KEY,
            }
            resolved = key or mapping.get(feature or "", TRACKER_SUBENTRY_KEY)
            return SimpleNamespace(key=resolved)

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            resolved = key or self.get_subentry_metadata(feature=feature).key
            return self._stable_ids.get(resolved, f"{resolved}-stable")

        def get_subentry_snapshot(
            self, key: str | None = None, *, feature: str | None = None
        ) -> list[dict[str, Any]]:
            resolved = key or self.get_subentry_metadata(feature=feature).key
            return [dict(row) for row in self._snapshots.get(resolved, [])]

        def is_device_visible_in_subentry(self, subentry_key: str, device_id: str) -> bool:
            return device_id in self._visible.get(subentry_key, set())

        def is_device_present(self, device_id: str) -> bool:
            return device_id in self._present

        def find_tracker_entity_entry(self, device_id: str) -> None:  # pragma: no cover - compatibility
            return None

        def get_device_location_data_for_subentry(
            self, subentry_key: str, device_id: str
        ) -> dict[str, Any] | None:
            if not self.is_device_visible_in_subentry(subentry_key, device_id):
                return None
            for row in self._snapshots.get(subentry_key, []):
                if row.get("id") == device_id:
                    return dict(row)
            return None

        def get_device_last_seen(self, device_id: str) -> str | None:
            data = self.get_device_location_data_for_subentry(
                TRACKER_SUBENTRY_KEY, device_id
            )
            if not data:
                return None
            return data.get("last_seen")

        def can_play_sound(self, _device_id: str) -> bool:
            return True

        def can_stop_sound(self, _device_id: str) -> bool:
            return True

        def can_request_manual_locate(self, _device_id: str) -> bool:
            return True

        async def async_request_refresh(self) -> None:  # pragma: no cover - defensive
            return None

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-test",
        data={},
        options={OPT_ENABLE_STATS_ENTITIES: True},
    )
    entry.add_to_hass(hass)

    coordinator = _StubCoordinator(hass, entry)
    runtime_data = SimpleNamespace(coordinator=coordinator)

    manager = EntityRecoveryManager(hass, entry, coordinator)
    runtime_data.entity_recovery_manager = manager
    entry.runtime_data = runtime_data

    tracker_subentry_identifier = coordinator.stable_subentry_identifier(
        key=TRACKER_SUBENTRY_KEY
    )
    service_subentry_identifier = coordinator.stable_subentry_identifier(
        key=SERVICE_SUBENTRY_KEY
    )

    added_entities: dict[str, list[Any]] = defaultdict(list)

    def _capture(platform: str) -> Callable[[Sequence[Any], bool], None]:
        def _inner(entities: Sequence[Any], update_before_add: bool = False) -> None:
            assert update_before_add is True
            added_entities[platform].extend(entities)

        return _inner

    def _tracker_is_visible(device_id: str) -> bool:
        return coordinator.is_device_visible_in_subentry(
            TRACKER_SUBENTRY_KEY, device_id
        )

    def _expected_tracker_ids() -> set[str]:
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return set()
        expected: set[str] = set()
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            if not isinstance(dev_id, str) or not dev_id or not _tracker_is_visible(dev_id):
                continue
            expected.add(f"{entry_id}:{tracker_subentry_identifier}:{dev_id}")
        return expected

    def _build_trackers(missing: set[str]) -> list[GoogleFindMyDeviceTracker]:
        built: list[GoogleFindMyDeviceTracker] = []
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return built
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            if not isinstance(dev_id, str) or not dev_id or not _tracker_is_visible(dev_id):
                continue
            uid = f"{entry_id}:{tracker_subentry_identifier}:{dev_id}"
            if uid not in missing:
                continue
            built.append(
                GoogleFindMyDeviceTracker(
                    coordinator,
                    device,
                    subentry_key=TRACKER_SUBENTRY_KEY,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
        return built

    manager.register_device_tracker_platform(
        expected_unique_ids=_expected_tracker_ids,
        entity_factory=_build_trackers,
        add_entities=_capture("device_tracker"),
    )

    def _expected_button_ids() -> set[str]:
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return set()
        expected: set[str] = set()
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            if not isinstance(dev_id, str) or not dev_id or not _tracker_is_visible(dev_id):
                continue
            for action in ("play_sound", "stop_sound", "locate_device"):
                expected.add(
                    f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_{action}"
                )
        return expected

    def _build_buttons(missing: set[str]) -> list[GoogleFindMyButtonEntity]:
        built: list[GoogleFindMyButtonEntity] = []
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return built
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            if not isinstance(dev_id, str) or not dev_id or not _tracker_is_visible(dev_id):
                continue
            for action, entity_cls in {
                "play_sound": GoogleFindMyPlaySoundButton,
                "stop_sound": GoogleFindMyStopSoundButton,
                "locate_device": GoogleFindMyLocateButton,
            }.items():
                uid = f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_{action}"
                if uid not in missing:
                    continue
                built.append(
                    entity_cls(
                        coordinator,
                        device,
                        device.get("name"),
                        subentry_key=TRACKER_SUBENTRY_KEY,
                        subentry_identifier=tracker_subentry_identifier,
                    )
                )
        return built

    manager.register_button_platform(
        expected_unique_ids=_expected_button_ids,
        entity_factory=_build_buttons,
        add_entities=_capture("button"),
    )

    created_stats = ["background_updates", "timeouts"]

    def _expected_sensor_ids() -> set[str]:
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return set()
        expected: set[str] = set()
        for stat_key in created_stats:
            expected.add(f"{DOMAIN}_{entry_id}_{service_subentry_identifier}_{stat_key}")
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            name = device.get("name")
            if (
                not isinstance(dev_id, str)
                or not dev_id
                or not isinstance(name, str)
                or not name
                or not _tracker_is_visible(dev_id)
            ):
                continue
            expected.add(
                f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_last_seen"
            )
        return expected

    def _build_sensors(missing: set[str]) -> list[Any]:
        built: list[Any] = []
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return built
        for stat_key in created_stats:
            uid = f"{DOMAIN}_{entry_id}_{service_subentry_identifier}_{stat_key}"
            if uid not in missing:
                continue
            description = STATS_DESCRIPTIONS[stat_key]
            built.append(
                GoogleFindMyStatsSensor(
                    coordinator,
                    stat_key,
                    description,
                    subentry_key=SERVICE_SUBENTRY_KEY,
                    subentry_identifier=service_subentry_identifier,
                )
            )
        for device in coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY):
            dev_id = device.get("id")
            name = device.get("name")
            if (
                not isinstance(dev_id, str)
                or not dev_id
                or not isinstance(name, str)
                or not name
                or not _tracker_is_visible(dev_id)
            ):
                continue
            uid = (
                f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_last_seen"
            )
            if uid not in missing:
                continue
            built.append(
                GoogleFindMyLastSeenSensor(
                    coordinator,
                    device,
                    subentry_key=TRACKER_SUBENTRY_KEY,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
        return built

    manager.register_sensor_platform(
        expected_unique_ids=_expected_sensor_ids,
        entity_factory=_build_sensors,
        add_entities=_capture("sensor"),
    )

    def _expected_binary_sensor_ids() -> set[str]:
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return set()
        return {
            f"{entry_id}:{service_subentry_identifier}:polling",
            f"{entry_id}:{service_subentry_identifier}:auth_status",
        }

    def _build_binary_sensors(missing: set[str]) -> list[Any]:
        built: list[Any] = []
        entry_id = entry.entry_id
        if not isinstance(entry_id, str) or not entry_id:
            return built
        mapping = {
            f"{entry_id}:{service_subentry_identifier}:polling": lambda: GoogleFindMyPollingSensor(
                coordinator,
                entry,
                subentry_key=SERVICE_SUBENTRY_KEY,
                subentry_identifier=service_subentry_identifier,
            ),
            f"{entry_id}:{service_subentry_identifier}:auth_status": lambda: GoogleFindMyAuthStatusSensor(
                coordinator,
                entry,
                subentry_key=SERVICE_SUBENTRY_KEY,
                subentry_identifier=service_subentry_identifier,
            ),
        }
        for unique_id, factory in mapping.items():
            if unique_id in missing:
                built.append(factory())
        return built

    manager.register_binary_sensor_platform(
        expected_unique_ids=_expected_binary_sensor_ids,
        entity_factory=_build_binary_sensors,
        add_entities=_capture("binary_sensor"),
    )

    tracker_ids = sorted(_expected_tracker_ids())
    button_ids = sorted(_expected_button_ids())
    sensor_ids = sorted(_expected_sensor_ids())
    binary_ids = sorted(_expected_binary_sensor_ids())

    assert len(tracker_ids) == 2
    assert len(button_ids) == 6
    assert len(sensor_ids) == 4
    assert len(binary_ids) == 2

    entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        tracker_ids[0],
        config_entry=entry,
    )
    for uid in button_ids[:3]:
        entity_registry.async_get_or_create(
            "button",
            DOMAIN,
            uid,
            config_entry=entry,
        )
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        sensor_ids[0],
        config_entry=entry,
    )
    entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        binary_ids[0],
        config_entry=entry,
    )

    await manager.async_recover_missing_entities()

    recovered_trackers = {entity.unique_id for entity in added_entities["device_tracker"]}
    assert recovered_trackers == {tracker_ids[1]}

    recovered_buttons = {entity.unique_id for entity in added_entities["button"]}
    assert recovered_buttons == set(button_ids[3:])

    recovered_sensors = {entity.unique_id for entity in added_entities["sensor"]}
    assert recovered_sensors == set(sensor_ids[1:])

    recovered_binary = {entity.unique_id for entity in added_entities["binary_sensor"]}
    assert recovered_binary == {binary_ids[1]}

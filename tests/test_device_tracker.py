# tests/test_device_tracker.py
"""Device tracker regression tests covering registry deduplication helpers."""

from __future__ import annotations

# tests/test_device_tracker.py

import asyncio
import importlib
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import DOMAIN, TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


class _EntityRegistryStub:
    """Minimal entity registry exposing lookups used by the coordinator."""

    def __init__(self) -> None:
        self._entity_index: dict[tuple[str, str, str], str] = {}
        self.entities: dict[str, SimpleNamespace] = {}

    def add(
        self,
        *,
        entity_id: str,
        unique_id: str,
        domain: str = "device_tracker",
        platform: str = DOMAIN,
        config_entry_id: str | None = None,
    ) -> None:
        entry = SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            domain=domain,
            platform=platform,
            config_entry_id=config_entry_id,
        )
        self.entities[entity_id] = entry
        self._entity_index[(domain, platform, unique_id)] = entity_id

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        return self._entity_index.get((domain, platform, unique_id))

    def async_get(self, entity_id: str) -> SimpleNamespace | None:
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id: str, *, new_unique_id: str) -> None:
        entry = self.entities.get(entity_id)
        if entry is None:
            raise ValueError(f"Entity {entity_id} not found")

        old_key = (entry.domain, entry.platform, entry.unique_id)
        self._entity_index.pop(old_key, None)

        entry.unique_id = new_unique_id
        self._entity_index[(entry.domain, entry.platform, new_unique_id)] = entity_id


def test_find_tracker_entity_entry_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallback scanning should locate tracker entries with legacy unique IDs."""

    registry = _EntityRegistryStub()
    registry.add(
        entity_id="device_tracker.googlefindmy_backpack",
        unique_id="tracker-subentry:tracker-42",
        config_entry_id="entry-42",
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.er.async_get",
        lambda hass: registry,
    )

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = SimpleNamespace()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-42")
    coordinator.get_device_display_name = lambda device_id: f"Tracker {device_id}"

    entry = coordinator.find_tracker_entity_entry("tracker-42")

    assert entry is not None
    assert entry.entity_id == "device_tracker.googlefindmy_backpack"
    assert entry.unique_id.startswith("entry-42:")
    assert entry.unique_id.endswith(":tracker-42")


def test_scanner_instantiates_tracker_for_known_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The device tracker platform should hydrate a tracker even if the registry already has it."""

    device_tracker = importlib.import_module("custom_components.googlefindmy.device_tracker")

    async def _fake_trigger_cloud_discovery(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        device_tracker,
        "_trigger_cloud_discovery",
        _fake_trigger_cloud_discovery,
    )

    scheduled: list[asyncio.Task[Any]] = []

    def _async_create_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass = SimpleNamespace(async_create_task=_async_create_task, data={})

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self.hass = hass
            self.config_entry = None
            self._listeners: list[Any] = []
            self._snapshot_calls = 0
            self.lookup_calls: list[str] = []

        def async_add_listener(self, listener):
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(self, *, key: str | None = None, feature: str | None = None) -> str:
            return "tracker-subentry"

        def get_subentry_metadata(self, *, key: str | None = None, feature: str | None = None) -> Any:
            return SimpleNamespace(key=TRACKER_SUBENTRY_KEY)

        def get_subentry_snapshot(self, key: str | None = None, *, feature: str | None = None) -> list[dict[str, Any]]:
            self._snapshot_calls += 1
            if self._snapshot_calls == 1:
                return []
            return [{"id": "tracker-1", "name": "Keys"}]

        def find_tracker_entity_entry(self, device_id: str):
            self.lookup_calls.append(device_id)
            return SimpleNamespace(
                entity_id="device_tracker.googlefindmy_keys",
                unique_id="tracker-subentry:tracker-1",
            )

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-1"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}
            self._callbacks: list[Any] = []

        def async_on_unload(self, callback: Any) -> None:
            self._callbacks.append(callback)

    coordinator = _StubCoordinator()
    entry = _StubConfigEntry(coordinator)
    coordinator.config_entry = entry

    added: list[list[Any]] = []

    def _capture_entities(entities: list[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))
        assert update_before_add is True

    async def _exercise() -> None:
        await device_tracker.async_setup_entry(hass, entry, _capture_entities)
        for task in scheduled:
            await task

    asyncio.run(_exercise())

    assert coordinator.lookup_calls == ["tracker-1"]
    assert added and len(added[-1]) == 1
    tracker_entity = added[-1][0]
    assert tracker_entity.unique_id == "entry-1:tracker-subentry:tracker-1"
    assert tracker_entity.device_id == "tracker-1"
    assert entry._callbacks, "async_on_unload should register cleanup callbacks"
    for task in scheduled:
        assert task.done()


def test_initial_snapshot_hydrates_registry_tracker() -> None:
    """Startup population should still create a tracker entity when the registry already knows it."""

    device_tracker = importlib.import_module("custom_components.googlefindmy.device_tracker")

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self.hass = SimpleNamespace(async_create_task=lambda coro: coro)
            self.config_entry = None
            self._listeners: list[Any] = []
            self.lookup_calls: list[str] = []

        def async_add_listener(self, listener):
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(self, *, key: str | None = None, feature: str | None = None) -> str:
            return "tracker-subentry"

        def get_subentry_metadata(self, *, key: str | None = None, feature: str | None = None) -> Any:
            return SimpleNamespace(key=TRACKER_SUBENTRY_KEY)

        def get_subentry_snapshot(self, key: str | None = None, *, feature: str | None = None) -> list[dict[str, Any]]:
            return [{"id": "tracker-1", "name": "Keys"}]

        def find_tracker_entity_entry(self, device_id: str):
            self.lookup_calls.append(device_id)
            return SimpleNamespace(
                entity_id="device_tracker.googlefindmy_keys",
                unique_id="tracker-subentry:tracker-1",
            )

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-1"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}

        def async_on_unload(self, callback: Any) -> None:
            pass

    coordinator = _StubCoordinator()
    entry = _StubConfigEntry(coordinator)
    coordinator.config_entry = entry

    added: list[list[Any]] = []

    def _capture_entities(entities: list[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))
        assert update_before_add is True

    asyncio.run(device_tracker.async_setup_entry(coordinator.hass, entry, _capture_entities))

    assert added and len(added[0]) == 1
    tracker_entity = added[0][0]
    assert tracker_entity.unique_id == "entry-1:tracker-subentry:tracker-1"
    assert coordinator.lookup_calls == ["tracker-1"]


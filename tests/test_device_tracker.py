# tests/test_device_tracker.py
"""Device tracker regression tests covering registry deduplication helpers."""

from __future__ import annotations

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
    assert entry.unique_id == "tracker-subentry:tracker-42"


def test_scanner_skips_entities_already_in_registry() -> None:
    """The device tracker platform should not create duplicates for known devices."""

    device_tracker = importlib.import_module("custom_components.googlefindmy.device_tracker")

    scheduled: list[asyncio.Task[Any]] = []

    def _async_create_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass = SimpleNamespace(async_create_task=_async_create_task)

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
        assert not added
        for task in scheduled:
            await task

    asyncio.run(_exercise())

    assert coordinator.lookup_calls == ["tracker-1"]
    assert not added
    assert entry._callbacks, "async_on_unload should register cleanup callbacks"
    for task in scheduled:
        assert task.done()


def test_initial_snapshot_skips_registry_duplicates() -> None:
    """Startup population should respect existing registry entries."""

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

    assert not added
    assert coordinator.lookup_calls == ["tracker-1"]


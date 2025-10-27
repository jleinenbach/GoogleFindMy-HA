# tests/test_options_flow_registry_updates.py
"""Tests asserting subentry repair steps update registry assignments."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

from custom_components.googlefindmy import (
    config_flow,
    ConfigEntrySubEntryManager,
    ConfigEntrySubentryDefinition,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from homeassistant.config_entries import ConfigSubentry


class _RegistryTracker:
    """Track entity/device registry assignments for verification."""

    def __init__(self) -> None:
        self.by_subentry: dict[str, tuple[str, ...]] = {}
        self.by_device: dict[str, str] = {}
        self.history: list[tuple[str, tuple[str, ...]]] = []
        self.removals: list[str] = []

    def apply(self, subentry_id: str, device_ids: tuple[str, ...]) -> None:
        for dev_id in self.by_subentry.get(subentry_id, ()):  # clear previous mapping
            self.by_device.pop(dev_id, None)
        normalized = tuple(dict.fromkeys(device_ids))
        for dev_id in normalized:
            prior = self.by_device.pop(dev_id, None)
            if prior and prior != subentry_id:
                prev_devices = list(self.by_subentry.get(prior, ()))
                if dev_id in prev_devices:
                    prev_devices.remove(dev_id)
                    self.by_subentry[prior] = tuple(prev_devices)
        self.by_subentry[subentry_id] = normalized
        for dev_id in normalized:
            self.by_device[dev_id] = subentry_id
        self.history.append((subentry_id, normalized))

    def remove_for_subentry(self, subentry_id: str) -> None:
        for dev_id, owner in list(self.by_device.items()):
            if owner == subentry_id:
                self.by_device.pop(dev_id, None)
        self.by_subentry.pop(subentry_id, None)
        self.removals.append(subentry_id)


class _ManagerWithRegistries:
    """Config entries manager stub that mirrors registry updates."""

    def __init__(
        self,
        entry: _EntryStub,
        entity_registry: _RegistryTracker,
        device_registry: _RegistryTracker,
    ) -> None:
        self._entry = entry
        self.entity_registry = entity_registry
        self.device_registry = device_registry
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []
        self.reloads: list[str] = []

    def async_update_entry(self, entry: _EntryStub, *, data: dict[str, Any]) -> None:
        assert entry is self._entry
        entry.data = data

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

    def async_update_subentry(
        self,
        entry: _EntryStub,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
    ) -> None:
        assert entry is self._entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        self.updated.append((subentry.subentry_id, dict(data)))
        visible = tuple(data.get("visible_device_ids", ()))
        self.entity_registry.apply(subentry.subentry_id, visible)
        self.device_registry.apply(subentry.subentry_id, visible)

    def async_add_subentry(self, entry: _EntryStub, subentry: ConfigSubentry) -> None:
        assert entry is self._entry
        entry.subentries[subentry.subentry_id] = subentry
        visible = tuple(subentry.data.get("visible_device_ids", ()))
        self.entity_registry.apply(subentry.subentry_id, visible)
        self.device_registry.apply(subentry.subentry_id, visible)

    def async_remove_subentry(self, entry: _EntryStub, subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self._entry
        removed = entry.subentries.pop(subentry_id, None)
        if removed is None:
            return False
        self.entity_registry.remove_for_subentry(subentry_id)
        self.device_registry.remove_for_subentry(subentry_id)
        self.removed.append(subentry_id)
        return True

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class _HassStub:
    """Home Assistant stub exposing registries and config entry helpers."""

    def __init__(
        self,
        entry: _EntryStub,
        entity_registry: _RegistryTracker,
        device_registry: _RegistryTracker,
    ) -> None:
        self.entity_registry = entity_registry
        self.device_registry = device_registry
        self.config_entries = _ManagerWithRegistries(
            entry, entity_registry, device_registry
        )
        self.data: dict[str, Any] = {}

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


class _EntryStub:
    """Config entry stub for options flow registry tests."""

    def __init__(self) -> None:
        self.entry_id = "entry-options"
        self.title = "Find My"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=[]))

    def add_subentry(
        self,
        *,
        key: str,
        title: str,
        visible_device_ids: list[str] | None,
    ) -> ConfigSubentry:
        payload: dict[str, Any] = {"group_key": key}
        if visible_device_ids is not None:
            payload["visible_device_ids"] = list(visible_device_ids)
        subentry = ConfigSubentry(
            data=MappingProxyType(payload),
            subentry_type="googlefindmy_feature_group",
            title=title,
            unique_id=f"{self.entry_id}-{key}",
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry


def _build_flow(entry: _EntryStub, hass: _HassStub) -> config_flow.OptionsFlowHandler:
    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow


def test_repairs_move_updates_registries_for_devices() -> None:
    """Moving devices should update both entity and device registries."""

    entry = _EntryStub()
    target = entry.add_subentry(
        key="target", title="Target", visible_device_ids=["dev-1"]
    )
    other = entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-2"])
    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    entity_registry.apply(target.subentry_id, ("dev-1",))
    entity_registry.apply(other.subentry_id, ("dev-2",))
    device_registry.apply(target.subentry_id, ("dev-1",))
    device_registry.apply(other.subentry_id, ("dev-2",))

    hass = _HassStub(entry, entity_registry, device_registry)
    flow = _build_flow(entry, hass)

    entry.runtime_data.coordinator.data = [
        {"device_id": "dev-1", "name": "Device 1"},
        {"device_id": "dev-2", "name": "Device 2"},
    ]

    async def _invoke() -> dict[str, Any]:
        result = await flow.async_step_repairs_move(
            {"target_subentry": "target", "device_ids": ["dev-1", "dev-2"]}
        )
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_invoke())

    assert result["type"] == "abort"
    manager = hass.config_entries
    assert manager.updated
    assert entity_registry.by_device == {
        "dev-1": target.subentry_id,
        "dev-2": target.subentry_id,
    }
    assert device_registry.by_device == {
        "dev-1": target.subentry_id,
        "dev-2": target.subentry_id,
    }
    assert entity_registry.by_subentry[other.subentry_id] == ()
    assert device_registry.by_subentry[other.subentry_id] == ()
    assert manager.reloads == [entry.entry_id]


def test_repairs_delete_removes_registry_entries() -> None:
    """Deleting a subentry should clear registry assignments for that subentry."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-3", "dev-4"]
    )
    fallback = entry.add_subentry(
        key="keep", title="Keep", visible_device_ids=["dev-5"]
    )
    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    entity_registry.apply(removable.subentry_id, ("dev-3", "dev-4"))
    entity_registry.apply(fallback.subentry_id, ("dev-5",))
    device_registry.apply(removable.subentry_id, ("dev-3", "dev-4"))
    device_registry.apply(fallback.subentry_id, ("dev-5",))

    hass = _HassStub(entry, entity_registry, device_registry)
    flow = _build_flow(entry, hass)

    async def _invoke_delete() -> dict[str, Any]:
        result = await flow.async_step_repairs_delete(
            {"delete_subentry": "remove", "fallback_subentry": "keep"}
        )
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_invoke_delete())

    assert result["type"] == "abort"
    manager = hass.config_entries
    assert removable.subentry_id in manager.removed
    assert entity_registry.by_device == {
        "dev-3": fallback.subentry_id,
        "dev-4": fallback.subentry_id,
        "dev-5": fallback.subentry_id,
    }
    assert device_registry.by_device == entity_registry.by_device
    assert fallback.subentry_id in entity_registry.by_subentry
    assert entity_registry.by_subentry[fallback.subentry_id] == (
        "dev-3",
        "dev-4",
        "dev-5",
    )
    assert entity_registry.removals == [removable.subentry_id]
    assert device_registry.removals == [removable.subentry_id]


def test_coordinator_propagates_visible_devices_to_registries() -> None:
    """Coordinator updates must synchronize subentry visibility and registries."""

    entry = _EntryStub()
    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    hass = _HassStub(entry, entity_registry, device_registry)

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    coordinator.data = [
        {"device_id": "dev-1", "name": "Device 1"},
        {"device_id": "dev-2", "name": "Device 2"},
    ]
    coordinator._enabled_poll_device_ids = {"dev-1", "dev-2"}
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = "core_tracking"

    subentry_manager = ConfigEntrySubEntryManager(hass, entry)

    core_definition = ConfigEntrySubentryDefinition(
        key="core_tracking",
        title="Core",
        data={"features": ["device_tracker"]},
    )
    secondary_definition = ConfigEntrySubentryDefinition(
        key="secondary",
        title="Secondary",
        data={
            "features": ["sensor"],
            "visible_device_ids": ["dev-2"],
        },
    )

    asyncio.run(subentry_manager.async_sync([core_definition, secondary_definition]))

    coordinator.attach_subentry_manager(subentry_manager)
    coordinator._refresh_subentry_index(coordinator.data)

    core_subentry = subentry_manager.get("core_tracking")
    secondary_subentry = subentry_manager.get("secondary")
    assert core_subentry is not None
    assert secondary_subentry is not None

    assert tuple(core_subentry.data.get("visible_device_ids", ())) == (
        "dev-1",
        "dev-2",
    )
    assert tuple(secondary_subentry.data.get("visible_device_ids", ())) == ("dev-2",)

    assert entity_registry.by_subentry[core_subentry.subentry_id] == (
        "dev-1",
        "dev-2",
    )
    assert device_registry.by_subentry[core_subentry.subentry_id] == (
        "dev-1",
        "dev-2",
    )
    assert entity_registry.by_subentry[secondary_subentry.subentry_id] == ()
    assert device_registry.by_subentry[secondary_subentry.subentry_id] == ()
    assert entity_registry.by_device.get("dev-1") == core_subentry.subentry_id
    assert device_registry.by_device.get("dev-1") == core_subentry.subentry_id
    assert entity_registry.history[-1] == (
        core_subentry.subentry_id,
        ("dev-1", "dev-2"),
    )
    assert device_registry.history[-1] == (
        core_subentry.subentry_id,
        ("dev-1", "dev-2"),
    )

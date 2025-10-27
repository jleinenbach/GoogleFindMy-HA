# tests/test_unload_subentry_cleanup.py
"""Tests verifying unload removes subentries and registry assignments."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import custom_components.googlefindmy as integration
from custom_components.googlefindmy.const import DOMAIN
from homeassistant.config_entries import ConfigSubentry


class _RegistryTracker:
    """Track registry cleanup operations."""

    def __init__(self) -> None:
        self.by_subentry: dict[str, tuple[str, ...]] = {}
        self.removals: list[str] = []

    def apply(self, subentry_id: str, device_ids: tuple[str, ...]) -> None:
        self.by_subentry[subentry_id] = device_ids

    def remove_for_subentry(self, subentry_id: str) -> None:
        self.by_subentry.pop(subentry_id, None)
        self.removals.append(subentry_id)


class _SubentryManagerStub:
    """Stub for ConfigEntrySubEntryManager capturing cleanup calls."""

    def __init__(
        self,
        entry: _EntryStub,
        entity_registry: _RegistryTracker,
        device_registry: _RegistryTracker,
    ) -> None:
        self._entry = entry
        self.entity_registry = entity_registry
        self.device_registry = device_registry
        self.removed: list[str] = []

    async def async_remove_all(self) -> None:
        for subentry_id in list(self._entry.subentries):
            self._entry.subentries.pop(subentry_id, None)
            self.entity_registry.remove_for_subentry(subentry_id)
            self.device_registry.remove_for_subentry(subentry_id)
            self.removed.append(subentry_id)


class _AsyncLock:
    """Minimal async lock stub used by the unload test."""

    async def __aenter__(self) -> _AsyncLock:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _ConfigEntriesHelper:
    """Subset of hass.config_entries used during unload."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        self.removed_subentries: list[str] = []

    async def async_unload_platforms(
        self, entry: _EntryStub, platforms: list[str]
    ) -> bool:
        assert entry is self._entry
        return True

    def async_remove_subentry(self, entry: _EntryStub, subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self._entry
        self.removed_subentries.append(subentry_id)
        return True


class _HassStub:
    """Minimal Home Assistant stub for async_unload_entry."""

    def __init__(
        self,
        entry: _EntryStub,
        runtime_data: integration.RuntimeData,
        entity_registry: _RegistryTracker,
        device_registry: _RegistryTracker,
    ) -> None:
        self.config_entries = _ConfigEntriesHelper(entry)
        self.data: dict[str, Any] = {
            DOMAIN: {
                "entries": {entry.entry_id: runtime_data},
                "fcm_lock": _AsyncLock(),
                "fcm_refcount": 1,
                "fcm_receiver": SimpleNamespace(async_stop=lambda: asyncio.sleep(0)),
            }
        }

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class _TokenCacheStub:
    """Token cache stub capturing close operations."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _CoordinatorStub:
    """Coordinator stub exposing async_shutdown hook."""

    def __init__(self) -> None:
        self.shutdown_called = False

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


class _EntryStub:
    """Config entry stub for unload tests."""

    def __init__(self) -> None:
        self.entry_id = "entry-unload"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.title = "Find My"
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data: integration.RuntimeData | None = None

    def add_subentry(self, key: str, device_ids: tuple[str, ...]) -> ConfigSubentry:
        subentry = ConfigSubentry(
            data=MappingProxyType({"group_key": key, "visible_device_ids": device_ids}),
            subentry_type="googlefindmy_feature_group",
            title=key.title(),
            unique_id=f"{self.entry_id}-{key}",
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry


def test_async_unload_entry_removes_subentries_and_registries(
    monkeypatch: Any,
) -> None:
    """Unload should drop subentries and clear registry assignments."""

    entry = _EntryStub()
    first = entry.add_subentry("core", ("dev-1", "dev-2"))
    second = entry.add_subentry("extra", ("dev-3",))

    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    entity_registry.apply(first.subentry_id, first.data["visible_device_ids"])
    entity_registry.apply(second.subentry_id, second.data["visible_device_ids"])
    device_registry.apply(first.subentry_id, first.data["visible_device_ids"])
    device_registry.apply(second.subentry_id, second.data["visible_device_ids"])

    token_cache = _TokenCacheStub()
    coordinator = _CoordinatorStub()
    subentry_manager = _SubentryManagerStub(entry, entity_registry, device_registry)
    runtime_data = integration.RuntimeData(
        coordinator=coordinator,
        token_cache=token_cache,
        subentry_manager=subentry_manager,
        fcm_receiver=None,
    )
    entry.runtime_data = runtime_data

    hass = _HassStub(entry, runtime_data, entity_registry, device_registry)

    async def _fake_release_fcm(hass_obj: Any) -> None:
        hass_obj.data[DOMAIN]["fcm_refcount"] = 0

    monkeypatch.setattr(integration, "_async_release_shared_fcm", _fake_release_fcm)
    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)
    monkeypatch.setattr(integration, "loc_unregister_fcm_provider", lambda: None)
    monkeypatch.setattr(integration, "api_unregister_fcm_provider", lambda: None)

    result = asyncio.run(integration.async_unload_entry(hass, entry))

    assert result is True
    assert coordinator.shutdown_called is True
    assert token_cache.closed is True
    assert subentry_manager.removed == [first.subentry_id, second.subentry_id]
    assert entity_registry.removals == [first.subentry_id, second.subentry_id]
    assert device_registry.removals == [first.subentry_id, second.subentry_id]
    assert not entry.subentries
    assert hass.config_entries.removed_subentries == []

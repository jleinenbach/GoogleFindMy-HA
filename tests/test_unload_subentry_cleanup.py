# tests/test_unload_subentry_cleanup.py
"""Tests verifying unload removes subentries and registry assignments."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry

import custom_components.googlefindmy as integration
from custom_components.googlefindmy.const import DOMAIN, SUBENTRY_TYPE_TRACKER
from tests.helpers.config_flow import ConfigEntriesDomainUniqueIdLookupMixin


def _platform_names(platforms: tuple[object, ...]) -> tuple[str, ...]:
    """Return normalized platform names for recorded calls."""

    names: list[str] = []
    for platform in platforms:
        if isinstance(platform, str):
            names.append(platform)
        else:
            value = getattr(platform, "value", None)
            if isinstance(value, str):
                names.append(value)
            else:
                names.append(str(platform))
    return tuple(names)


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


class _ConfigEntriesHelper(ConfigEntriesDomainUniqueIdLookupMixin):
    """Subset of hass.config_entries used during unload."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        self.removed_subentries: list[str] = []
        self.unloaded_subentries: list[str] = []
        self.setup_calls: list[str] = []
        self.forward_unload_calls: list[tuple[_EntryStub, tuple[object, ...]]] = []
        self.unload_platform_calls: list[tuple[_EntryStub, tuple[object, ...]]] = []
        self.forward_setup_calls: list[tuple[_EntryStub, tuple[object, ...]]] = []
        self.parent_unload_invocations = 0

    def async_entries(self, domain: str | None = None) -> list[_EntryStub]:
        if domain is not None and domain != DOMAIN:
            return []
        return [self._entry]

    async def async_unload(self, entry_id: str) -> bool:
        self.unloaded_subentries.append(entry_id)
        return True

    async def async_unload_platforms(
        self, entry: _EntryStub, platforms: Sequence[object]
    ) -> bool:
        assert entry is self._entry
        self.parent_unload_invocations += 1
        self.unload_platform_calls.append((entry, tuple(platforms)))
        return True

    async def async_forward_entry_unload(
        self,
        entry: _EntryStub,
        platforms: object,
    ) -> bool:
        assert entry is self._entry
        if isinstance(platforms, (list, tuple, set)):
            payload = tuple(platforms)
        else:
            payload = (platforms,)
        self.forward_unload_calls.append((entry, payload))
        return True

    async def async_forward_entry_setups(
        self,
        entry: _EntryStub,
        platforms: Sequence[object],
    ) -> None:
        assert entry is self._entry
        self.forward_setup_calls.append((entry, tuple(platforms)))

    def async_remove_subentry(self, entry: _EntryStub, subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self._entry
        self.removed_subentries.append(subentry_id)
        return True

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

    def async_get_subentries(self, entry_id: str) -> list[ConfigSubentry]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        return list(entry.subentries.values())

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
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
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title=key.title(),
            unique_id=f"{self.entry_id}-{key}",
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry


class _SubentryConfigEntriesHelper:
    """Config entries helper tracking subentry unload requests."""

    def __init__(self) -> None:
        self.unload_platform_calls: list[tuple[Any, tuple[object, ...]]] = []
        self.forward_unload_calls: list[tuple[Any, tuple[object, ...]]] = []

    async def async_unload_platforms(
        self, entry: Any, platforms: list[object]
    ) -> bool:  # noqa: FBT001 - Home Assistant signature
        self.unload_platform_calls.append((entry, tuple(platforms)))
        return True

    async def async_forward_entry_unload(
        self,
        entry: Any,
        platforms: object,
    ) -> bool:  # noqa: FBT001 - Home Assistant signature
        if isinstance(platforms, (list, tuple, set)):
            payload = tuple(platforms)
        else:
            payload = (platforms,)
        self.forward_unload_calls.append((entry, payload))
        return True


@pytest.mark.asyncio
async def test_async_unload_subentry_clears_runtime_data_and_preserves_parent_cache() -> None:
    """Subentry unload should clear runtime data without touching the parent cache."""

    runtime_data = integration.RuntimeData(
        coordinator=SimpleNamespace(),
        token_cache=SimpleNamespace(),
        subentry_manager=SimpleNamespace(),
        fcm_receiver=None,
    )

    parent_entry_id = "parent-entry"
    hass = SimpleNamespace(
        config_entries=_SubentryConfigEntriesHelper(),
        data={DOMAIN: {"entries": {parent_entry_id: runtime_data}}},
    )

    child_entry = SimpleNamespace(
        entry_id="child-entry",
        data={"group_key": "tracker"},
        runtime_data=runtime_data,
        parent_entry_id=parent_entry_id,
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    result = await integration._async_unload_subentry(  # type: ignore[arg-type]
        hass, child_entry
    )

    assert result is True
    assert child_entry.runtime_data is None
    entries_bucket = hass.data[DOMAIN]["entries"]
    assert entries_bucket == {parent_entry_id: runtime_data}
    assert hass.config_entries.unload_platform_calls
    recorded_entry, recorded_platforms = hass.config_entries.unload_platform_calls[0]
    assert recorded_entry == child_entry
    assert _platform_names(recorded_platforms) == tuple(
        platform.value if hasattr(platform, "value") else str(platform)
        for platform in integration.PLATFORMS
    )


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
    calls = hass.config_entries.forward_unload_calls
    aggregated: set[str] = set()
    for recorded_entry, platforms in calls:
        assert recorded_entry is entry
        aggregated.update(_platform_names(platforms))
    assert aggregated == set(integration.TRACKER_FEATURE_PLATFORMS)
    assert hass.config_entries.parent_unload_invocations == 1
    assert hass.config_entries.unload_platform_calls == [
        (entry, tuple(integration.PLATFORMS))
    ]
    assert hass.config_entries.removed_subentries == []


def test_async_unload_entry_handles_legacy_forward_signature(monkeypatch: Any) -> None:
    """Unload should fall back when Home Assistant lacks config_subentry_id support."""

    entry = _EntryStub()
    subentry = entry.add_subentry("legacy", ("dev-legacy",))

    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    entity_registry.apply(subentry.subentry_id, subentry.data["visible_device_ids"])
    device_registry.apply(subentry.subentry_id, subentry.data["visible_device_ids"])

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

    legacy_calls: list[tuple[_EntryStub, tuple[object, ...]]] = []

    def legacy_forward(entry_obj: _EntryStub, platforms: object) -> bool:
        if isinstance(platforms, (list, tuple, set)):
            payload = tuple(platforms)
        else:
            payload = (platforms,)
        legacy_calls.append((entry_obj, payload))
        return True

    hass.config_entries.async_forward_entry_unload = legacy_forward  # type: ignore[attr-defined]

    async def _fake_release_fcm(hass_obj: Any) -> None:
        hass_obj.data[DOMAIN]["fcm_refcount"] = 0

    monkeypatch.setattr(integration, "_async_release_shared_fcm", _fake_release_fcm)
    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)
    monkeypatch.setattr(integration, "loc_unregister_fcm_provider", lambda: None)
    monkeypatch.setattr(integration, "api_unregister_fcm_provider", lambda: None)

    result = asyncio.run(integration.async_unload_entry(hass, entry))

    assert result is True
    aggregated: set[str] = set()
    for _, platforms in legacy_calls:
        aggregated.update(_platform_names(platforms))
    assert aggregated == set(integration.TRACKER_FEATURE_PLATFORMS)
    assert hass.config_entries.parent_unload_invocations == 1
    assert hass.config_entries.unload_platform_calls == [
        (entry, tuple(integration.PLATFORMS))
    ]


def test_async_unload_entry_rolls_back_when_parent_unload_fails(
    monkeypatch: Any,
) -> None:
    """Parent platform unload failure should keep subentries online."""

    entry = _EntryStub()
    subentry = entry.add_subentry("core", ("dev-1", "dev-2"))

    entity_registry = _RegistryTracker()
    device_registry = _RegistryTracker()
    entity_registry.apply(subentry.subentry_id, subentry.data["visible_device_ids"])
    device_registry.apply(subentry.subentry_id, subentry.data["visible_device_ids"])

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

    async def _fail_parent_unload(
        entry_obj: _EntryStub, platforms: Sequence[object]
    ) -> bool:
        hass.config_entries.parent_unload_invocations += 1
        hass.config_entries.unload_platform_calls.append((entry_obj, tuple(platforms)))
        return False

    hass.config_entries.async_unload_platforms = _fail_parent_unload  # type: ignore[assignment]

    async def _fake_release_fcm(hass_obj: Any) -> None:
        hass_obj.data[DOMAIN]["fcm_refcount"] = 0

    monkeypatch.setattr(integration, "_async_release_shared_fcm", _fake_release_fcm)
    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)
    monkeypatch.setattr(integration, "loc_unregister_fcm_provider", lambda: None)
    monkeypatch.setattr(integration, "api_unregister_fcm_provider", lambda: None)

    result = asyncio.run(integration.async_unload_entry(hass, entry))

    assert result is False
    # Subentries and registries should remain untouched because the unload aborted.
    assert entry.subentries == {subentry.subentry_id: subentry}
    assert hass.config_entries.forward_unload_calls == []
    assert hass.config_entries.removed_subentries == []
    assert hass.config_entries.parent_unload_invocations == 1
    assert hass.config_entries.unload_platform_calls == [
        (entry, tuple(integration.PLATFORMS))
    ]
    # Parent unload failures must not trigger manual subentry forwarding; Home
    # Assistant will re-run subentry setup as needed.
    assert hass.config_entries.forward_setup_calls == []
    # Runtime data is reattached to the bucket so the entry keeps running.
    assert hass.data[DOMAIN]["entries"][entry.entry_id] is runtime_data
    assert entry.runtime_data is runtime_data
    # Coordinator and token cache must not shut down on abort.
    assert coordinator.shutdown_called is False
    assert token_cache.closed is False

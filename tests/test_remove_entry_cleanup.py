# tests/test_remove_entry_cleanup.py

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import DOMAIN
from custom_components.googlefindmy.const import OPT_DELETE_CACHES_ON_REMOVE


class _TokenCacheStub:
    """Capture close/remove calls during entry removal."""

    def __init__(self) -> None:
        self.closed = False
        self.store_removed = False

    async def close(self) -> None:
        self.closed = True

    async def async_remove_store(self) -> None:
        self.store_removed = True


class _CoordinatorStub:
    """Minimal coordinator stub tracking shutdown."""

    def __init__(self) -> None:
        self.shutdown_called = False

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


class _GoogleHomeFilterStub:
    """Stubbed Google Home filter capturing shutdown invocations."""

    def __init__(self) -> None:
        self.shutdown_called = False

    def async_shutdown(self) -> None:
        self.shutdown_called = True


class _EntryStub:
    """Config entry stub used in removal tests."""

    def __init__(self) -> None:
        self.entry_id = "entry-remove"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.title = "Find My Entry"
        self.runtime_data: Any | None = None


class _HassStub:
    """Home Assistant stub exposing the data layout used by async_remove_entry."""

    def __init__(self, entry: _EntryStub, runtime_data: integration.RuntimeData) -> None:
        self.data: dict[str, Any] = {
            DOMAIN: {
                "entries": {entry.entry_id: runtime_data},
                "device_owner_index": {"device-1": entry.entry_id},
            }
        }
        self.config_entries = SimpleNamespace(async_entries=lambda _domain=None: [])


@pytest.fixture
def _no_fcm_release(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Intercept shared FCM release calls and record invocations."""

    calls: list[None] = []

    async def _fake_release(hass: Any, entry: Any | None = None) -> None:  # noqa: ANN401 - test stub
        calls.append(None)

    monkeypatch.setattr(integration, "_async_release_shared_fcm", _fake_release)
    return calls


def _setup_runtime(entry: _EntryStub) -> tuple[_CoordinatorStub, _TokenCacheStub, _GoogleHomeFilterStub, integration.RuntimeData]:
    """Create runtime objects associated with the entry stub."""

    coordinator = _CoordinatorStub()
    token_cache = _TokenCacheStub()
    google_home_filter = _GoogleHomeFilterStub()
    runtime_data = integration.RuntimeData(
        coordinator=coordinator,
        token_cache=token_cache,
        subentry_manager=SimpleNamespace(),
        fcm_receiver=None,
        google_home_filter=google_home_filter,
    )
    entry.runtime_data = runtime_data
    return coordinator, token_cache, google_home_filter, runtime_data


def test_async_remove_entry_purges_store_and_creates_issue(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """Removing an entry should purge the cache and create an informational issue."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    entry = _EntryStub()
    coordinator, token_cache, google_home_filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert token_cache.closed is True
    assert token_cache.store_removed is True
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert "device-1" not in hass.data[DOMAIN]["device_owner_index"]
    assert entry.runtime_data is None
    assert _no_fcm_release  # FCM release should have been attempted

    issue_id = f"cache_purged_{entry.entry_id}"
    assert any(item["issue_id"] == issue_id for item in issue_registry_capture.created)
    assert issue_registry_capture.deleted == []


def test_async_remove_entry_respects_retention_option(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """When cache deletion is disabled, the store should be preserved and no issue created."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    entry = _EntryStub()
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    coordinator, token_cache, google_home_filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    issue_id = f"cache_purged_{entry.entry_id}"
    issue_registry_capture.registry.issues[issue_id] = {
        "issue_id": issue_id,
        "domain": DOMAIN,
    }

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert token_cache.store_removed is False
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert entry.runtime_data is None
    assert _no_fcm_release

    assert all(item["issue_id"] != issue_id for item in issue_registry_capture.created)
    assert (DOMAIN, issue_id) in issue_registry_capture.deleted


def test_async_remove_entry_fallback_store_remove(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """Fallback store removal should run when no token cache is present."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    store_args: list[tuple[Any, Any, str]] = []
    store_removals: list[str] = []

    class _StoreStub:
        def __init__(self, hass_obj: Any, version: Any, key: str) -> None:
            store_args.append((hass_obj, version, key))
            self._hass = hass_obj
            self._key = key

        async def async_remove(self) -> None:
            store_removals.append(self._key)

    monkeypatch.setattr(integration, "Store", _StoreStub)

    entry = _EntryStub()
    coordinator = _CoordinatorStub()
    google_home_filter = _GoogleHomeFilterStub()
    runtime_data = integration.RuntimeData(
        coordinator=coordinator,
        token_cache=None,
        subentry_manager=SimpleNamespace(),
        fcm_receiver=None,
        google_home_filter=google_home_filter,
    )
    entry.runtime_data = runtime_data
    hass = _HassStub(entry, runtime_data)

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert entry.runtime_data is None
    assert store_args == [
        (hass, integration.STORAGE_VERSION, f"{integration.STORAGE_KEY}_{entry.entry_id}")
    ]
    assert store_removals == [f"{integration.STORAGE_KEY}_{entry.entry_id}"]
    assert _no_fcm_release

    issue_id = f"cache_purged_{entry.entry_id}"
    assert any(item["issue_id"] == issue_id for item in issue_registry_capture.created)

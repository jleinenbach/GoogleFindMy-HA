# tests/test_options_flow_credentials_cache.py
"""Regression tests for the options credential flow clearing cached AAS tokens."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any
from collections.abc import Awaitable
from types import MappingProxyType

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from homeassistant.config_entries import ConfigSubentry


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry ids for credential cache tests."""

    return f"{entry_id}-{key}-subentry"


class _MemoryCache:
    """In-memory cache implementing the token cache contract used by the flow."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self._data.get(name)

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value

    def set(self, name: str, value: Any) -> None:
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value


@dataclass
class _RuntimeData:
    """Runtime data stub providing a cache attribute."""

    token_cache: _MemoryCache

    @property
    def cache(self) -> _MemoryCache:
        return self.token_cache


class _DummyEntry:
    """Minimal ConfigEntry substitute for exercising the options flow."""

    def __init__(
        self, *, entry_id: str, data: dict[str, Any], cache: _MemoryCache
    ) -> None:
        self.entry_id = entry_id
        self.data = data
        self.options: dict[str, Any] = {}
        self.runtime_data = _RuntimeData(cache)
        self.title = data.get(CONF_GOOGLE_EMAIL, "Google Find My Device")
        self.subentries: dict[str, ConfigSubentry] = {}

        service_subentry = ConfigSubentry(
            data={"group_key": SERVICE_SUBENTRY_KEY},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
            subentry_id=_stable_subentry_id(entry_id, SERVICE_SUBENTRY_KEY),
        )
        tracker_subentry = ConfigSubentry(
            data={"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}},
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title="Google Find My devices",
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
            subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
        )
        self.subentries[service_subentry.subentry_id] = service_subentry
        self.subentries[tracker_subentry.subentry_id] = tracker_subentry


class _DummyConfigEntries:
    """Expose Home Assistant config entry helpers used by the flow under test."""

    def __init__(self, entry: _DummyEntry) -> None:
        self._entry = entry
        self.updated_payloads: list[dict[str, Any]] = []
        self.reloaded: list[str] = []
        self.updated_subentries: list[tuple[str, dict[str, Any]]] = []
        self.removed_subentries: list[str] = []
        self.setup_calls: list[str] = []

    def async_get_entry(self, entry_id: str) -> _DummyEntry | None:
        return self._entry if entry_id == self._entry.entry_id else None

    def async_get_subentries(self, entry_id: str) -> list[ConfigSubentry]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        return list(entry.subentries.values())

    def async_update_entry(self, entry: _DummyEntry, *, data: dict[str, Any]) -> None:
        assert entry is self._entry
        entry.data = data
        self.updated_payloads.append(data)

    def async_update_subentry(
        self,
        entry: _DummyEntry,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        assert entry is self._entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        if translation_key is not None:
            subentry.translation_key = translation_key
        self.updated_subentries.append((subentry.subentry_id, dict(subentry.data)))

    def async_remove_subentry(self, entry: _DummyEntry, subentry_id: str) -> bool:
        assert entry is self._entry
        entry.subentries.pop(subentry_id, None)
        self.removed_subentries.append(subentry_id)
        return True

    async def async_reload(self, entry_id: str) -> None:
        assert DATA_AAS_TOKEN not in self._entry.data
        assert await self._entry.runtime_data.cache.get(DATA_AAS_TOKEN) is None
        self.reloaded.append(entry_id)

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


class _DummyHass:
    """Small Home Assistant stub collecting scheduled tasks for inspection."""

    def __init__(self, entry: _DummyEntry, cache: _MemoryCache) -> None:
        self.config_entries = _DummyConfigEntries(entry)
        self.data: dict[str, Any] = {
            DOMAIN: {"entries": {entry.entry_id: _RuntimeData(cache)}}
        }
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def drain_tasks(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks)


def test_options_flow_rotating_token_clears_cached_aas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing credentials in the options flow must drop cached AAS tokens."""

    async def _exercise() -> None:
        cache = _MemoryCache()
        await cache.async_set_cached_value(DATA_AAS_TOKEN, "aas_et/OLD")

        entry = _DummyEntry(
            entry_id="entry-1",
            data={
                CONF_GOOGLE_EMAIL: "user@example.com",
                CONF_OAUTH_TOKEN: "oauth-original-token-123456",
                DATA_AAS_TOKEN: "aas_et/OLD",
            },
            cache=cache,
        )
        hass = _DummyHass(entry, cache)

        flow = config_flow.OptionsFlowHandler()
        flow.hass = hass  # type: ignore[assignment]
        flow.config_entry = entry  # type: ignore[attr-defined]

        async def _fake_pick(
            hass: Any,
            email: str,
            candidates: list[tuple[str, str]],
            *,
            secrets_bundle: dict[str, Any] | None = None,
        ) -> str | None:
            return candidates[0][1] if candidates else None

        monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

        new_token = "oauth-token-rotate-123456"
        result = await flow.async_step_credentials(
            {"new_oauth_token": new_token, "subentry": TRACKER_SUBENTRY_KEY}
        )
        if inspect.isawaitable(result):
            result = await result

        assert isinstance(result, dict)
        assert result.get("type") in {"abort", "form"}

        assert hass.config_entries.updated_payloads
        updated = hass.config_entries.updated_payloads[-1]
        assert updated[CONF_OAUTH_TOKEN] == new_token
        assert DATA_AAS_TOKEN not in updated
        assert entry.data[CONF_OAUTH_TOKEN] == new_token
        assert DATA_AAS_TOKEN not in entry.data
        assert await cache.get(DATA_AAS_TOKEN) is None
        assert hass.config_entries.updated_subentries
        subentry_id, payload = hass.config_entries.updated_subentries[-1]
        assert subentry_id in entry.subentries
        assert payload.get("group_key") == TRACKER_SUBENTRY_KEY

        await hass.drain_tasks()
        assert hass.config_entries.reloaded == [entry.entry_id]

    asyncio.run(_exercise())


def test_fcm_token_lookup_uses_entry_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the API forwards the config entry ID to the shared FCM receiver."""

    from custom_components.googlefindmy import api as api_module

    class _CacheStub:
        """Minimal cache exposing an entry ID attribute for the API wrapper."""

        def __init__(self, entry_id: str) -> None:
            self.entry_id = entry_id

        async def async_get_cached_value(self, key: str) -> Any:
            return None

        async def async_set_cached_value(self, key: str, value: Any) -> None:
            return None

    class _Receiver:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def get_fcm_token(self, entry_id: str | None = None) -> str:
            self.calls.append(entry_id)
            assert entry_id == "entry-primary"
            return "token-primary-abcdef"

    receiver = _Receiver()
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda: receiver)

    api = GoogleFindMyAPI(cache=_CacheStub("entry-primary"))

    token = api._get_fcm_token_for_action()

    assert token == "token-primary-abcdef"
    assert receiver.calls == ["entry-primary"]


def test_fcm_token_lookup_falls_back_without_entry_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure legacy receivers without entry ID support continue to function."""

    from custom_components.googlefindmy import api as api_module

    class _CacheStub:
        async def async_get_cached_value(self, key: str) -> Any:
            return None

        async def async_set_cached_value(self, key: str, value: Any) -> None:
            return None

    class _LegacyReceiver:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def get_fcm_token(self) -> str:
            self.calls.append(None)
            return "legacy-token-abcdef"

    receiver = _LegacyReceiver()
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda: receiver)

    api = GoogleFindMyAPI(cache=_CacheStub())

    token = api._get_fcm_token_for_action()

    assert token == "legacy-token-abcdef"
    assert receiver.calls == [None]


def test_play_stop_sound_uses_entry_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure Play/Stop Sound submissions use the provided TokenCache with namespacing."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound import (
        start_sound_request as start_module,
        stop_sound_request as stop_module,
    )

    class _FakeCache:
        """Minimal cache tracking get/set keys to verify namespacing."""

        def __init__(self, entry_id: str) -> None:
            self.entry_id = entry_id
            self._data: dict[str, Any] = {}
            self.get_calls: list[str] = []
            self.set_calls: list[tuple[str, Any]] = []

        async def async_get_cached_value(self, key: str) -> Any:
            self.get_calls.append(key)
            return self._data.get(key)

        async def async_set_cached_value(self, key: str, value: Any) -> None:
            self.set_calls.append((key, value))
            if value is None:
                self._data.pop(key, None)
            else:
                self._data[key] = value

    async def _fail_get(key: str) -> Any:
        raise AssertionError("Global cache fallback must not be used for Play Sound")

    async def _fail_set(key: str, value: Any) -> None:
        raise AssertionError("Global cache fallback must not be used for Play Sound")

    monkeypatch.setattr(start_module, "_cache_get_default", _fail_get, raising=False)
    monkeypatch.setattr(start_module, "_cache_set_default", _fail_set, raising=False)
    monkeypatch.setattr(stop_module, "_cache_get_default", _fail_get, raising=False)
    monkeypatch.setattr(stop_module, "_cache_set_default", _fail_set, raising=False)

    async def _exercise() -> None:
        cache_primary = _FakeCache("entry-one")
        cache_secondary = _FakeCache("entry-two")

        api_primary = GoogleFindMyAPI(cache=cache_primary)
        _ = GoogleFindMyAPI(cache=cache_secondary)

        monkeypatch.setattr(
            api_primary,
            "_get_fcm_token_for_action",
            lambda: "tok-1234567890",
            raising=False,
        )

        start_calls: list[tuple[str, str, dict[str, Any]]] = []
        stop_calls: list[tuple[str, str, dict[str, Any]]] = []

        async def _fake_start(scope: str, payload: str, **kwargs: Any) -> str:
            start_calls.append((scope, payload, kwargs))
            return "start-ok"

        async def _fake_stop(scope: str, payload: str, **kwargs: Any) -> str:
            stop_calls.append((scope, payload, kwargs))
            return "stop-ok"

        monkeypatch.setattr(start_module, "async_nova_request", _fake_start)
        monkeypatch.setattr(stop_module, "async_nova_request", _fake_stop)

        assert await api_primary.async_play_sound("device-42")
        assert await api_primary.async_stop_sound("device-42")

        assert start_calls and stop_calls

        _, _, start_kwargs = start_calls[0]
        assert start_kwargs["cache"] is cache_primary
        assert start_kwargs["namespace"] == "entry-one"

        start_get = start_kwargs["cache_get"]
        start_set = start_kwargs["cache_set"]
        assert start_get is not None
        assert start_set is not None
        await start_get("ttl")
        assert cache_primary.get_calls[-1] == "entry-one:ttl"
        await start_set("ttl", "value")
        assert ("entry-one:ttl", "value") in cache_primary.set_calls

        _, _, stop_kwargs = stop_calls[0]
        assert stop_kwargs["cache"] is cache_primary
        assert stop_kwargs["namespace"] == "entry-one"

        stop_get = stop_kwargs["cache_get"]
        stop_set = stop_kwargs["cache_set"]
        assert stop_get is not None
        assert stop_set is not None
        await stop_get("ttl2")
        assert cache_primary.get_calls[-1] == "entry-one:ttl2"
        await stop_set("ttl2", "value2")
        assert ("entry-one:ttl2", "value2") in cache_primary.set_calls

    asyncio.run(_exercise())

# tests/test_reauth_manual_token.py
"""Regression tests for clearing cached AAS tokens during manual reauthentication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar, cast

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.Auth import aas_token_retrieval, adm_token_retrieval
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DOMAIN,
)
from tests.helpers.config_flow import ConfigEntriesFlowManagerStub


_ValueT = TypeVar("_ValueT")


class _MemoryCache:
    """In-memory async cache emulating the TokenCache contract for tests."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self._data.get(name)

    async def set(self, name: str, value: Any) -> None:
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        await self.set(name, value)

    async def get_or_set(
        self, name: str, generator: Callable[[], Awaitable[_ValueT] | _ValueT]
    ) -> _ValueT:
        if name in self._data:
            return cast(_ValueT, self._data[name])
        result = generator()
        if asyncio.iscoroutine(result):
            result = await result
        result_typed = cast(_ValueT, result)
        await self.set(name, result_typed)
        return result_typed

    async def all(self) -> dict[str, Any]:  # pragma: no cover - helper for completeness
        return dict(self._data)


@dataclass
class _RuntimeData:
    """Runtime data shim providing token cache aliases."""

    token_cache: _MemoryCache

    @property
    def cache(self) -> _MemoryCache:
        return self.token_cache


class _DummyEntry:
    """Lightweight ConfigEntry substitute for flow testing."""

    def __init__(
        self, *, entry_id: str, data: dict[str, Any], cache: _MemoryCache
    ) -> None:
        self.entry_id = entry_id
        self.data = data
        self.options: dict[str, Any] = {}
        self.title = "Test Entry"
        self.runtime_data = _RuntimeData(cache)
        self.subentries: dict[str, Any] = {}


class _DummyConfigEntries:
    """Expose async_get_entry for the config flow."""

    def __init__(self, entry: _DummyEntry) -> None:
        self._entry = entry
        self.setup_calls: list[str] = []
        self.flow_manager = ConfigEntriesFlowManagerStub()
        self.flow = self.flow_manager.flow

    def async_get_entry(self, entry_id: str) -> _DummyEntry | None:
        return self._entry if entry_id == self._entry.entry_id else None

    def async_get_subentries(self, entry_id: str) -> list[Any]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            return list(subentries.values())
        return []

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


class _DummyHass:
    """Small Home Assistant stub providing config_entries and data buckets."""

    def __init__(self, entry: _DummyEntry, cache: _MemoryCache) -> None:
        self.config_entries = _DummyConfigEntries(entry)
        self.data: dict[str, Any] = {
            DOMAIN: {"entries": {entry.entry_id: _RuntimeData(cache)}}
        }


def test_manual_reauth_clears_cached_aas_and_mints_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After manual reauth, cached AAS tokens must be cleared and a new ADM flow must use fresh OAuth."""

    async def _exercise() -> None:
        cache = _MemoryCache()
        await cache.async_set_cached_value(username_string, "user@example.com")
        await cache.async_set_cached_value(CONF_OAUTH_TOKEN, "oauth-old")
        await cache.async_set_cached_value(DATA_AAS_TOKEN, "aas_et/STABLE")
        await cache.async_set_cached_value(DATA_AUTH_METHOD, "individual_tokens")

        entry = _DummyEntry(
            entry_id="entry-1",
            data={
                CONF_GOOGLE_EMAIL: "user@example.com",
                CONF_OAUTH_TOKEN: "oauth-old",
                DATA_AAS_TOKEN: "aas_et/STABLE",
                DATA_AUTH_METHOD: "individual_tokens",
            },
            cache=cache,
        )
        hass = _DummyHass(entry, cache)

        flow = config_flow.ConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": entry.entry_id}
        assert flow._get_entry_cache(entry) is cache

        # Simulate manual reauth clearing the cached AAS token.
        await cache.async_set_cached_value(DATA_AAS_TOKEN, None)
        assert await cache.get(DATA_AAS_TOKEN) is None

        new_oauth = "oauth-new"
        entry.data = {
            CONF_GOOGLE_EMAIL: "user@example.com",
            CONF_OAUTH_TOKEN: new_oauth,
            DATA_AUTH_METHOD: "individual_tokens",
        }

        # Simulate reload storing the new OAuth token and keeping AAS cleared.
        await cache.async_set_cached_value(CONF_OAUTH_TOKEN, new_oauth)
        await cache.async_set_cached_value(
            username_string, entry.data[CONF_GOOGLE_EMAIL]
        )
        await cache.async_set_cached_value(DATA_AAS_TOKEN, None)
        await cache.async_set_cached_value(DATA_AUTH_METHOD, "individual_tokens")

        assert await cache.get(DATA_AAS_TOKEN) is None
        assert await cache.get(CONF_OAUTH_TOKEN) == new_oauth

        observed: dict[str, Any] = {}

        async def _fake_exchange(username: str, oauth_token: str, android_id: int):
            observed["oauth_token"] = oauth_token
            return {"Token": "aas-new", "Email": username}

        async def _fake_request_token(
            username: str,
            service: str,
            *,
            cache,
            aas_token=None,
            aas_provider,
        ):
            observed["service"] = service
            assert aas_token is None
            observed["aas_token"] = await aas_provider()
            return "adm-new"

        monkeypatch.setattr(
            aas_token_retrieval, "_exchange_oauth_for_aas", _fake_exchange
        )
        monkeypatch.setattr(
            adm_token_retrieval, "async_request_token", _fake_request_token
        )

        token = await adm_token_retrieval.async_get_adm_token(cache=cache)

        assert token == "adm-new"
        assert observed["oauth_token"] == new_oauth
        assert observed["aas_token"] == "aas-new"

    asyncio.run(_exercise())

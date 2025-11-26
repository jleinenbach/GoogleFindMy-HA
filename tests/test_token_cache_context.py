"""TokenCache context provider priority tests."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.googlefindmy.Auth import token_cache
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.NovaApi import nova_request


class _FakeHass:
    """Minimal async Home Assistant stub for TokenCache interactions."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def async_add_executor_job(self, func, *args, **kwargs):
        return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_context_provider_overrides_default_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache provider should take precedence over the global default instance."""

    hass = _FakeHass()
    provider_cache = await TokenCache.create(hass, "entry-provider")
    default_cache = await TokenCache.create(hass, "entry-default")

    monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
    monkeypatch.setattr(
        token_cache,
        "_STATE",
        {"legacy_migration_done": False, "default_entry_id": None},
        raising=False,
    )

    token_cache._register_instance("entry-provider", provider_cache)
    token_cache._register_instance("entry-default", default_cache)
    token_cache._set_default_entry_id("entry-default")

    await provider_cache.set("key", "from-provider")
    await default_cache.set("key", "from-default")

    monkeypatch.setattr(
        nova_request,
        "_STATE",
        {"hass": None, "async_refresh_lock": None, "cache_provider": None},
        raising=False,
    )
    nova_request.register_cache_provider(lambda: provider_cache)
    try:
        cached_value = await token_cache.async_get_cached_value("key")
    finally:
        nova_request.unregister_cache_provider()

    assert cached_value == "from-provider"

    await provider_cache.close()
    await default_cache.close()

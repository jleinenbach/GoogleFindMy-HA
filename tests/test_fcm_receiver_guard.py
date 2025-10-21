# tests/test_fcm_receiver_guard.py
"""Regression tests for the shared FCM receiver guard."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Callable, Optional

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA


class _StubReceiver:
    """Stub FCM receiver lacking async registration methods."""

    def __init__(self) -> None:
        self.stop_calls = 0

    async def async_stop(self) -> None:
        """Record stop invocations for verification."""

        self.stop_calls += 1


def test_async_acquire_discards_invalid_cached_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached receiver without async registration methods is replaced."""

    hass = SimpleNamespace(data={DOMAIN: {}})
    stub = _StubReceiver()
    hass.data[DOMAIN]["fcm_receiver"] = stub

    loader_module = ModuleType("homeassistant.loader")
    loader_module.async_get_integration = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader_module)

    module = importlib.import_module("custom_components.googlefindmy.__init__")
    async_acquire_shared_fcm = module._async_acquire_shared_fcm

    recorded_getters: dict[str, Callable[[], object]] = {}

    def capture_loc(getter: Callable[[], object]) -> None:
        recorded_getters["loc"] = getter

    def capture_api(getter: Callable[[], object]) -> None:
        recorded_getters["api"] = getter

    monkeypatch.setattr(
        module,
        "loc_register_fcm_provider",
        capture_loc,
    )
    monkeypatch.setattr(
        module,
        "api_register_fcm_provider",
        capture_api,
    )

    async def _run() -> FcmReceiverHA:
        return await async_acquire_shared_fcm(hass)

    new_receiver = asyncio.run(_run())

    assert isinstance(new_receiver, FcmReceiverHA)
    assert hass.data[DOMAIN]["fcm_receiver"] is new_receiver
    assert stub.stop_calls == 1
    assert "loc" in recorded_getters
    assert "api" in recorded_getters
    assert recorded_getters["loc"]() is new_receiver
    assert recorded_getters["api"]() is new_receiver
    assert hass.data[DOMAIN]["fcm_refcount"] == 1


def test_multi_entry_buffers_prevent_global_cache_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure two entries never fall back to global cache helpers."""

    async def _boom(*_args, **_kwargs) -> None:
        raise AssertionError("Global cache helper must not be used")

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.token_cache.async_get_cached_value",
        _boom,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.token_cache.async_set_cached_value",
        _boom,
    )

    receiver = FcmReceiverHA()

    class DummyCache:
        """Simple async cache stub bound to a single entry."""

        def __init__(self, entry_id: str) -> None:
            self.entry_id = entry_id
            self.data: dict[str, object] = {}

        async def get(self, key: str) -> object | None:
            return self.data.get(key)

        async def set(self, key: str, value: object | None) -> None:
            self.data[key] = value

    class DummyEntry:
        def __init__(self, entry_id: str) -> None:
            self.entry_id = entry_id
            self.options: dict[str, object] = {}

    class DummyCoordinator:
        def __init__(self, entry_id: str) -> None:
            self.config_entry = DummyEntry(entry_id)
            self.cache = DummyCache(entry_id)

    start_calls: list[str] = []

    async def fake_start(self, entry_id: str, _cache: Optional[DummyCache]) -> None:
        start_calls.append(entry_id)

    monkeypatch.setattr(FcmReceiverHA, "_start_supervisor_for_entry", fake_start)

    coord_one = DummyCoordinator("entry-1")
    coord_two = DummyCoordinator("entry-2")

    creds_one = {"fcm": {"registration": {"token": "token-entry-1"}}}
    creds_two = {"fcm": {"registration": {"token": "token-entry-2"}}}

    async def _exercise() -> None:
        await receiver.async_initialize()

        receiver._on_credentials_updated_for_entry("entry-1", creds_one)
        receiver._on_credentials_updated_for_entry("entry-2", creds_two)
        await asyncio.sleep(0)

        receiver.register_coordinator(coord_one)
        receiver.register_coordinator(coord_two)

        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_exercise())

    cache_one = coord_one.cache.data
    cache_two = coord_two.cache.data

    assert cache_one["fcm_credentials"] == creds_one
    assert cache_two["fcm_credentials"] == creds_two
    assert cache_one["fcm_routing_tokens"] == ["token-entry-1"]
    assert cache_two["fcm_routing_tokens"] == ["token-entry-2"]

    assert receiver._pending_creds == {}
    assert receiver._pending_routing_tokens == {}
    assert receiver.get_fcm_token("entry-1") == "token-entry-1"
    assert receiver.get_fcm_token("entry-2") == "token-entry-2"

    assert start_calls.count("entry-1") == 1
    assert start_calls.count("entry-2") == 1

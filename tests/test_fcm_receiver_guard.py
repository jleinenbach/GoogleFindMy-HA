"""Tests for the shared FCM receiver guard logic."""

# tests/test_fcm_receiver_guard.py
from __future__ import annotations

import asyncio
import base64
import importlib
import sys
from contextlib import suppress
from types import ModuleType, SimpleNamespace
from collections.abc import Callable
from typing import Any, Awaitable, Coroutine, TypeVar, cast

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
    FcmReceiverHA,
    _call_in_executor,
)
from custom_components.googlefindmy.Auth.token_cache import TokenCache

_T = TypeVar("_T")


class _StubReceiver:
    """Stub FCM receiver lacking async registration methods."""

    def __init__(self) -> None:
        self.stop_calls = 0

    async def async_stop(self) -> None:
        """Record stop invocations for verification."""

        self.stop_calls += 1


def test_async_acquire_discards_invalid_cached_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached receiver without async registration methods is replaced."""

    hass = SimpleNamespace(data={DOMAIN: {}})
    stub = _StubReceiver()
    hass.data[DOMAIN]["fcm_receiver"] = stub

    async def _async_get_integration(*_args: object, **_kwargs: object) -> None:
        return None

    loader_module = ModuleType("homeassistant.loader")
    setattr(loader_module, "async_get_integration", _async_get_integration)
    monkeypatch.setitem(sys.modules, "homeassistant.loader", loader_module)

    module = importlib.import_module("custom_components.googlefindmy")
    async_acquire_shared_fcm = cast(
        Callable[[object], Awaitable[FcmReceiverHA]],
        getattr(module, "_async_acquire_shared_fcm"),
    )

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


def test_call_in_executor_without_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback path without a running loop executes synchronously."""

    monkeypatch.setattr(asyncio, "to_thread", None, raising=False)

    def _raise_runtime_error() -> asyncio.AbstractEventLoop:
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(asyncio, "get_running_loop", _raise_runtime_error)

    calls: list[int] = []

    def _work(value: int) -> int:
        calls.append(value)
        return value + 1

    async def _runner() -> int:
        return await _call_in_executor(_work, 41)

    result = asyncio.run(_runner())

    assert result == 42
    assert calls == [41]


def test_multi_entry_buffers_prevent_global_cache_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure two entries never fall back to global cache helpers."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
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

    async def fake_start(
        self: FcmReceiverHA, entry_id: str, _cache: DummyCache | None
    ) -> None:
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


def test_unregister_prunes_token_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing a coordinator clears its tokens and blocks future fan-out."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        receiver = FcmReceiverHA()

        async def fake_start(
            _self: FcmReceiverHA, _entry_id: str, _cache: object | None
        ) -> None:
            return None

        monkeypatch.setattr(FcmReceiverHA, "_start_supervisor_for_entry", fake_start)

        def create_task(
            coro: Coroutine[Any, Any, _T], *, name: str | None = None
        ) -> asyncio.Task[_T]:
            return loop.create_task(coro, name=name)

        monkeypatch.setattr(asyncio, "create_task", create_task)

        class DummyCache:
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
                self.google_home_filter = None

            def is_ignored(self, _device_id: str) -> bool:
                return False

        loop.run_until_complete(receiver.async_initialize())

        monkeypatch.setattr(
            receiver,
            "_extract_canonic_id_from_response",
            lambda _hex: "device-xyz",
        )

        seen_routes: list[tuple[str, set[str] | None]] = []

        async def capture_process(
            entry_id: str,
            canonic_id: str,
            _hex: str,
            target_entries: set[str] | None,
        ) -> None:
            seen_routes.append(
                (entry_id, set(target_entries) if target_entries else None)
            )

        monkeypatch.setattr(receiver, "_process_background_update", capture_process)

        coord_one = DummyCoordinator("entry-one")
        coord_two = DummyCoordinator("entry-two")

        creds_one = {"fcm": {"registration": {"token": "token-one"}}}
        creds_two = {"fcm": {"registration": {"token": "token-two"}}}

        receiver._on_credentials_updated_for_entry("entry-one", creds_one)
        receiver._on_credentials_updated_for_entry("entry-two", creds_two)

        loop.run_until_complete(asyncio.sleep(0))

        receiver.register_coordinator(coord_one)
        receiver.register_coordinator(coord_two)

        loop.run_until_complete(asyncio.sleep(0))

        payload = base64.b64encode(b"payload").decode()
        envelope = {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": payload}}

        receiver._on_notification("entry-one", envelope, None, None)
        loop.run_until_complete(asyncio.sleep(0))

        assert seen_routes

        seen_routes.clear()

        receiver.unregister_coordinator(coord_one)

        receiver._on_notification("entry-one", envelope, None, None)
        loop.run_until_complete(asyncio.sleep(0))

        assert seen_routes == []
        assert "token-one" not in receiver._token_to_entries
        assert "entry-one" not in receiver._entry_to_tokens
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)

        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


def test_receiver_reuses_hass_managed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receiver provides Home Assistant's shared session to FcmPushClient."""

    hass = SimpleNamespace()
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    sentinel_session = object()
    recorded: dict[str, object] = {}

    def fake_async_get_clientsession(hass_arg: object) -> object:
        recorded["hass"] = hass_arg
        return sentinel_session

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.fcm_receiver_ha.async_get_clientsession",
        fake_async_get_clientsession,
    )

    class DummyPushClient:
        def __init__(
            self,
            _callback: Callable[..., Awaitable[object]],
            _config: object,
            _creds: object,
            _creds_cb: Callable[..., Awaitable[object]],
            *,
            config: object | None = None,
            http_client_session: object | None = None,
        ) -> None:
            recorded["session"] = http_client_session
            recorded["config"] = config
            self.run_state = None
            self.do_listen = False

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.fcm_receiver_ha.FcmPushClient",
        DummyPushClient,
    )

    async def _exercise() -> DummyPushClient | None:
        await receiver.async_initialize()
        client = await receiver._ensure_client_for_entry("entry-id", None)
        return cast(DummyPushClient | None, client)

    created = asyncio.run(_exercise())

    assert isinstance(created, DummyPushClient)
    assert recorded["hass"] is hass
    assert recorded["session"] is sentinel_session


def test_register_coordinator_exposes_cache_and_tracks_push_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering a coordinator wires its cache and counts background updates."""

    receiver = FcmReceiverHA()

    class DummyCache:
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
            self._cache = DummyCache(entry_id)
            self.performance_metrics: dict[str, int] = {}
            self.updated: list[tuple[str, dict[str, object]]] = []
            self.push_calls: list[list[str]] = []
            self.google_home_filter = None

        @property
        def cache(self) -> TokenCache:
            return cast(TokenCache, self._cache)

        def update_device_cache(
            self, device_id: str, location_data: dict[str, object]
        ) -> None:
            self.updated.append((device_id, dict(location_data)))
            self.increment_stat("background_updates")

        def increment_stat(self, name: str) -> None:
            self.performance_metrics[name] = self.performance_metrics.get(name, 0) + 1

        def push_updated(self, device_ids: list[str]) -> None:
            self.push_calls.append(list(device_ids))

        def is_ignored(self, _device_id: str) -> bool:
            return False

    started: list[tuple[str, TokenCache | None]] = []

    async def fake_start(
        self: FcmReceiverHA, entry_id: str, cache: TokenCache | None
    ) -> None:  # pragma: no cover - simple proxy
        started.append((entry_id, cache))

    monkeypatch.setattr(FcmReceiverHA, "_start_supervisor_for_entry", fake_start)

    coord = DummyCoordinator("entry-test")

    async def _exercise() -> None:
        await receiver.async_initialize()
        receiver.register_coordinator(coord)
        await asyncio.sleep(0)
        key = ("entry-test", "device-1")
        receiver._pending[key] = {
            "latitude": 1.0,
            "longitude": 2.0,
            "last_updated": 1234.0,
        }
        receiver._pending_targets[key] = {"entry-test"}
        await receiver._flush(key)
        await asyncio.sleep(0)

    asyncio.run(_exercise())

    assert receiver._entry_caches["entry-test"] is coord.cache
    assert started == [("entry-test", coord.cache)]
    assert coord.performance_metrics["background_updates"] == 1
    assert coord.updated == [
        ("device-1", {"latitude": 1.0, "longitude": 2.0, "last_updated": 1234.0})
    ]
    assert coord.push_calls == [["device-1"]]

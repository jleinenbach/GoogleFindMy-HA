# tests/test_location_request_cache_isolation.py

# tests/test_location_request_cache_isolation.py

import asyncio
from typing import Any
from collections.abc import Awaitable, Callable

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound import (
    start_sound_request,
    stop_sound_request,
)
from custom_components.googlefindmy.exceptions import (
    MissingNamespaceError,
    MissingTokenCacheError,
)


class FakeTokenCache:
    """Minimal cache stub recording namespaced access patterns."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.entry_id = label
        self.calls: list[tuple[str, str, Any | None]] = []
        self.values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        self.calls.append(("get", key, None))
        return self.values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.calls.append(("set", key, value))
        self.values[key] = value


class DummyFcmReceiver:
    """Simple receiver that triggers the callback on the next loop iteration."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[[str, str], None]] = {}

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> str:
        self.registered[device_id] = callback
        loop = asyncio.get_running_loop()
        loop.call_soon(callback, device_id, "deadbeef")
        return "fcm-token"

    async def async_unregister_for_location_updates(self, device_id: str) -> None:
        self.registered.pop(device_id, None)


def test_locate_request_prefers_entry_scoped_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locate flow must not fall back to the global cache when entry cache is provided."""

    primary_cache = FakeTokenCache("entry-one")
    receiver = DummyFcmReceiver()

    def fake_make_location_callback(
        *, ctx: Any, canonic_device_id: str, **_: Any
    ) -> Callable[[str, str], None]:
        def _callback(response_canonic_id: str, _: str) -> None:
            ctx.data = [{"canonic_id": response_canonic_id}]
            ctx.event.set()

        return _callback

    async def fake_async_nova_request(
        api_scope: str,
        hex_payload: str,
        *,
        cache_get: Callable[[str], Awaitable[Any]],
        cache_set: Callable[[str, Any], Awaitable[None]],
        cache: FakeTokenCache,
        namespace: str | None,
        **kwargs: Any,
    ) -> str:
        assert api_scope == location_request.NOVA_ACTION_API_SCOPE
        assert hex_payload == "payload"
        assert cache is primary_cache
        assert namespace == "entry-one"
        await cache_set("ttl", "value")
        await cache_get("ttl")
        return "00"

    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda: receiver)
    monkeypatch.setattr(
        location_request, "_make_location_callback", fake_make_location_callback
    )
    monkeypatch.setattr(location_request, "async_nova_request", fake_async_nova_request)
    monkeypatch.setattr(
        location_request, "create_location_request", lambda *args, **kwargs: "payload"
    )

    async def _run() -> None:
        result = await location_request.get_location_data_for_device(
            canonic_device_id="device-123",
            name="Tracker",
            session=None,
            username="user@example.com",
            cache=primary_cache,
        )

        assert result == [{"canonic_id": "device-123"}]
        calls = primary_cache.calls
        assert calls[0][:2] == ("get", "entry-one:nova_contributor_mode")
        assert calls[1][:2] == ("get", "entry-one:nova_last_network_mode_switch")
        assert calls[2][0] == "set"
        assert calls[2][1] == "entry-one:nova_last_network_mode_switch"
        assert isinstance(calls[2][2], int)
        assert calls[3:] == [
            ("set", "entry-one:ttl", "value"),
            ("get", "entry-one:ttl", None),
        ]

    asyncio.run(_run())


def test_start_sound_request_requires_cache() -> None:
    """Start sound submitter must raise when cache is missing."""

    async def _run() -> None:
        with pytest.raises(MissingTokenCacheError):
            await start_sound_request.async_submit_start_sound_request(
                "device-123",
                "token",
            )

    asyncio.run(_run())


def test_location_request_persists_mode_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contributor mode overrides are persisted and forwarded to the request builder."""

    cache = FakeTokenCache("entry-override")
    receiver = DummyFcmReceiver()

    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda: receiver)

    def fake_make_location_callback(
        *, ctx: Any, canonic_device_id: str, **_: Any
    ) -> Callable[[str, str], None]:
        def _callback(response_canonic_id: str, _: str) -> None:
            ctx.data = [{"canonic_id": response_canonic_id}]
            ctx.event.set()

        return _callback

    monkeypatch.setattr(
        location_request, "_make_location_callback", fake_make_location_callback
    )

    async def fake_async_nova_request(
        api_scope: str,
        hex_payload: str,
        *,
        cache_get: Callable[[str], Awaitable[Any]],
        cache_set: Callable[[str, Any], Awaitable[None]],
        cache: FakeTokenCache,
        namespace: str | None,
        **kwargs: Any,
    ) -> str:
        await cache_set("ttl", "value")
        await cache_get("ttl")
        return hex_payload

    monkeypatch.setattr(location_request, "async_nova_request", fake_async_nova_request)

    captured: dict[str, Any] = {}

    def fake_create_location_request(
        device_id: str,
        fcm_token: str,
        request_uuid: str,
        *,
        contributor_mode: str,
        last_mode_switch: int,
    ) -> str:
        captured["mode"] = contributor_mode
        captured["switch"] = last_mode_switch
        return "payload"

    monkeypatch.setattr(
        location_request, "create_location_request", fake_create_location_request
    )

    async def _run() -> None:
        await location_request.get_location_data_for_device(
            canonic_device_id="device-override",
            name="Tracker",
            cache=cache,
            contributor_mode="high_traffic",
            last_mode_switch=1_700_000_000,
        )

    asyncio.run(_run())

    assert captured["mode"] == "high_traffic"
    assert captured["switch"] == 1_700_000_000

    calls = cache.calls
    assert calls[0][:2] == ("get", "entry-override:nova_contributor_mode")
    assert calls[1] == ("set", "entry-override:nova_contributor_mode", "high_traffic")
    assert calls[2] == (
        "set",
        "entry-override:nova_last_network_mode_switch",
        1_700_000_000,
    )
    assert calls[3:] == [
        ("set", "entry-override:ttl", "value"),
        ("get", "entry-override:ttl", None),
    ]


def test_stop_sound_request_requires_cache() -> None:
    """Stop sound submitter must raise when cache is missing."""

    async def _run() -> None:
        with pytest.raises(MissingTokenCacheError):
            await stop_sound_request.async_submit_stop_sound_request(
                "device-123",
                "token",
            )

    asyncio.run(_run())


def test_start_sound_request_prefers_entry_scoped_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start sound flow must only touch the provided entry-local cache."""

    cache = FakeTokenCache("entry-one")

    async def fake_async_nova_request(
        api_scope: str,
        payload: str,
        *,
        cache_get: Callable[[str], Awaitable[Any]],
        cache_set: Callable[[str, Any], Awaitable[None]],
        cache: FakeTokenCache,
        namespace: str | None,
        **_: Any,
    ) -> str:
        assert api_scope == start_sound_request.NOVA_ACTION_API_SCOPE
        assert payload == "payload"
        assert cache is cache_ref
        assert namespace == "entry-one"
        await cache_set("ttl", "value")
        await cache_get("ttl")
        return "00"

    cache_ref = cache

    monkeypatch.setattr(
        start_sound_request, "async_nova_request", fake_async_nova_request
    )
    monkeypatch.setattr(
        start_sound_request, "start_sound_request", lambda *_, **__: "payload"
    )

    async def _run() -> None:
        result = await start_sound_request.async_submit_start_sound_request(
            "device-123",
            "token",
            cache=cache_ref,
        )

        assert result == "00"
        assert cache_ref.calls == [
            ("set", "entry-one:ttl", "value"),
            ("get", "entry-one:ttl", None),
        ]

    asyncio.run(_run())


def test_stop_sound_request_prefers_entry_scoped_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop sound flow must only touch the provided entry-local cache."""

    cache = FakeTokenCache("entry-two")

    async def fake_async_nova_request(
        api_scope: str,
        payload: str,
        *,
        cache_get: Callable[[str], Awaitable[Any]],
        cache_set: Callable[[str, Any], Awaitable[None]],
        cache: FakeTokenCache,
        namespace: str | None,
        **_: Any,
    ) -> str:
        assert api_scope == stop_sound_request.NOVA_ACTION_API_SCOPE
        assert payload == "payload"
        assert cache is cache_ref
        assert namespace == "entry-two"
        await cache_set("ttl", "value")
        await cache_get("ttl")
        return "00"

    cache_ref = cache

    monkeypatch.setattr(
        stop_sound_request, "async_nova_request", fake_async_nova_request
    )
    monkeypatch.setattr(
        stop_sound_request, "stop_sound_request", lambda *_, **__: "payload"
    )

    async def _run() -> None:
        result = await stop_sound_request.async_submit_stop_sound_request(
            "device-456",
            "token",
            cache=cache_ref,
        )

        assert result == "00"
        assert cache_ref.calls == [
            ("set", "entry-two:ttl", "value"),
            ("get", "entry-two:ttl", None),
        ]

    asyncio.run(_run())


def test_locate_request_requires_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing cache should raise a translated MissingTokenCacheError."""

    receiver = DummyFcmReceiver()
    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda: receiver)

    async def _run() -> None:
        with pytest.raises(MissingTokenCacheError):
            await location_request.get_location_data_for_device(
                canonic_device_id="device-123",
                name="Tracker",
            )

    asyncio.run(_run())


def test_locate_request_requires_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caches without entry_id should trigger MissingNamespaceError."""

    class _CacheWithoutEntry(FakeTokenCache):
        def __init__(self) -> None:
            super().__init__("entryless")
            self.entry_id = ""

    receiver = DummyFcmReceiver()
    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda: receiver)

    cache = _CacheWithoutEntry()

    async def _run() -> None:
        with pytest.raises(MissingNamespaceError):
            await location_request.get_location_data_for_device(
                canonic_device_id="device-456",
                name="Tracker",
                cache=cache,
            )

    asyncio.run(_run())

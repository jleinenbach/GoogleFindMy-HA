# tests/test_fcm_receiver_manual_locate.py
"""Tests for manual locate registration and background decode helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Callable

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA


class DummyCache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class DummyEntry:
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id


class DummyCoordinator:
    def __init__(self, entry_id: str, cache: DummyCache, tracked_id: str) -> None:
        self.config_entry = DummyEntry(entry_id)
        self.cache = cache
        self._tracked_id = tracked_id

    def is_device_present(self, device_id: str) -> bool:
        return device_id == self._tracked_id

    def get_device_display_name(self, device_id: str) -> str | None:
        if device_id == self._tracked_id:
            return "Tracked Device"
        return None


def test_manual_locate_registration_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering manual locate stores callbacks and returns cached token."""

    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    coordinator = DummyCoordinator(entry_id, cache, device_id)

    ensure_calls: list[tuple[str, Any]] = []
    start_calls: list[tuple[str, Any]] = []
    register_calls: list[str] = []

    async def fake_ensure(eid: str, provided_cache: Any) -> object:
        ensure_calls.append((eid, provided_cache))
        return object()

    async def fake_start(eid: str, provided_cache: Any) -> None:
        start_calls.append((eid, provided_cache))

    async def fake_register(eid: str) -> bool:
        register_calls.append(eid)
        return True

    monkeypatch.setattr(receiver, "_ensure_client_for_entry", fake_ensure)
    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", fake_start)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", fake_register)

    receiver.creds[entry_id] = {
        "fcm": {"registration": {"token": "token-123"}},
    }

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    async def _run() -> None:
        receiver.register_coordinator(coordinator)
        await asyncio.sleep(0)
        start_calls.clear()

        token = await receiver.async_register_for_location_updates(
            device_id, manual_callback
        )

        assert token == "token-123"
        assert receiver.location_update_callbacks[device_id] is manual_callback
        assert ensure_calls == [(entry_id, cache)]
        assert start_calls == [(entry_id, cache)]
        assert register_calls == []

        await receiver.async_unregister_for_location_updates(device_id)
        assert device_id not in receiver.location_update_callbacks

    asyncio.run(_run())


def test_run_callback_async_uses_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The callback helper delegates work to ``asyncio.to_thread`` when available."""

    receiver = FcmReceiverHA()
    invoked: list[tuple[str, str]] = []
    recorded: list[tuple[object, tuple[str, str]]] = []

    async def fake_to_thread(func: Callable[[str, str], Any], /, *args: str) -> None:
        recorded.append((func, args))
        func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    def callback(canonic: str, payload_hex: str) -> None:
        invoked.append((canonic, payload_hex))

    async def _run() -> None:
        await receiver._run_callback_async(callback, "dev-1", "deadbeef")

    asyncio.run(_run())

    assert recorded == [(callback, ("dev-1", "deadbeef"))]
    assert invoked == [("dev-1", "deadbeef")]


def test_process_background_update_uses_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background decode is executed in a worker thread helper."""

    receiver = FcmReceiverHA()
    schedule_calls: list[tuple[str, str]] = []

    def decode_stub(entry_id: str, payload_hex: str) -> dict[str, Any]:
        return {"latitude": 1.0, "payload": payload_hex, "entry_id": entry_id}

    async def fake_to_thread(
        func: Callable[[str, str], dict[str, Any]], /, *args: str
    ) -> dict[str, Any]:
        assert func is decode_stub
        return func(*args)

    monkeypatch.setattr(receiver, "_decode_background_location", decode_stub)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        receiver, "_schedule_flush", lambda key: schedule_calls.append(key)
    )

    async def _run() -> None:
        await receiver._process_background_update(
            "entry-1", "canonic-1", "c0ffee", {"entry-1", "entry-2"}
        )

    asyncio.run(_run())

    key = ("entry-1", "canonic-1")
    assert key in receiver._pending
    assert receiver._pending[key]["latitude"] == 1.0
    assert receiver._pending_targets[key] == {"entry-1", "entry-2"}
    assert schedule_calls == [key]

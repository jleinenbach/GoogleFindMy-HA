# tests/test_fcm_receiver_manual_locate.py
from __future__ import annotations

import asyncio
from typing import Any

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

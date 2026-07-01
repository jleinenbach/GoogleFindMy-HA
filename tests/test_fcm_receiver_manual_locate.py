# tests/test_fcm_receiver_manual_locate.py
"""Tests for manual locate registration and background decode helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha as fcm_mod
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

    async def fake_ensure(
        eid: str, provided_cache: Any, generation: int | None = None
    ) -> object:
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
    """Background decode is awaited directly and schedules a flush."""

    receiver = FcmReceiverHA()
    schedule_calls: list[tuple[str, str]] = []
    decode_calls: list[tuple[str, str]] = []

    async def decode_stub(entry_id: str, payload_hex: str) -> dict[str, Any]:
        decode_calls.append((entry_id, payload_hex))
        return {"latitude": 1.0, "payload": payload_hex, "entry_id": entry_id}

    monkeypatch.setattr(receiver, "_decode_background_location_async", decode_stub)
    monkeypatch.setattr(receiver, "_schedule_flush", schedule_calls.append)

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
    assert decode_calls == [("entry-1", "c0ffee")]


# ---------------------------------------------------------------------------
# Readiness gate: fail-closed when the FCM client never reaches STARTED
# ---------------------------------------------------------------------------


class _RunState:
    """Minimal stand-in for FcmPushClientRunState (only STARTED is compared)."""

    STARTED = "STARTED"


class _DummyPc:
    """Push client double exposing only the polled ``run_state`` attribute."""

    def __init__(self, run_state: object) -> None:
        self.run_state = run_state


def _wire_manual_locate(
    monkeypatch: pytest.MonkeyPatch,
    receiver: FcmReceiverHA,
    entry_id: str,
    cache: DummyCache,
) -> None:
    """Stub every collaborator the readiness gate calls before the poll loop."""

    async def _ensure_client(eid: str, c: Any, generation: int | None = None) -> object:
        return object()

    async def _start(eid: str, c: Any) -> None:
        return None

    async def _ensure_token(eid: str) -> str:
        return "token-123"

    async def _persist(eid: str, tok: str) -> None:
        return None

    monkeypatch.setattr(
        receiver, "_select_manual_locate_entry", lambda cid: (entry_id, cache)
    )
    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure_client)
    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", _start)
    monkeypatch.setattr(receiver, "_ensure_token_for_entry", _ensure_token)
    monkeypatch.setattr(receiver, "_update_token_routing", lambda tok, ids: None)
    monkeypatch.setattr(receiver, "_persist_routing_token", _persist)
    monkeypatch.setattr(fcm_mod, "FcmPushClientRunState", _RunState)


@pytest.mark.asyncio
async def test_manual_locate_fails_closed_when_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that never reaches STARTED yields None and leaves no callback.

    Regression for the deterministic first-run locate timeout: the gate used to
    fall through to ``return token`` (fail-open), handing a token to a listener
    that was not yet connected so the caller blocked until an opaque 30s timeout.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    receiver.pcs[entry_id] = _DummyPc(run_state="CONNECTING")  # type: ignore[assignment]

    # Do not actually sleep through the readiness budget.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token is None
    assert device_id not in receiver.location_update_callbacks


@pytest.mark.asyncio
async def test_manual_locate_returns_token_when_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA happy path: STARTED within budget returns the token, keeps the callback."""
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    receiver.pcs[entry_id] = _DummyPc(run_state=_RunState.STARTED)  # type: ignore[assignment]

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert receiver.location_update_callbacks[device_id] is manual_callback

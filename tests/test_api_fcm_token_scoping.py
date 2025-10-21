# tests/test_api_fcm_token_scoping.py
from __future__ import annotations

import logging

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI


@dataclass
class DummyCache:
    """Minimal cache shim exposing an entry namespace for the API helper."""

    entry_id: Optional[str] = None

    async def async_get_cached_value(self, key: str) -> Any:
        return None

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        return None


class TrackingReceiver:
    """Record invocations of get_fcm_token while serving deterministic tokens."""

    def __init__(self, tokens: dict[str, str]):
        self._tokens = tokens
        self.calls: list[Optional[str]] = []

    def get_fcm_token(self, entry_id: Optional[str] = None) -> Optional[str]:
        self.calls.append(entry_id)
        if entry_id:
            if entry_id not in self._tokens:
                raise AssertionError(f"Unexpected entry_id {entry_id!r}")
            return self._tokens[entry_id]
        return self._tokens.get("legacy")


def test_get_fcm_token_prefers_entry_scope(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Ensure API helper forwards entry_id to the receiver when available."""

    receiver = TrackingReceiver({"entry-1": "token-entry-1", "legacy": "legacy-token"})
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda: receiver)

    api = GoogleFindMyAPI(cache=DummyCache(entry_id="entry-1"))
    caplog.set_level(logging.INFO)
    token = api._get_fcm_token_for_action()

    assert token == "token-entry-1"
    assert receiver.calls == ["entry-1"]
    assert "falling back to legacy scope" not in caplog.text


def test_get_fcm_token_fallback_handles_missing_entry_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Legacy fallback still returns a token when no entry namespace is available."""

    receiver = TrackingReceiver({"legacy": "legacy-token"})
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda: receiver)

    api = GoogleFindMyAPI(cache=DummyCache(entry_id=None))
    caplog.set_level(logging.INFO)
    token = api._get_fcm_token_for_action()

    assert token == "legacy-token"
    assert receiver.calls == [None]
    assert "Cannot obtain FCM token" not in caplog.text


def test_actions_use_scoped_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Play/Stop Sound actions obtain the token tied to their entry."""

    submissions: list[tuple[str, str, str, Optional[str], Optional[str]]] = []

    async def fake_submit_start(
        device_id: str,
        token: str,
        *,
        session: Any,
        namespace: Optional[str],
        cache: DummyCache,
    ) -> str:
        submissions.append(("start", device_id, token, namespace, getattr(cache, "entry_id", None)))
        return "ok"

    async def fake_submit_stop(
        device_id: str,
        token: str,
        *,
        session: Any,
        namespace: Optional[str],
        cache: DummyCache,
    ) -> str:
        submissions.append(("stop", device_id, token, namespace, getattr(cache, "entry_id", None)))
        return "ok"

    monkeypatch.setattr(
        "custom_components.googlefindmy.api.async_submit_start_sound_request",
        fake_submit_start,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.api.async_submit_stop_sound_request",
        fake_submit_stop,
    )

    api_entry_1 = GoogleFindMyAPI(cache=DummyCache(entry_id="entry-1"))
    api_entry_2 = GoogleFindMyAPI(cache=DummyCache(entry_id="entry-2"))

    monkeypatch.setattr(api_entry_1, "_get_fcm_token_for_action", lambda: "token-entry-1")
    monkeypatch.setattr(api_entry_2, "_get_fcm_token_for_action", lambda: "token-entry-2")

    async def _exercise() -> None:
        assert await api_entry_1.async_play_sound("device-1")
        assert await api_entry_2.async_play_sound("device-2")
        assert await api_entry_1.async_stop_sound("device-1")
        assert await api_entry_2.async_stop_sound("device-2")

    asyncio.run(_exercise())

    tokens_used = [entry[2] for entry in submissions]
    assert tokens_used == [
        "token-entry-1",
        "token-entry-2",
        "token-entry-1",
        "token-entry-2",
    ]

    namespaces = [entry[3] for entry in submissions]
    assert namespaces == ["entry-1", "entry-2", "entry-1", "entry-2"]

    caches = [entry[4] for entry in submissions]
    assert caches == ["entry-1", "entry-2", "entry-1", "entry-2"]


def test_async_get_device_location_uses_scoped_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-account location requests must not fall back to legacy cache helpers."""

    recorded: list[tuple[Optional[str], Any]] = []

    async def fake_get_location_data(
        canonic_device_id: str,
        name: str,
        *,
        session: Any,
        namespace: Optional[str],
        cache: DummyCache,
        **_: Any,
    ) -> list[dict[str, Any]]:
        recorded.append((namespace, cache))
        return [{"canonic_id": canonic_device_id, "latitude": 1}]

    monkeypatch.setattr(api_module, "get_location_data_for_device", fake_get_location_data)

    api_entry_1 = GoogleFindMyAPI(cache=DummyCache(entry_id="entry-1"))
    api_entry_2 = GoogleFindMyAPI(cache=DummyCache(entry_id="entry-2"))

    async def _exercise() -> None:
        loc1 = await api_entry_1.async_get_device_location("device-1", "Device 1")
        loc2 = await api_entry_2.async_get_device_location("device-2", "Device 2")

        assert loc1 == {"canonic_id": "device-1", "latitude": 1}
        assert loc2 == {"canonic_id": "device-2", "latitude": 1}

    asyncio.run(_exercise())

    assert recorded == [
        ("entry-1", api_entry_1._cache),  # type: ignore[attr-defined]
        ("entry-2", api_entry_2._cache),  # type: ignore[attr-defined]
    ]

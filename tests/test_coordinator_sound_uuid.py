# tests/test_coordinator_sound_uuid.py
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator


@pytest.mark.asyncio
async def test_async_play_sound_stores_uuid() -> None:
    """Play sound should cache the returned request UUID per device."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    api_calls: list[SimpleNamespace] = []

    async def _async_play_sound(device_id: str) -> tuple[bool, str]:
        api_calls.append(SimpleNamespace(device_id=device_id))
        return True, "uuid-1"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is True
    assert coordinator._sound_request_uuids == {"device-1": "uuid-1"}  # type: ignore[attr-defined]
    assert api_calls == [SimpleNamespace(device_id="device-1")]


@pytest.mark.asyncio
async def test_async_stop_sound_uses_cached_uuid() -> None:
    """Stop sound should look up a cached UUID when none is provided."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {"device-1": "uuid-1"}  # type: ignore[attr-defined]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]
    coordinator._api_push_ready = lambda: True  # type: ignore[attr-defined]

    api_calls: list[tuple[str, str | None]] = []

    async def _async_stop_sound(device_id: str, request_uuid: str | None) -> bool:
        api_calls.append((device_id, request_uuid))
        return True

    coordinator.api = SimpleNamespace(async_stop_sound=_async_stop_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_stop_sound("device-1")

    assert result is True
    assert coordinator._sound_request_uuids == {}  # type: ignore[attr-defined]
    assert api_calls == [("device-1", "uuid-1")]


@pytest.mark.asyncio
async def test_async_stop_sound_warns_when_uuid_missing(caplog: pytest.LogCaptureFixture) -> None:
    """Stop sound should warn when no cached UUID is available."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {}  # type: ignore[attr-defined]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]
    coordinator._api_push_ready = lambda: True  # type: ignore[attr-defined]

    api_calls: list[tuple[str, str | None]] = []

    async def _async_stop_sound(device_id: str, request_uuid: str | None) -> bool:
        api_calls.append((device_id, request_uuid))
        return True

    coordinator.api = SimpleNamespace(async_stop_sound=_async_stop_sound)  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING):
        result = await coordinator.async_stop_sound("device-1")

    assert result is True
    assert api_calls == [("device-1", None)]
    assert "Missing Play Sound UUID for device-1" in caplog.text

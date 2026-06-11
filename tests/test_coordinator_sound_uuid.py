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
async def test_async_play_sound_stores_uuid_on_failed_dispatch() -> None:
    """Store the cancel key even when the play dispatch failed post-dispatch.

    api.async_play_sound returns ``(False, uuid)`` when the request was sent but
    the outcome was an empty response or a handled error. The coordinator must
    still cache that UUID ("fail toward keeping the cancel key") so a later Stop
    can correlate. Regression for IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    transport_problems: list[bool] = []
    coordinator._note_push_transport_problem = (  # type: ignore[attr-defined]
        lambda: transport_problems.append(True)
    )
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        # Post-dispatch failure: sent, but handled error / empty response.
        return False, "uuid-2"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    assert coordinator._sound_request_uuids == {"device-1": "uuid-2"}  # type: ignore[attr-defined]
    # Failure still flags a push-transport problem.
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_async_play_sound_skips_store_on_pre_dispatch_guard() -> None:
    """Do not cache a UUID when nothing was sent (pre-dispatch guard).

    api.async_play_sound returns ``(False, None)`` only when it bailed out before
    dispatch (e.g. no FCM token). With no request on the wire there is no cancel
    key to keep, so the cache must stay empty.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    transport_problems: list[bool] = []
    coordinator._note_push_transport_problem = (  # type: ignore[attr-defined]
        lambda: transport_problems.append(True)
    )
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        # Pre-dispatch guard: nothing sent, no cancel key.
        return False, None

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    assert coordinator._sound_request_uuids == {}  # type: ignore[attr-defined]
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_pre_dispatch_failure_keeps_existing_cancel_key() -> None:
    """A failed Play must not clobber an earlier, still-ringing play's key.

    Codex regression (PR #1100): a device is already ringing from an earlier
    successful Play whose cancel key is cached. A *new* Play attempt fails before
    reaching the wire, so api.async_play_sound returns ``(False, None)``. The
    coordinator must keep the existing key intact — overwriting it would make the
    later Stop send a fresh, non-correlating UUID and leave the device ringing.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    # Earlier successful Play cached a valid cancel key for this device.
    coordinator._sound_request_uuids = {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        # New attempt fails pre-dispatch: nothing sent, no cancel key.
        return False, None

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    # The previous, still-valid cancel key survives untouched.
    assert coordinator._sound_request_uuids == {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]


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
async def test_async_stop_sound_warns_when_uuid_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
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

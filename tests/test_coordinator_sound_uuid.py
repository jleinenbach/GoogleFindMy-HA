# tests/test_coordinator_sound_uuid.py
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.const import StopSoundOutcome
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.cache import (
    SOUND_UUID_MAX_AGE_S,
    is_sound_uuid_expired,
)


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
async def test_async_play_sound_skips_store_on_non_accepted_play() -> None:
    """Do not cache a UUID when the command was not accepted.

    Under the success-only contract api.async_play_sound returns ``(False, None)``
    for *every* non-acceptance — a pre-dispatch guard (e.g. no FCM token), a
    connection-setup error, or a server rejection (401/403/5xx). The play never
    started a ring, so there is no cancel key to keep and the cache must stay
    empty (while a push-transport problem is still flagged).
    Regression for IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY.
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
        # Non-accepted play (pre-dispatch guard / rejection): no cancel key.
        return False, None

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    assert coordinator._sound_request_uuids == {}  # type: ignore[attr-defined]
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_non_accepted_play_keeps_existing_cancel_key() -> None:
    """A non-accepted Play must not clobber an earlier, still-ringing play's key.

    Codex regression (PR #1100, iter-4): a device is already ringing from an
    earlier successful Play whose cancel key is cached. A *new* Play attempt is
    not accepted — either it fails before reaching the wire OR the server rejects
    it with 401/403/5xx. Under the success-only contract api.async_play_sound
    returns ``(False, None)`` for *both* causes, so the coordinator keeps the
    existing key intact. Overwriting it (the iter-4 bug: a rejected play used to
    return its never-accepted UUID) would make the later Stop send a fresh,
    non-correlating UUID and leave the device ringing.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    # Earlier successful Play cached a valid cancel key for this device.
    coordinator._sound_request_uuids = {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        # New attempt not accepted (e.g. server 401/403, or pre-dispatch): the
        # success-only contract yields (False, None) — no cancel key.
        return False, None

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    # The previous, still-valid cancel key survives untouched.
    assert coordinator._sound_request_uuids == {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_post_dispatch_ambiguous_play_caches_uuid() -> None:
    """A dispatched-but-unconfirmed Play caches its key so Stop can still cancel.

    Codex regression (PR #1100, iter-5): a network failure at/after the request
    reached the wire (server disconnect, read timeout) may have started a ring
    even without a 200. api.async_play_sound then returns ``(False, <uuid>)`` —
    ``ok`` is False, but the cancel key is preserved. The coordinator caches every
    non-null UUID, so it stores this key (letting a later Stop target the
    possibly-active ring) while still flagging the push-transport problem.
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
        # Post-dispatch ambiguity: not confirmed (ok=False), but a ring may be
        # active, so the cancel key is returned for caching.
        return False, "uuid-postsend"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    # The ambiguous-dispatch key is cached so a later Stop can target it.
    assert coordinator._sound_request_uuids == {"device-1": "uuid-postsend"}  # type: ignore[attr-defined]
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_ambiguous_play_does_not_overwrite_known_cancel_key() -> None:
    """An ambiguous new Play must not clobber a known-good cancel key.

    Codex regression (PR #1106, follow-up): a device is already ringing from an
    earlier accepted Play whose cancel key is cached. A *new* Play attempt hits a
    transient 5xx that may have been generated before Nova accepted the command,
    so api.async_play_sound returns ``(False, <new-uuid>)`` — ok is False but a
    fresh UUID is present. Storing every non-null UUID would overwrite the
    known-good key for the still-ringing earlier play, making the default Stop
    target the wrong request. The storing rule (overwrite only on ok=True or an
    empty slot) keeps the cached key intact while still flagging the transport
    problem.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    # Earlier accepted Play cached a valid cancel key for this device.
    coordinator._sound_request_uuids = {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    transport_problems: list[bool] = []
    coordinator._note_push_transport_problem = (  # type: ignore[attr-defined]
        lambda: transport_problems.append(True)
    )
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        # Ambiguous transient 5xx: not accepted (ok=False), fresh UUID present.
        return False, "uuid-ambiguous-new"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    # The known-good cancel key survives; the ambiguous UUID is discarded.
    assert coordinator._sound_request_uuids == {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_ambiguous_play_keeps_fresh_tracked_cancel_key() -> None:
    """A fresh, timestamp-tracked cancel key still survives an ambiguous Play.

    The expiry-aware guard must only treat *aged* keys as absent. With a
    recent timestamp present, the known-good key is younger than
    SOUND_UUID_MAX_AGE_S, so an ambiguous ``(False, <new-uuid>)`` must not
    clobber it (the still-ringing earlier play keeps its cancel handle).
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    coordinator._sound_request_timestamps = {"device-1": time.time()}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        return False, "uuid-ambiguous-new"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    assert coordinator._sound_request_uuids == {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambiguous_play_replaces_expired_cancel_key() -> None:
    """An expired cancel key must not block caching a fresh ambiguous UUID.

    Codex regression (PR #1106, follow-up 2): a prior Play key lingers past the
    30-minute sound-UUID expiry, then a new Play hits a dispatch-ambiguous
    failure returning ``(False, <new-uuid>)``. The reload filter would have
    discarded the stale key, so the store-path guard must treat it as absent
    and cache the new UUID — otherwise the only handle on the possibly-current
    ring is dropped and the default Stop keeps targeting the expired request.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {"device-1": "uuid-expired"}  # type: ignore[attr-defined]
    # Timestamp older than the max age: the load path would discard this key.
    coordinator._sound_request_timestamps = {  # type: ignore[attr-defined]
        "device-1": time.time() - (SOUND_UUID_MAX_AGE_S + 60)
    }
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    transport_problems: list[bool] = []
    coordinator._note_push_transport_problem = (  # type: ignore[attr-defined]
        lambda: transport_problems.append(True)
    )
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        return False, "uuid-fresh-ambiguous"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    # The expired key is treated as absent, so the fresh handle is cached.
    assert coordinator._sound_request_uuids == {"device-1": "uuid-fresh-ambiguous"}  # type: ignore[attr-defined]
    assert transport_problems == [True]


@pytest.mark.asyncio
async def test_async_stop_sound_ignores_expired_cached_uuid() -> None:
    """Stop must not target an expired cached UUID (sibling of the store guard).

    A cached key older than SOUND_UUID_MAX_AGE_S refers to a ring that has long
    auto-stopped; the reload filter would discard it. The default Stop path must
    treat it as absent and stop without it rather than cancel a dead request.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {"device-1": "uuid-expired"}  # type: ignore[attr-defined]
    coordinator._sound_request_timestamps = {  # type: ignore[attr-defined]
        "device-1": time.time() - (SOUND_UUID_MAX_AGE_S + 60)
    }
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]
    coordinator._api_push_ready = lambda: True  # type: ignore[attr-defined]

    api_calls: list[tuple[str, str | None]] = []

    async def _async_stop_sound(device_id: str, request_uuid: str | None) -> bool:
        api_calls.append((device_id, request_uuid))
        return True

    coordinator.api = SimpleNamespace(async_stop_sound=_async_stop_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_stop_sound("device-1")

    # Ignored key means no correlation, so the stop is unprovable, not a success.
    assert result is StopSoundOutcome.UNCORRELATED
    # The expired UUID is ignored; Stop is attempted without a request UUID.
    assert api_calls == [("device-1", None)]
    # Housekeeping: the dead key is dropped, it would not survive a reload anyway.
    assert coordinator._sound_request_uuids == {}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ambiguous_play_keeps_untracked_cancel_key() -> None:
    """An untracked cancel key (no timestamp entry) is treated as fresh.

    The staleness check must not crash or wrongly expire a key that lives in
    ``_sound_request_uuids`` without a matching ``_sound_request_timestamps``
    entry (a transient/anomalous state). Such a key cannot be proven aged, so
    an ambiguous Play must leave it intact rather than drop the only handle.
    """

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._sound_request_uuids = {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]
    # Timestamp map exists but has no entry for this device.
    coordinator._sound_request_timestamps = {}  # type: ignore[attr-defined]
    coordinator.can_play_sound = lambda _device_id: True  # type: ignore[assignment]
    coordinator._note_push_transport_problem = lambda: None  # type: ignore[attr-defined]
    coordinator._set_auth_state = lambda **kwargs: None  # type: ignore[attr-defined]

    async def _async_play_sound(device_id: str) -> tuple[bool, str | None]:
        return False, "uuid-ambiguous-new"

    coordinator.api = SimpleNamespace(async_play_sound=_async_play_sound)  # type: ignore[attr-defined]

    result = await coordinator.async_play_sound("device-1")

    assert result is False
    assert coordinator._sound_request_uuids == {"device-1": "uuid-ringing"}  # type: ignore[attr-defined]


def test_is_sound_uuid_expired_boundary() -> None:
    """The shared expiry predicate is exclusive at the max-age boundary."""

    now = 10_000.0
    # Exactly at the boundary is not yet expired (strictly greater-than).
    assert (
        is_sound_uuid_expired(now - SOUND_UUID_MAX_AGE_S, now, SOUND_UUID_MAX_AGE_S)
        is False
    )
    # One second past the boundary is expired.
    assert (
        is_sound_uuid_expired(now - SOUND_UUID_MAX_AGE_S - 1, now, SOUND_UUID_MAX_AGE_S)
        is True
    )
    # A just-stored key is fresh.
    assert is_sound_uuid_expired(now, now, SOUND_UUID_MAX_AGE_S) is False


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

    assert result is StopSoundOutcome.CANCELLED
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

    assert result is StopSoundOutcome.UNCORRELATED
    assert api_calls == [("device-1", None)]
    assert "No cancel key for device-1" in caplog.text
    assert "the ring may keep playing" in caplog.text

# tests/test_fcm_dead_token_detector.py
"""A push token Google accepts but no longer delivers to heals itself.

Measured on 2026-11-14 (issue 224 follow-up, two accounts in one Home
Assistant): Google answered every locate request of the second account with
HTTP 200, yet not a single push arrived on that account's channel over three
restarts. One re-registration (``async_reregister_fcm``) issued a new token
and the next two requests were answered by push within a second. On the
healthy account, 10 of 10 requests were answered by push, including requests
for a tracker that had no location to report.

The receiver therefore counts, per config entry, accepted requests that ran
into ``LOCATION_REQUEST_TIMEOUT_S`` without any push on that entry's channel.
Any push on the channel resets the count, whether it matches a request or not.
At ``FCM_DEAD_TOKEN_TIMEOUT_THRESHOLD`` it starts one throttled
re-registration (``async_regenerate_fcm_token``) as its own task and locks the
entry out for ``FCM_DEAD_TOKEN_REREGISTER_LOCKOUT_S``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    FCM_DEAD_TOKEN_REREGISTER_LOCKOUT_S,
    FCM_DEAD_TOKEN_TIMEOUT_THRESHOLD,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request as location_request_module,
)

N = FCM_DEAD_TOKEN_TIMEOUT_THRESHOLD
_TOKEN = "token-value-that-must-never-be-logged"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def regenerate(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every re-registration the detector starts; answer ``True``."""

    calls: list[str] = []

    async def _fake(*, hass: Any, entry_id: str) -> bool:
        calls.append(entry_id)
        return True

    monkeypatch.setattr(fcm_receiver_ha, "async_regenerate_fcm_token", _fake)
    return calls


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> tuple[FcmReceiverHA, _Clock]:
    rx = FcmReceiverHA()
    rx.attach_hass(SimpleNamespace(data={}, loop=None))  # type: ignore[arg-type]
    clock = _Clock()
    monkeypatch.setattr(rx, "_dead_token_now", clock)
    return rx, clock


async def _settle(rx: FcmReceiverHA | None = None) -> None:
    """Let the scheduled re-registration task run to its end."""

    await asyncio.sleep(0)
    if rx is not None and rx._active_tasks:
        await asyncio.wait_for(asyncio.gather(*rx._active_tasks), timeout=5)


def _push(canonic_payload: bool = True) -> dict[str, Any]:
    if not canonic_payload:
        return {"data": {}}
    raw = base64.b64encode(b"\x0a\x03abc").decode()
    return {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": raw}}


@pytest.mark.asyncio
async def test_t_t3_t5_silent_channel_reregisters_once_for_that_entry(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """(a) The measured case: N accepted timeouts on B, no push -> one call for B."""

    rx, _clock = receiver
    for _ in range(N):
        rx.note_accepted_without_push("entry-b")
    rx.note_accepted_without_push("entry-a")
    await _settle(rx)

    assert regenerate == ["entry-b"]


@pytest.mark.asyncio
async def test_t_t3_below_threshold_starts_nothing(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    rx, _clock = receiver
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)

    assert regenerate == []


@pytest.mark.asyncio
async def test_t_t4_any_push_on_the_channel_resets_the_count(
    receiver: tuple[FcmReceiverHA, _Clock],
    regenerate: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A push for a foreign device on B's channel still proves B's token lives."""

    rx, _clock = receiver

    async def _foreign(_hex: str) -> str:
        return "some-other-device"

    monkeypatch.setattr(rx, "_extract_canonic_id_async", _foreign)

    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await rx._handle_notification_async("entry-b", _push())
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)

    assert regenerate == []


@pytest.mark.asyncio
async def test_t_t4_a_push_without_fmd_payload_resets_the_count(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """(c) The reset sits ahead of every early return of the handler."""

    rx, _clock = receiver
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await rx._handle_notification_async("entry-b", _push(canonic_payload=False))
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)

    assert regenerate == []


@pytest.mark.asyncio
async def test_t_t4_a_push_on_another_channel_does_not_reset(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """A's living channel says nothing about B's token."""

    rx, _clock = receiver
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await rx._handle_notification_async("entry-a", _push(canonic_payload=False))
    rx.note_accepted_without_push("entry-b")
    await _settle(rx)

    assert regenerate == ["entry-b"]


@pytest.mark.asyncio
async def test_t_t6_lockout_blocks_until_it_expires(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """(d) Further silence inside the lockout starts nothing; after it, one call."""

    rx, clock = receiver
    for _ in range(N):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)
    assert regenerate == ["entry-b"]

    clock.now += FCM_DEAD_TOKEN_REREGISTER_LOCKOUT_S - 1
    for _ in range(N):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)
    assert regenerate == ["entry-b"]

    clock.now += 2
    rx.note_accepted_without_push("entry-b")
    await _settle(rx)
    assert regenerate == ["entry-b", "entry-b"]


@pytest.mark.asyncio
async def test_t_t6_count_starts_fresh_for_the_new_token(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """Silence before the re-registration belongs to the old token.

    After the lockout, fewer than N silent requests on the new token start
    nothing, however many there were before the trigger.
    """

    rx, clock = receiver
    for _ in range(N):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)
    assert regenerate == ["entry-b"]

    clock.now += FCM_DEAD_TOKEN_REREGISTER_LOCKOUT_S + 1
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    await _settle(rx)

    assert regenerate == ["entry-b"]


def test_t_t6_lockout_is_four_hours_and_threshold_three() -> None:
    """V-T1 and V-T2 as decided on 2026-11-14."""

    assert FCM_DEAD_TOKEN_TIMEOUT_THRESHOLD == 3
    assert FCM_DEAD_TOKEN_REREGISTER_LOCKOUT_S == 4 * 3600


@pytest.mark.asyncio
async def test_t_t7_warning_names_the_entry_and_never_the_token(
    receiver: tuple[FcmReceiverHA, _Clock],
    regenerate: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(f) One WARNING with entry id and count; the count itself only on DEBUG."""

    rx, _clock = receiver
    rx.pcs["entry-b"] = SimpleNamespace(credentials={"fcm": {"token": _TOKEN}})  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=fcm_receiver_ha.__name__):
        for _ in range(N):
            rx.note_accepted_without_push("entry-b")
        await _settle(rx)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "entry-b" in message
    assert f"after {N} accepted locate requests" in message
    assert _TOKEN not in caplog.text
    debug_counts = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "silent" in r.getMessage()
    ]
    assert len(debug_counts) >= N - 1


@pytest.mark.asyncio
async def test_t_t5_rejected_reregistration_logs_info_and_keeps_the_lockout(
    receiver: tuple[FcmReceiverHA, _Clock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(h) The cooldown of the throttled path answers False: INFO, lockout stays."""

    rx, _clock = receiver
    calls: list[str] = []

    async def _refused(*, hass: Any, entry_id: str) -> bool:
        calls.append(entry_id)
        return False

    monkeypatch.setattr(fcm_receiver_ha, "async_regenerate_fcm_token", _refused)
    with caplog.at_level(logging.INFO, logger=fcm_receiver_ha.__name__):
        for _ in range(2 * N):
            rx.note_accepted_without_push("entry-b")
        await _settle(rx)

    assert calls == ["entry-b"]
    assert any(
        r.levelno == logging.INFO and "not started" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_t_t5_a_failing_reregistration_is_logged_not_raised(
    receiver: tuple[FcmReceiverHA, _Clock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rx, _clock = receiver

    async def _boom(*, hass: Any, entry_id: str) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(fcm_receiver_ha, "async_regenerate_fcm_token", _boom)
    with caplog.at_level(logging.WARNING, logger=fcm_receiver_ha.__name__):
        for _ in range(N):
            rx.note_accepted_without_push("entry-b")
        await _settle(rx)

    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_t_t5_without_hass_nothing_is_started(regenerate: list[str]) -> None:
    rx = FcmReceiverHA()
    for _ in range(N):
        rx.note_accepted_without_push("entry-b")
    rx.note_accepted_without_push(None)
    rx.note_accepted_without_push("")
    await _settle(rx)

    assert regenerate == []


@pytest.mark.asyncio
async def test_t_t4_a_push_during_overlapping_requests_is_not_undone(
    receiver: tuple[FcmReceiverHA, _Clock], regenerate: list[str]
) -> None:
    """Codex review on PR 1455: N requests wait at once, one push arrives.

    The push resets the count, but the N timeouts that follow belong to
    requests that were pending while the channel delivered; none of them
    counts.
    """

    rx, _clock = receiver
    started = [rx.dead_token_push_generation("entry-b") for _ in range(N)]
    await rx._handle_notification_async("entry-b", _push(canonic_payload=False))
    for generation in started:
        rx.note_accepted_without_push("entry-b", push_generation=generation)
    await _settle(rx)
    assert regenerate == []

    current = rx.dead_token_push_generation("entry-b")
    for _ in range(N):
        rx.note_accepted_without_push("entry-b", push_generation=current)
    await _settle(rx)
    assert regenerate == ["entry-b"]


def test_t_t4_push_generation_is_per_entry() -> None:
    rx = FcmReceiverHA()
    assert rx.dead_token_push_generation(None) == 0
    assert rx.dead_token_push_generation("entry-a") == 0


@pytest.mark.asyncio
async def test_t_t4_push_generation_counts_pushes_of_its_channel_only() -> None:
    rx = FcmReceiverHA()
    await rx._handle_notification_async("entry-a", _push(canonic_payload=False))
    await rx._handle_notification_async("entry-a", _push(canonic_payload=False))

    assert rx.dead_token_push_generation("entry-a") == 2
    assert rx.dead_token_push_generation("entry-b") == 0


def test_t_t3_purge_drops_the_detector_state(
    receiver: tuple[FcmReceiverHA, _Clock],
) -> None:
    rx, _clock = receiver
    for _ in range(N - 1):
        rx.note_accepted_without_push("entry-b")
    rx._dead_token_lockout_until["entry-b"] = 1.0
    rx._dead_token_push_generation["entry-b"] = 4

    rx._purge_entry_tokens("entry-b")

    assert "entry-b" not in rx._dead_token_silent_counts
    assert "entry-b" not in rx._dead_token_lockout_until
    assert "entry-b" not in rx._dead_token_push_generation


# ---------------------------------------------------------------------------
# The signal from the locate flow
# ---------------------------------------------------------------------------


class _NotingReceiver:
    def __init__(self, generation: Any = 7) -> None:
        self.noted: list[tuple[str | None, int | None]] = []
        self._generation = generation

    async def async_register_for_location_updates(
        self, _device_id: str, _callback: Any
    ) -> str:
        return "fcm-token"

    async def async_unregister_for_location_updates(self, _device_id: str) -> None:
        return None

    def dead_token_push_generation(self, _entry_id: str | None) -> Any:
        return self._generation

    def note_accepted_without_push(
        self, entry_id: str | None, push_generation: int | None = None
    ) -> None:
        self.noted.append((entry_id, push_generation))


class _Cache:
    entry_id = "entry-b"

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value


def _wire(monkeypatch: pytest.MonkeyPatch, rx: Any, nova: Any) -> None:
    monkeypatch.setattr(location_request_module, "_FCM_ReceiverGetter", lambda *_a: rx)
    monkeypatch.setattr(
        location_request_module,
        "_make_location_callback",
        lambda **_k: lambda *_a: None,
    )
    monkeypatch.setattr(
        location_request_module, "create_location_request", lambda *a, **k: "deadbeef"
    )
    monkeypatch.setattr(location_request_module, "async_nova_request", nova)
    monkeypatch.setattr(location_request_module, "LOCATION_REQUEST_TIMEOUT_S", 0.01)


async def _locate() -> list[dict[str, Any]]:
    return await location_request_module.get_location_data_for_device(
        canonic_device_id="device-b",
        name="Tracker",
        session=None,
        username="user@example.com",
        cache=_Cache(),
    )


@pytest.mark.asyncio
async def test_t_t3_accepted_timeout_signals_the_receiver_of_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(g) The timeout branch tells the receiver; the result stays empty."""

    rx = _NotingReceiver()

    async def _accepted(*_a: object, **_k: object) -> bytes:
        return b""

    _wire(monkeypatch, rx, _accepted)

    assert await _locate() == []
    assert rx.noted == [("entry-b", 7)]


@pytest.mark.asyncio
async def test_t_t3_a_request_that_was_not_accepted_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) ``LocationRequestNotAcceptedError`` never reaches the counter."""

    rx = _NotingReceiver()

    async def _refused(*_a: object, **_k: object) -> bytes:
        raise RuntimeError("nova failed")

    _wire(monkeypatch, rx, _refused)

    with pytest.raises(location_request_module.LocationRequestNotAcceptedError):
        await _locate()
    assert rx.noted == []


@pytest.mark.parametrize(
    ("receiver_double", "expected"),
    [
        (_NotingReceiver(generation=3), 3),
        (_NotingReceiver(generation="3"), None),
        (object(), None),
    ],
)
def test_t_t4_push_generation_lookup_is_best_effort(
    receiver_double: Any, expected: int | None
) -> None:
    assert (
        location_request_module._push_generation(receiver_double, "entry-b") == expected
    )


def test_t_t4_a_raising_generation_lookup_yields_none() -> None:
    class _Raising(_NotingReceiver):
        def dead_token_push_generation(self, _entry_id: str | None) -> Any:
            raise RuntimeError("lookup broke")

    assert location_request_module._push_generation(_Raising(), "entry-b") is None


def test_t_t3_a_hostile_receiver_attribute_does_not_escape_the_signal() -> None:
    """Best effort also covers an attribute lookup that raises.

    Called directly: through the locate flow the broad outer handler would
    turn an escaping error into ``[]`` as well and hide the difference.
    """

    class _Hostile(_NotingReceiver):
        def __getattribute__(self, name: str) -> Any:
            if name == "note_accepted_without_push":
                raise RuntimeError("hostile lookup")
            return object.__getattribute__(self, name)

    location_request_module._note_accepted_without_push(_Hostile(), "entry-b")


@pytest.mark.asyncio
async def test_t_t3_a_failing_signal_keeps_the_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best effort: a receiver whose method raises does not change the outcome."""

    class _Raising(_NotingReceiver):
        def note_accepted_without_push(
            self, entry_id: str | None, push_generation: int | None = None
        ) -> None:
            raise RuntimeError("detector broke")

    async def _accepted(*_a: object, **_k: object) -> bytes:
        return b""

    _wire(monkeypatch, _Raising(), _accepted)

    assert await _locate() == []


# ---------------------------------------------------------------------------
# Characterisation: the cross-account fallback of ``get_fcm_token`` (V-T3)
# ---------------------------------------------------------------------------


def _creds(token: str) -> dict[str, Any]:
    return {"fcm": {"registration": {"token": token}}}


def test_get_fcm_token_falls_back_to_another_entry_token() -> None:
    """Pinned as it is: an entry without a token gets another entry's token.

    The fallback was meant as an emergency path, not the regular one. The
    locate path reaches it too (``_ensure_token_for_entry`` calls
    ``get_fcm_token``), as does the sound path; changing it belongs in its own
    change. Only the entry-scoped variant, used by the first-locate reconnect,
    never falls back.
    """

    rx = FcmReceiverHA()
    rx.creds["entry-a"] = _creds("token-of-a")
    rx.creds["entry-b"] = None

    assert rx.get_fcm_token("entry-b") == "token-of-a"
    assert rx._entry_scoped_fcm_token("entry-b") is None
    assert rx.get_fcm_token("entry-a") == "token-of-a"

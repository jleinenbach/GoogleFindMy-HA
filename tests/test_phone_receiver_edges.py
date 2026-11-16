# tests/test_phone_receiver_edges.py
"""Edge cases of the push receiver's phone path.

Delivery of a push without canonical ID to an open phone request, the argument
checks of ``register_phone_request``, the entry-scoped token lookup and the
readiness steps when the entry has no client. The receiver is real; clients,
supervisor and registration are replaced. Tokens and IDs are invented.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

_ENTRY = "entry-phone-edges"
_TOKEN = "token-of-the-entry"


def _receiver() -> FcmReceiverHA:
    rx = FcmReceiverHA()
    rx.attach_hass(SimpleNamespace(data={}, loop=None))  # type: ignore[arg-type]
    return rx


def _push(request_uuid: str) -> str:
    update = DeviceUpdate_pb2.DeviceUpdate()
    if request_uuid:
        update.fcmMetadata.requestUuid = request_uuid
    return update.SerializeToString().hex()


# --- request id of a push --------------------------------------------------------


def test_unreadable_push_has_no_request_id() -> None:
    assert _receiver()._extract_request_uuid_from_response("zz-not-hex") is None


def test_push_without_request_id_gives_none() -> None:
    assert _receiver()._extract_request_uuid_from_response(_push("")) is None


# --- delivery to an open phone request ----------------------------------------------


@pytest.mark.asyncio
async def test_push_without_request_id_is_not_delivered() -> None:
    rx = _receiver()
    got: list[str] = []
    rx.register_phone_request(_ENTRY, "uuid-open", got.append)

    assert await rx._deliver_phone_push(_ENTRY, _push("")) is False
    assert got == []


@pytest.mark.asyncio
async def test_push_for_another_request_is_not_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rx = _receiver()
    got: list[str] = []
    rx.register_phone_request(_ENTRY, "uuid-open", got.append)
    caplog.set_level(logging.DEBUG)

    assert await rx._deliver_phone_push(_ENTRY, _push("uuid-other")) is False
    assert got == []
    assert "matches no open phone request" in caplog.text


@pytest.mark.asyncio
async def test_failing_callback_still_counts_as_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rx = _receiver()

    def _boom(_hex: str) -> None:
        raise RuntimeError("callback broke")

    rx.register_phone_request(_ENTRY, "uuid-open", _boom)
    caplog.set_level(logging.DEBUG)

    assert await rx._deliver_phone_push(_ENTRY, _push("uuid-open")) is True
    assert "Phone location callback failed" in caplog.text
    assert "delivered to an open phone request" not in caplog.text


# --- argument checks ------------------------------------------------------------


@pytest.mark.parametrize(("entry_id", "request_uuid"), [("", "u"), (_ENTRY, "")])
def test_register_requires_entry_and_request_id(
    entry_id: str, request_uuid: str
) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        _receiver().register_phone_request(entry_id, request_uuid, print)


def test_register_requires_a_callable() -> None:
    with pytest.raises(TypeError, match="callable"):
        _receiver().register_phone_request(_ENTRY, "u", "not callable")  # type: ignore[arg-type]


@pytest.mark.parametrize("entry_id", ["", "   "])
def test_entry_token_needs_an_entry_id(entry_id: str) -> None:
    rx = _receiver()
    rx.creds[_ENTRY] = {"fcm": {"registration": {"token": _TOKEN}}}
    assert rx.get_entry_fcm_token(entry_id) is None


# --- entry-scoped token -------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_token_present_needs_no_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver()
    rx.creds[_ENTRY] = {"fcm": {"registration": {"token": _TOKEN}}}

    async def _register(entry_id: str) -> bool:
        raise AssertionError("must not register")

    monkeypatch.setattr(rx, "_register_for_fcm_entry", _register)
    assert await rx._ensure_entry_scoped_token(_ENTRY) == _TOKEN


@pytest.mark.asyncio
async def test_scoped_token_after_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver()
    rx.creds["entry-other"] = {"fcm": {"registration": {"token": "token-of-other"}}}

    async def _register(entry_id: str) -> bool:
        rx.creds[entry_id] = {"fcm": {"registration": {"token": _TOKEN}}}
        return True

    monkeypatch.setattr(rx, "_register_for_fcm_entry", _register)
    assert await rx._ensure_entry_scoped_token(_ENTRY) == _TOKEN


# --- readiness steps -------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_passes_an_explicit_cache_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver()
    rx.creds[_ENTRY] = {"fcm": {"registration": {"token": _TOKEN}}}
    cache = object()
    seen: list[Any] = []

    async def _listening(
        entry_id: str, got_cache: Any, *, purpose: str, strict_token: bool = False
    ) -> str | None:
        seen.append(got_cache)
        return _TOKEN

    monkeypatch.setattr(rx, "_async_ensure_entry_listening", _listening)
    assert await rx.async_ensure_phone_channel(_ENTRY, cache) is True  # type: ignore[arg-type]
    assert seen == [cache]


@pytest.mark.asyncio
async def test_gate_without_client_fails_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rx = _receiver()
    rx.creds[_ENTRY] = {"fcm": {"registration": {"token": _TOKEN}}}

    async def _no_client(entry_id: str, cache: Any, generation: Any = None) -> None:
        return None

    monkeypatch.setattr(rx, "_ensure_client_for_entry", _no_client)
    caplog.set_level(logging.WARNING)

    assert await rx.async_ensure_phone_channel(_ENTRY) is False
    assert "Phone request readiness failed: client unavailable" in caplog.text
    assert _TOKEN not in caplog.text
    assert rx._entry_last_locate_sent_monotonic == {}

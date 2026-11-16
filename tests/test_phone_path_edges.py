# tests/test_phone_path_edges.py
"""Small edges of the phone modules that the flow tests do not reach.

A failing action observer never costs the request, cancellation passes through,
a receiver without the token getter is not used, the request register tolerates
a push from another thread and a second push, closes unknown requests quietly,
and the status readers drop unreadable fields. The coordinator helpers answer
safely on an instance without their stores. IDs and tokens are invented.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

from custom_components.googlefindmy import phone_identity
from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
    phone_locate,
    phone_sound,
    phone_status,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_action_request import (
    PhoneAction,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_request_registry import (
    PhoneRequestRegistry,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from tests.test_phone_locate_flow import (
    _ENTRY_A,
    _PHONE_A1,
    _TOKEN_A,
    _install_nova,
    _locate,
    _Nova,
    _receiver,
)

_ANDROID_ID = 1_234_567_890_123_456_789
_PHONE = "aphone-" + "b" * 32


def _broken_observer(_number: int, _trigger: str) -> None:
    raise RuntimeError("counter broke")


# --- action observer and cancellation ---------------------------------------------


@pytest.mark.asyncio
async def test_failing_observer_does_not_stop_the_locate_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    nova = _Nova()
    _install_nova(monkeypatch, nova)
    caplog.set_level(logging.DEBUG)

    await _locate(
        rx=rx,
        registry=PhoneRequestRegistry(),
        timeout_s=0.05,
        observer=_broken_observer,
    )
    assert len(nova.calls) == 1
    assert "Phone action observer failed: RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_locate_passes_cancellation_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})

    def _cancel(_scope: str, _kwargs: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    _install_nova(monkeypatch, _Nova(on_call=_cancel))
    registry = PhoneRequestRegistry()
    with pytest.raises(asyncio.CancelledError):
        await _locate(rx=rx, registry=registry)
    assert not registry.is_open(_ENTRY_A, _PHONE_A1)


@pytest.mark.asyncio
async def test_failing_observer_does_not_stop_the_sound_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[str] = []

    async def _wire(_scope: str, payload: str, **_kw: Any) -> str:
        sent.append(payload)
        return "0a00"

    monkeypatch.setattr(phone_sound, "async_nova_request", _wire)
    caplog.set_level(logging.DEBUG)
    result = await phone_sound.async_submit_phone_sound_request(
        PhoneAction.RING,
        _ANDROID_ID,
        _TOKEN_A,
        "uuid-ring",
        entry_id=_ENTRY_A,
        session=None,
        cache=None,
        namespace=_ENTRY_A,
        action_observer=_broken_observer,
    )
    assert result == "0a00"
    assert len(sent) == 1
    assert "Phone action observer failed: RuntimeError" in caplog.text


def test_receiver_without_token_getter_gives_no_token() -> None:
    assert phone_locate._receiver_token(object(), _ENTRY_A) is None


# --- structural protocols declare, they do not implement ---------------------------


@pytest.mark.asyncio
async def test_protocol_stubs_are_declarations_only() -> None:
    stub = cast(Any, None)
    receiver = phone_locate.PhoneLocateReceiver
    assert receiver.get_entry_fcm_token(stub, _ENTRY_A) is None
    assert receiver.register_phone_request(stub, _ENTRY_A, "u", print) is None
    assert receiver.unregister_phone_request(stub, _ENTRY_A, "u") is None
    key_cache = phone_identity._KeyCache
    assert await key_cache.async_get_cached_value(stub, "k") is None
    assert await key_cache.async_set_cached_value(stub, "k", 1) is None


# --- request register ---------------------------------------------------------------


def _open(registry: PhoneRequestRegistry, uuid: str = "u-1") -> Any:
    request = registry.open(
        entry_id=_ENTRY_A,
        request_uuid=uuid,
        phone_id=_PHONE,
        android_id=_ANDROID_ID,
        timeout_s=5.0,
    )
    assert request is not None
    return request


@pytest.mark.asyncio
async def test_push_from_another_thread_reaches_the_request() -> None:
    request = _open(PhoneRequestRegistry())
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, request.deliver, "0a01")
    assert await asyncio.wait_for(request.wait(), 1.0) == "0a01"


@pytest.mark.asyncio
async def test_a_second_push_is_ignored() -> None:
    request = _open(PhoneRequestRegistry())
    request.deliver("0a01")
    request.deliver("0a02")
    assert await request.wait() == "0a01"


def test_closing_an_unknown_request_is_quiet() -> None:
    registry = PhoneRequestRegistry()
    registry.close(_ENTRY_A, "never-opened")
    assert not registry.is_open(_ENTRY_A, _PHONE)


@pytest.mark.asyncio
async def test_closing_keeps_a_newer_request_of_the_same_phone() -> None:
    registry = PhoneRequestRegistry()
    _open(registry, "u-old")
    # The phone slot already names another request (white-box: the register
    # never does this itself, the guard keeps close() from dropping it).
    registry._by_phone[(_ENTRY_A, _PHONE)] = "u-new"
    registry.close(_ENTRY_A, "u-old")
    assert registry._by_phone[(_ENTRY_A, _PHONE)] == "u-new"


def test_push_for_an_unknown_request_is_not_decrypted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert PhoneRequestRegistry().decrypt_push(_ENTRY_A, "u-x", "0a00") is None
    assert "matched no open request" in caplog.text


# --- status fields ------------------------------------------------------------------


def test_battery_record_without_level_gives_none() -> None:
    # Field 2 only (``0x10 0x05``): the record is present, the level is not.
    assert phone_status.battery_from_field(b"\x10\x05") is None


def test_unreadable_wifi_record_gives_none() -> None:
    assert phone_status.ssid_from_field(b"\x80") is None


# --- coordinator helpers on a bare instance ------------------------------------------


def _bare_coordinator() -> Any:
    return cast(Any, GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator))


def test_phone_status_without_store_is_none() -> None:
    assert _bare_coordinator().get_phone_status(_PHONE) is None


def test_phone_wait_without_stamps_is_none() -> None:
    row = {"id": _PHONE, "locate_route": "phone"}
    assert _bare_coordinator()._phone_auto_poll_wait_s(row) is None


def test_status_from_another_thread_hops_onto_the_loop() -> None:
    coordinator = _bare_coordinator()
    hops: list[tuple[Any, ...]] = []
    coordinator._is_on_hass_loop = lambda: False
    coordinator._run_on_hass_loop = lambda *args: hops.append(args)
    coordinator._phone_status = {}
    status = PhoneStatus(battery_pct=50, wifi_ssid=None, observed_at=1.0)

    coordinator._store_phone_status(_PHONE, status)
    assert hops == [(coordinator._store_phone_status, _PHONE, status)]
    assert coordinator._phone_status == {}

# tests/test_phone_api_edges.py
"""Edge cases of the API's phone path that the flow tests do not reach.

The per-entry phone key loaded once, cancellation passed through, the three ways
the push receiver provider can answer, a missing entry scope, a locate answer
without a fix, and a stop whose transport returns nothing. IDs and tokens are
invented.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import (
    GoogleFindMyAPI,
    LocationRequestNotAcceptedError,
)
from custom_components.googlefindmy.const import SoundDispatchOutcome
from tests.test_phone_ring_paths import _ENTRY, _PHONE, _api, _Cache, _Receiver

# --- per-entry phone identity key ---------------------------------------------------


@pytest.mark.asyncio
async def test_phone_key_is_loaded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))
    calls: list[object] = []

    async def _create(cache: object) -> bytes:
        calls.append(cache)
        return b"k" * 32

    monkeypatch.setattr(api, "_decoder_token_cache", object)
    monkeypatch.setattr(api_module, "async_get_or_create_phone_id_key", _create)

    first = await api._async_load_phone_id_key()
    second = await api._async_load_phone_id_key()
    assert first == second == b"k" * 32
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_phone_key_load_passes_cancellation_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))

    async def _cancelled(_cache: object) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(api, "_decoder_token_cache", object)
    monkeypatch.setattr(api_module, "async_get_or_create_phone_id_key", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await api._async_load_phone_id_key()
    assert api._phone_id_key is None


# --- push receiver provider ---------------------------------------------------------


def test_no_receiver_provider_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", None)
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))
    assert api._phone_fcm_receiver(_ENTRY) is None


def test_provider_without_entry_argument_is_called_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = _Receiver()

    def _getter() -> object:
        return receiver

    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", _getter)
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))
    assert api._phone_fcm_receiver(_ENTRY) is receiver


def test_failing_provider_gives_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken(*_a: Any) -> object:
        raise RuntimeError("provider broke")

    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", _broken)
    api = GoogleFindMyAPI(cache=cast(Any, _Cache()))
    caplog.set_level(logging.DEBUG)
    assert api._phone_fcm_receiver(_ENTRY) is None
    assert (
        "FCM receiver provider failed for the phone path: RuntimeError" in caplog.text
    )


# --- refusals before anything is built ----------------------------------------------


@pytest.mark.asyncio
async def test_readiness_check_passes_cancellation_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CancellingReceiver(_Receiver):
        async def async_ensure_phone_channel(
            self, entry_id: str, cache: object = None
        ) -> bool:
            raise asyncio.CancelledError

    api = _api(monkeypatch, _CancellingReceiver())
    with pytest.raises(asyncio.CancelledError):
        await api._async_prepare_phone_request(_PHONE)


@pytest.mark.asyncio
async def test_no_entry_scope_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(monkeypatch, _Receiver())

    class _NoScope(_Cache):
        entry_id = ""

    api._cache = cast(Any, _NoScope())
    with pytest.raises(LocationRequestNotAcceptedError) as err:
        await api._async_prepare_phone_request(_PHONE)
    assert err.value.stage == "phone_no_entry_scope"


@pytest.mark.asyncio
async def test_no_receiver_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(monkeypatch, _Receiver())
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda *_a: None)
    with pytest.raises(LocationRequestNotAcceptedError) as err:
        await api._async_prepare_phone_request(_PHONE)
    assert err.value.stage == "phone_receiver_unavailable"


# --- answers ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_without_a_fix_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(monkeypatch, _Receiver())

    async def _no_records(_device_id: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(api, "_async_get_phone_location", _no_records)
    assert await api.async_get_device_location(_PHONE, "Phone") == {}


@pytest.mark.asyncio
async def test_stop_with_an_empty_transport_answer_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _api(monkeypatch, _Receiver())

    async def _nothing(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(api_module, "async_submit_phone_sound_request", _nothing)
    caplog.set_level(logging.ERROR)
    outcome = await api.async_stop_sound(_PHONE)
    assert outcome is SoundDispatchOutcome.INTERNAL_ERROR
    assert "empty response from server" in caplog.text

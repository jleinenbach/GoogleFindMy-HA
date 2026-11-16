# tests/test_phone_locate_flow.py
"""Locate flow for a phone listed without a canonical ID.

Register of open requests with per-request keys, the push switch in the FCM
receiver (``requestUuid`` on the entry's channel), the phone branch of
``GoogleFindMyAPI``, ``max_retries`` on the transport, and the wire-level lock on
the send path. Everything runs against doubles: the Nova transport is replaced
or fed a stub session, the receiver is the real ``FcmReceiverHA`` without a
client, pushes are built from the vendored protobuf classes and encrypted with
``cryptography``.

The phone modules are looked up at run time with ``pytest.fail`` instead of
an import, so that on the state before the feature this module is collected and
fails test by test rather than erroring at collection.

Privacy: the Android ID and all names here are invented
(``_ANDROID_ID = 0x1122334455667788``). No record of these tests may carry an
Android ID (decimal or hex), a key, a payload or coordinates; the log hygiene
test at the end checks that for the success and the failure path.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import inspect
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    PHONE_ID_PREFIX,
    PHONE_MAX_AUTH_RESENDS,
)
from custom_components.googlefindmy.coordinator.cache import CacheOperations
from custom_components.googlefindmy.coordinator.helpers import locate_clock
from custom_components.googlefindmy.coordinator.helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK_M,
)
from custom_components.googlefindmy.NovaApi import nova_request as nova_module
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
    create_location_request,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaError,
    NovaHTTPError,
    NovaRateLimitError,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from tests.test_coordinator_automatic_polling import (
    _make_coordinator,
    _ShiftedTime,
    polling_module,
)
from tests.test_nova_request import _DummyResponse, _DummySession, _StubCache

pytestmark = pytest.mark.asyncio

# Invented values only (rule 8 of the work package).
_ANDROID_ID = 0x1122334455667788
_ANDROID_ID_B = 0x2233445566778899
_ENTRY_A = "entry-a"
_ENTRY_B = "entry-b"
_PHONE_A1 = PHONE_ID_PREFIX + "a1" * 16
_PHONE_A2 = PHONE_ID_PREFIX + "a2" * 16
_PHONE_B1 = PHONE_ID_PREFIX + "b1" * 16
_TOKEN_A = "push-token-of-a"
_TOKEN_B = "push-token-of-b"
_FIX_SECONDS = 1_800_000_000
# Stamps the cache judges must lie in the past (it rejects a far-future stamp).
_CACHE_T0 = 1_700_000_000.0
_LAT, _LON, _ACC = 48.1234, 11.5678, 11.5
_PAYLOAD_KEY = "com.google.android.apps.adm.FCM_PAYLOAD"


def _mod(name: str) -> Any:
    """Return a phone module or fail the test: the feature is not there yet."""
    try:
        return importlib.import_module(f"custom_components.googlefindmy.{name}")
    except ImportError as err:  # pragma: no cover - only before the phone modules exist
        pytest.fail(f"phone module missing: {name} ({err})")


def _require(owner: Any, name: str) -> Any:
    value = getattr(owner, name, None)
    if value is None:
        pytest.fail(f"{getattr(owner, '__name__', owner)!s} has no {name}")
    return value


def _phone_locate() -> Any:
    return _mod("NovaApi.ExecuteAction.LocatePhone.phone_locate")


def _registry_module() -> Any:
    return _mod("NovaApi.ExecuteAction.LocatePhone.phone_request_registry")


def _guard_module() -> Any:
    return _mod("NovaApi.ExecuteAction.LocatePhone.phone_payload_guard")


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _decode_request(payload_hex: str) -> Any:
    request = DeviceUpdate_pb2.ExecuteActionRequest()
    request.ParseFromString(bytes.fromhex(payload_hex))
    return request


def _request_parts(payload_hex: str) -> tuple[str, bytes, int]:
    """Return ``(request_uuid, locate_key, android_id)`` of a locate payload."""
    request = cast(Any, _decode_request(payload_hex))
    return (
        request.requestMetadata.requestUuid,
        bytes(request.action.phoneActionField4.phoneLocateField1),
        int(request.scope.phone.phoneInformationField1),
    )


def _raw_fields(data: bytes) -> list[tuple[int, int]]:
    """Field numbers and wire types of a message, read without the schema."""
    out: list[tuple[int, int]] = []
    pos = 0
    while pos < len(data):
        tag = 0
        shift = 0
        while True:
            byte = data[pos]
            pos += 1
            tag |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        number, wire_type = tag >> 3, tag & 7
        out.append((number, wire_type))
        if wire_type == 0:
            while data[pos] & 0x80:
                pos += 1
            pos += 1
        elif wire_type == 1:
            pos += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            while True:
                byte = data[pos]
                pos += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            pos += length
        elif wire_type == 5:
            pos += 4
        else:  # pragma: no cover - a group would be a defect of the builder
            raise AssertionError(f"unexpected wire type {wire_type}")
    return out


def _raw_action_numbers(payload_hex: str) -> set[int]:
    """Field numbers of the action message, read raw from the serialized bytes."""
    request = cast(Any, _decode_request(payload_hex))
    return {number for number, _ in _raw_fields(request.action.SerializeToString())}


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _ld(number: int, content: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(content)) + content


def _phone_request_with_raw_action(action: bytes) -> str:
    """Hex ``ExecuteActionRequest`` with the phone scope and a hand-made action."""
    scope = _ld(1, _varint(8) + _varint(_ANDROID_ID)) + _varint((2 << 3) | 0) + b"\x01"
    metadata = _ld(2, b"uuid-1") + _ld(4, _ld(1, b"token"))
    return (_ld(1, scope) + _ld(2, action) + _ld(3, metadata)).hex()


def _plaintext(lat: float, lon: float, acc: float | None) -> bytes:
    plain = DeviceUpdate_pb2.PhoneLocationPlaintext()
    plain.position.latitude = lat
    plain.position.longitude = lon
    if acc is not None:
        plain.accuracyMeters = acc
    return plain.SerializeToString()


def _envelope(
    key: bytes,
    *,
    lat: float = _LAT,
    lon: float = _LON,
    acc: float | None = _ACC,
    seconds: int = _FIX_SECONDS,
) -> bytes:
    iv = bytes(range(16, 32))
    padder = padding.PKCS7(128).padder()
    padded = padder.update(_plaintext(lat, lon, acc)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    cipher = DeviceUpdate_pb2.PhoneLocationCipher(ciphertext=ciphertext, iv=iv)
    envelope = DeviceUpdate_pb2.PhoneLocationEnvelope()
    envelope.encrypted = cipher.SerializeToString()
    envelope.time = Common_pb2.Time(seconds=seconds, nanos=0).SerializeToString()
    return envelope.SerializeToString()


def _push_hex(request_uuid: str, android_id: int, envelope: bytes | None) -> str:
    """Hex ``DeviceUpdate`` as the phone sends it: no canonical id, field 36 set."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    update.fcmMetadata.requestUuid = request_uuid
    ident = update.deviceMetadata.identifierInformation
    ident.type = DeviceUpdate_pb2.IDENTIFIER_ANDROID
    ident.phoneInformation.phoneInformationField1 = android_id
    record = DeviceUpdate_pb2.DeviceMetadata.PhoneInformation()
    if envelope is not None:
        record.phoneInformationField36 = envelope
    update.deviceMetadata.phoneInformation = record.SerializeToString()
    return update.SerializeToString().hex()


def _fcm_payload(push_hex: str) -> dict[str, Any]:
    raw = base64.b64encode(bytes.fromhex(push_hex)).decode()
    return {"data": {_PAYLOAD_KEY: raw}}


async def _push(rx: FcmReceiverHA, entry_id: str, push_hex: str) -> None:
    await rx._handle_notification_async(entry_id, _fcm_payload(push_hex))


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _receiver(tokens: dict[str, str]) -> FcmReceiverHA:
    """Real receiver without clients; entry tokens from the stored creds."""
    rx = FcmReceiverHA()
    rx.attach_hass(SimpleNamespace(data={}, loop=None))  # type: ignore[arg-type]
    for entry_id, token in tokens.items():
        rx.creds[entry_id] = {"fcm": {"registration": {"token": token}}}

    async def _channel_ready(entry_id: str, cache: object = None) -> bool:
        # Readiness gate answered as "listening" for the entries that
        # have a token; the gate itself is tested in test_phone_ring_paths.py.
        del cache
        return entry_id in tokens

    rx.async_ensure_phone_channel = _channel_ready  # type: ignore[method-assign]
    return rx


class _Nova:
    """Replacement for ``async_nova_request`` inside ``phone_locate``.

    Records every call; ``on_call`` may raise, deliver a push or wait.
    """

    def __init__(
        self, on_call: Callable[[str, dict[str, Any]], Any] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_call = on_call

    async def __call__(self, scope: str, payload: str, **kwargs: Any) -> str:
        assert scope == "nbe_execute_action"
        self.calls.append((payload, kwargs))
        if self.on_call is not None:
            result = self.on_call(payload, kwargs)
            if inspect.isawaitable(result):
                await result
        return ""


def _install_nova(monkeypatch: pytest.MonkeyPatch, nova: _Nova) -> None:
    monkeypatch.setattr(_phone_locate(), "async_nova_request", nova)


class _Cache:
    entry_id: str | None

    def __init__(self, entry_id: str | None = _ENTRY_A) -> None:
        self.entry_id = entry_id

    async def async_get_cached_value(self, _key: str) -> Any:
        return None

    async def async_set_cached_value(self, _key: str, _value: Any) -> None:
        return None


async def _locate(
    *,
    rx: FcmReceiverHA,
    registry: Any,
    phone_id: str = _PHONE_A1,
    android_id: int = _ANDROID_ID,
    entry_id: str = _ENTRY_A,
    timeout_s: float = 2.0,
    observer: Any = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = await _phone_locate().locate_phone(
        phone_id=phone_id,
        android_id=android_id,
        entry_id=entry_id,
        receiver=rx,
        registry=registry,
        session=None,
        cache=_Cache(entry_id),
        namespace=entry_id,
        timeout_s=timeout_s,
        action_observer=observer,
    )
    return result


def _answering_nova(rx: FcmReceiverHA, entry_id: str = _ENTRY_A, **fix: Any) -> _Nova:
    """A transport that answers each POST with the matching push on ``entry_id``."""

    async def _answer(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, android_id = _request_parts(payload)
        await _push(
            rx, entry_id, _push_hex(request_uuid, android_id, _envelope(key, **fix))
        )

    return _Nova(_answer)


# ---------------------------------------------------------------------------
# Action whitelist and the lock on the send path (run-time byte check)
# ---------------------------------------------------------------------------


async def test_av_phone_locate_sends_only_action_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every payload the phone path hands to the transport carries action {4}, never 30."""
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    nova = _answering_nova(rx)
    _install_nova(monkeypatch, nova)

    rows = await _locate(rx=rx, registry=_registry_module().PhoneRequestRegistry())

    assert len(rows) == 1
    assert len(nova.calls) == 1
    payload, kwargs = nova.calls[0]
    request = cast(Any, _decode_request(payload))
    assert {f.number for f, _ in request.action.ListFields()} == {4}
    assert _raw_action_numbers(payload) == {4}
    assert 30 not in _raw_action_numbers(payload)
    assert request.scope.type == DeviceUpdate_pb2.DeviceType.ANDROID_DEVICE
    assert int(request.scope.phone.phoneInformationField1) == _ANDROID_ID
    assert request.requestMetadata.gcmRegistrationId.id == _TOKEN_A
    assert kwargs["max_retries"] == 0
    # At most one resend after a 401.
    assert kwargs["max_auth_resends"] == PHONE_MAX_AUTH_RESENDS == 1


async def test_av_api_phone_branch_sends_only_action_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through ``GoogleFindMyAPI`` the same holds: {4} on the wire, the row comes back."""
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    nova = _answering_nova(rx)
    _install_nova(monkeypatch, nova)
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    api = GoogleFindMyAPI(cache=_Cache(_ENTRY_A))
    _require(api, "_phone_targets")[_PHONE_A1] = _ANDROID_ID

    best = await api.async_get_device_location(_PHONE_A1, "Phone A1")

    assert best["latitude"] == pytest.approx(_LAT)
    assert best["longitude"] == pytest.approx(_LON)
    assert best["_decrypt_proven"] is False
    assert [_raw_action_numbers(p) for p, _ in nova.calls] == [{4}]


@pytest.mark.parametrize("number", [2, 6, 7, 13, 14, 30, 31, 32])
async def test_lock_refuses_every_other_action_for_a_phone(number: int) -> None:
    guard = _guard_module()
    payload = _phone_request_with_raw_action(_ld(number, b""))
    with pytest.raises(guard.PhoneActionNotAllowedError):
        guard.check_phone_action_payload(payload)


async def test_lock_refuses_two_fields_and_wrong_content() -> None:
    guard = _guard_module()
    key = bytes(16)
    cases = {
        "two allowed fields": _ld(1, b"") + _ld(5, b""),
        "4 next to 1": _ld(4, _ld(1, key)) + _ld(1, b""),
        "locate without key": _ld(4, b""),
        "locate with 15-byte key": _ld(4, _ld(1, key[:15])),
        "locate with a second inner field": _ld(4, _ld(1, key) + _ld(2, b"x")),
        "ring with content": _ld(1, b"\x08\x01"),
        "no action at all": b"",
        "varint action": _varint((4 << 3) | 0) + b"\x01",
    }
    for label, action in cases.items():
        with pytest.raises(guard.PhoneActionNotAllowedError, match=r".+"):
            guard.check_phone_action_payload(_phone_request_with_raw_action(action))
        assert label
    with pytest.raises(guard.PhoneActionNotAllowedError):
        guard.check_phone_action_payload("zz")


async def test_lock_passes_the_builders_three_actions_and_ignores_trackers() -> None:
    guard = _guard_module()
    builder = _mod("NovaApi.ExecuteAction.LocatePhone.phone_action_request")
    for action, key in ((1, None), (4, bytes(range(16))), (5, None)):
        payload = builder.build_phone_action_request(
            _ANDROID_ID, action, _TOKEN_A, "uuid-x", locate_key=key
        )
        assert guard.check_phone_action_payload(payload) is True
    tracker = create_location_request("tracker-canonic-id", _TOKEN_A, "uuid-y")
    assert guard.check_phone_action_payload(tracker) is False


async def test_lock_sits_inside_async_nova_request_before_any_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport itself refuses a phone payload with action 30: no POST happens."""
    guard = _guard_module()
    session = _DummySession([_DummyResponse(200, b"")])
    monkeypatch.setattr(nova_module, "async_get_adm_token_api", _fake_adm_token)
    payload = _phone_request_with_raw_action(_ld(30, b""))

    with pytest.raises(guard.PhoneActionNotAllowedError):
        await nova_module.async_nova_request(
            "nbe_execute_action",
            payload,
            username="user@example.com",
            cache=_StubCache(),
            session=session,
        )

    assert session.calls == []
    # A tracker payload on the same scope is not judged and goes out.
    await nova_module.async_nova_request(
        "nbe_execute_action",
        create_location_request("tracker-canonic-id", _TOKEN_A, "uuid-y"),
        username="user@example.com",
        cache=_StubCache(),
        session=session,
    )
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# One key per request, one open request per phone, right delivery
# ---------------------------------------------------------------------------


async def test_three_interleaved_requests_land_at_the_right_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two accounts, two phones, answers in reverse order: each fix reaches its request."""
    rx = _receiver({_ENTRY_A: _TOKEN_A, _ENTRY_B: _TOKEN_B})
    registry_a = _registry_module().PhoneRequestRegistry()
    registry_b = _registry_module().PhoneRequestRegistry()
    posted = asyncio.Event()
    release = asyncio.Event()

    async def _hold(_payload: str, _kwargs: dict[str, Any]) -> None:
        if len(nova.calls) == 3:
            posted.set()
        await release.wait()

    nova = _Nova(_hold)
    _install_nova(monkeypatch, nova)

    plan = [
        (registry_a, _PHONE_A1, _ANDROID_ID, _ENTRY_A, 10.0),
        (registry_a, _PHONE_A2, _ANDROID_ID_B, _ENTRY_A, 20.0),
        (registry_b, _PHONE_B1, _ANDROID_ID, _ENTRY_B, 30.0),
    ]
    tasks = [
        asyncio.create_task(
            _locate(rx=rx, registry=reg, phone_id=pid, android_id=aid, entry_id=eid)
        )
        for reg, pid, aid, eid, _lat in plan
    ]
    await asyncio.wait_for(posted.wait(), timeout=5)
    assert len(registry_a) == 2
    assert len(registry_b) == 1
    keys = [_request_parts(p)[1] for p, _ in nova.calls]
    assert len({*keys}) == 3  # three requests, three different keys
    release.set()
    await asyncio.sleep(0)

    for (payload, _kw), (_reg, _pid, aid, eid, lat) in reversed(
        list(zip(nova.calls, plan, strict=True))
    ):
        request_uuid, key, android_id = _request_parts(payload)
        assert android_id == aid
        await _push(rx, eid, _push_hex(request_uuid, aid, _envelope(key, lat=lat)))

    results = await asyncio.gather(*tasks)
    assert [r[0]["latitude"] for r in results] == pytest.approx([10.0, 20.0, 30.0])
    assert len(registry_a) == 0
    assert len(registry_b) == 0


async def test_second_request_on_an_open_phone_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    release = asyncio.Event()

    async def _hold(_payload: str, _kwargs: dict[str, Any]) -> None:
        await release.wait()

    nova = _Nova(_hold)
    _install_nova(monkeypatch, nova)
    first = asyncio.create_task(_locate(rx=rx, registry=registry))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(registry) == 1

    # Bounded: a register that admitted the second request would make it wait for
    # the held transport forever; the bound turns that mutant into a failure.
    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await asyncio.wait_for(_locate(rx=rx, registry=registry), timeout=2)

    assert info.value.stage == "phone_request_open"
    assert len(nova.calls) == 1  # exactly one POST
    assert len(registry) == 1  # exactly one key
    release.set()
    request_uuid, key, _aid = _request_parts(nova.calls[0][0])
    await _push(rx, _ENTRY_A, _push_hex(request_uuid, _ANDROID_ID, _envelope(key)))
    rows = await first
    assert rows[0]["latitude"] == pytest.approx(_LAT)


async def test_key_is_fresh_per_request_and_never_caller_supplied() -> None:
    registry = _registry_module().PhoneRequestRegistry()
    assert "key" not in inspect.signature(registry.open).parameters
    assert "locate_key" not in inspect.signature(registry.open).parameters
    first = registry.open(
        entry_id=_ENTRY_A,
        request_uuid="u1",
        phone_id=_PHONE_A1,
        android_id=_ANDROID_ID,
        timeout_s=1,
    )
    second = registry.open(
        entry_id=_ENTRY_A,
        request_uuid="u2",
        phone_id=_PHONE_A2,
        android_id=_ANDROID_ID_B,
        timeout_s=1,
    )
    assert first is not None and second is not None
    assert len(first.key) == 16
    assert first.key != second.key
    assert "key" not in repr(first) and first.key.hex() not in repr(first)
    assert str(_ANDROID_ID) not in repr(first)
    registry.close(_ENTRY_A, "u1")
    registry.close(_ENTRY_A, "u2")


# ---------------------------------------------------------------------------
# The register is empty after the answer and after the timeout
# ---------------------------------------------------------------------------


async def test_register_is_empty_after_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    _install_nova(monkeypatch, _answering_nova(rx))

    rows = await _locate(rx=rx, registry=registry)

    assert rows[0]["last_seen"] == pytest.approx(_FIX_SECONDS)
    assert len(registry) == 0
    assert registry.is_open(_ENTRY_A, _PHONE_A1) is False
    assert rx._phone_request_callbacks == {}


async def test_register_is_empty_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    nova = _Nova()
    _install_nova(monkeypatch, nova)

    rows = await _locate(rx=rx, registry=registry, timeout_s=0.01)

    assert rows == []
    assert len(registry) == 0
    assert rx._phone_request_callbacks == {}
    assert len(nova.calls) == 1


# ---------------------------------------------------------------------------
# No repeat after a failure; max_retries=0; default stays 6
# ---------------------------------------------------------------------------


async def _fake_adm_token(
    username: str | None = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
    cache: Any,
) -> str:
    return "resolved-token"


async def _instant_sleep(_delay: float) -> None:
    return None


async def test_max_retries_zero_posts_exactly_once_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nova_module, "async_get_adm_token_api", _fake_adm_token)
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    session = _DummySession([_DummyResponse(500, b"boom") for _ in range(10)])
    if (
        "max_retries"
        not in inspect.signature(nova_module.async_nova_request).parameters
    ):
        pytest.fail("async_nova_request has no parameter max_retries")

    with pytest.raises(NovaHTTPError) as info:
        await nova_module.async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=_StubCache(),
            session=session,
            max_retries=0,
        )

    assert info.value.status == 500
    assert len(session.calls) == 1
    assert "after 1 attempts" in str(info.value)


async def test_regression_tracker_retry_default_stays_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert nova_module.NOVA_MAX_RETRIES == 6
    params = inspect.signature(nova_module.async_nova_request).parameters
    assert params["max_retries"].default == 6
    monkeypatch.setattr(nova_module, "async_get_adm_token_api", _fake_adm_token)
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    session = _DummySession([_DummyResponse(500, b"boom") for _ in range(10)])
    with pytest.raises(NovaHTTPError):
        await nova_module.async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=_StubCache(),
            session=session,
        )
    assert len(session.calls) == 7


async def test_http_500_one_post_one_record_old_location_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()

    def _server_error(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise NovaHTTPError(500, "Nova API failed after 1 attempts")

    nova = _Nova(_server_error)
    _install_nova(monkeypatch, nova)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await _locate(rx=rx, registry=registry)

    assert info.value.stage == "phone_server_error"
    assert info.value.status == 500
    assert len(nova.calls) == 1
    assert len(registry) == 0
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "entry-a" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "phone_server_error" in warnings[0].getMessage()


async def test_timeout_returns_empty_without_a_second_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    nova = _Nova()
    _install_nova(monkeypatch, nova)

    rows = await _locate(
        rx=rx, registry=_registry_module().PhoneRequestRegistry(), timeout_s=0.01
    )

    assert rows == []
    assert len(nova.calls) == 1


async def test_next_automatic_request_only_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed phone request is not repeated in the cycle; the next one waits for the interval."""
    phone = {"id": _PHONE_A1, "name": "Phone A1", "locate_route": "phone"}

    class _FailingAPI:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def is_push_ready(self) -> bool:
            return True

        async def async_get_device_location(self, dev_id: str, _name: str) -> Any:
            self.calls.append(dev_id)
            nova_module._notify_request_dispatch()
            raise LocationRequestNotAcceptedError(
                stage="phone_server_error", status=500
            )

    api = _FailingAPI()
    coord = _make_coordinator(monkeypatch, options={}, api=cast(Any, api))
    coord._last_device_list = [dict(phone)]
    stored = {"last_seen": time.time() - 600, "latitude": 1.0, "longitude": 2.0}
    coord._device_location_data[_PHONE_A1] = dict(stored)
    coord._last_poll_mono = time.monotonic() - 1000.0

    await coord._async_update_data()
    await _drain_background(coord)
    assert api.calls == [_PHONE_A1]
    assert coord._device_location_data[_PHONE_A1] == stored

    # Immediately again: not due, nothing is sent.
    await coord._async_update_data()
    await _drain_background(coord)
    assert api.calls == [_PHONE_A1]

    # After the account interval (200 s here) a phone still waits: the floor puts
    # the floor PHONE_MIN_AUTO_POLL_INTERVAL_S (900 s) in front of it.
    with patch.object(polling_module, "time", _ShiftedTime(250.0)):
        await coord._async_update_data()
        await _drain_background(coord)
    assert api.calls == [_PHONE_A1]

    # After the effective interval (900 s for a phone) the next request goes out.
    coord._last_list_poll_mono += 950.0
    with patch.object(polling_module, "time", _ShiftedTime(950.0)):
        await coord._async_update_data()
        await _drain_background(coord)
    assert api.calls == [_PHONE_A1, _PHONE_A1]
    assert coord._device_location_data[_PHONE_A1] == stored


async def _drain_background(coord: Any) -> None:
    tasks = list(coord.hass.background)
    if tasks:
        await asyncio.gather(*tasks)
        coord.hass.background.clear()
    save_task = getattr(coord, "_stats_save_task", None)
    if save_task is not None and not save_task.done():
        save_task.cancel()
        try:
            await save_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# End to end: age rule and accuracy fallback in the cache
# ---------------------------------------------------------------------------


def _cache_coordinator() -> Any:
    coord = MagicMock()
    coord._device_location_data = {}
    coord.increment_stat = MagicMock()
    return coord


def _row(fix: Any) -> dict[str, Any]:
    row: dict[str, Any] = _phone_locate().phone_location_row(fix, phone_id=_PHONE_A1)
    return row


async def test_older_push_does_not_replace_the_stored_location() -> None:
    crypto = _mod("NovaApi.ExecuteAction.LocatePhone.phone_location_crypto")
    coord = _cache_coordinator()
    newer = _row(
        crypto.PhoneFix(
            latitude=1.0, longitude=2.0, timestamp=_CACHE_T0 + 10.0, accuracy=5.0
        )
    )
    older = _row(
        crypto.PhoneFix(latitude=3.0, longitude=4.0, timestamp=_CACHE_T0, accuracy=5.0)
    )
    coord._device_location_data[_PHONE_A1] = dict(newer)

    assert (
        CacheOperations._is_significant_update(coord, _PHONE_A1, dict(older)) is False
    )
    same_or_newer = _row(
        crypto.PhoneFix(
            latitude=3.0, longitude=4.0, timestamp=_CACHE_T0 + 11.0, accuracy=5.0
        )
    )
    assert (
        CacheOperations._is_significant_update(coord, _PHONE_A1, same_or_newer) is True
    )


async def test_accuracy_unset_by_the_phone_path_gets_the_cache_fallback() -> None:
    crypto = _mod("NovaApi.ExecuteAction.LocatePhone.phone_location_crypto")
    coord = _cache_coordinator()
    without = _row(
        crypto.PhoneFix(latitude=1.0, longitude=2.0, timestamp=_CACHE_T0, accuracy=None)
    )
    assert without["accuracy"] is None
    assert "accuracy_estimated" not in without

    assert CacheOperations._is_significant_update(coord, _PHONE_A1, without) is True
    assert without["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert without["accuracy_estimated"] is True

    measured = _row(
        crypto.PhoneFix(latitude=1.0, longitude=2.0, timestamp=_CACHE_T0, accuracy=11.5)
    )
    assert CacheOperations._is_significant_update(coord, _PHONE_A1, measured) is True
    assert measured["accuracy"] == pytest.approx(11.5)
    assert measured["accuracy_estimated"] is False


async def test_fix_without_accuracy_flows_through_the_whole_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    _install_nova(monkeypatch, _answering_nova(rx, acc=None))
    rows = await _locate(rx=rx, registry=_registry_module().PhoneRequestRegistry())
    assert rows[0]["accuracy"] is None
    assert rows[0]["is_own_report"] is True


# ---------------------------------------------------------------------------
# Account separation on the push channel and the token
# ---------------------------------------------------------------------------


async def test_push_on_channel_b_with_uuid_of_a_is_not_decrypted_for_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A, _ENTRY_B: _TOKEN_B})
    registry = _registry_module().PhoneRequestRegistry()
    captured: list[str] = []

    async def _wrong_channel(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, android_id = _request_parts(payload)
        captured.append(request_uuid)
        await _push(rx, _ENTRY_B, _push_hex(request_uuid, android_id, _envelope(key)))

    _install_nova(monkeypatch, _Nova(_wrong_channel))

    rows = await _locate(rx=rx, registry=registry, timeout_s=0.05)

    assert rows == []  # A's request timed out: the push on B's channel did not reach it
    assert len(registry) == 0
    assert captured

    # Control: the same push on A's channel resolves A's request.
    async def _right_channel(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, android_id = _request_parts(payload)
        await _push(rx, _ENTRY_A, _push_hex(request_uuid, android_id, _envelope(key)))

    _install_nova(monkeypatch, _Nova(_right_channel))
    rows = await _locate(rx=rx, registry=registry, timeout_s=0.5)
    assert rows[0]["latitude"] == pytest.approx(_LAT)


async def test_no_entry_token_means_no_post_even_if_another_entry_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback to a foreign entry's token."""
    rx = _receiver({_ENTRY_B: _TOKEN_B})  # A has none, B has one
    assert rx.get_fcm_token(_ENTRY_A) == _TOKEN_B  # the legacy getter would fall back
    assert _require(rx, "get_entry_fcm_token")(_ENTRY_A) is None
    nova = _Nova()
    _install_nova(monkeypatch, nova)

    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await _locate(rx=rx, registry=_registry_module().PhoneRequestRegistry())

    assert info.value.stage == "phone_no_fcm_token"
    assert nova.calls == []


async def test_push_naming_another_android_id_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Android ID in the push must equal the one of the request."""
    rx = _receiver({_ENTRY_A: _TOKEN_A})

    async def _other_phone(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, _android_id = _request_parts(payload)
        await _push(
            rx, _ENTRY_A, _push_hex(request_uuid, _ANDROID_ID_B, _envelope(key))
        )

    _install_nova(monkeypatch, _Nova(_other_phone))
    rows = await _locate(rx=rx, registry=_registry_module().PhoneRequestRegistry())
    assert rows == []


# ---------------------------------------------------------------------------
# Race: the push arrives before the transport returns
# ---------------------------------------------------------------------------


async def test_push_before_the_post_returns_still_yields_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    seen_open: list[int] = []

    async def _push_first(payload: str, _kwargs: dict[str, Any]) -> None:
        seen_open.append(len(registry))
        request_uuid, key, android_id = _request_parts(payload)
        await _push(rx, _ENTRY_A, _push_hex(request_uuid, android_id, _envelope(key)))
        await asyncio.sleep(0.02)  # the POST "returns" after the push

    _install_nova(monkeypatch, _Nova(_push_first))

    rows = await _locate(rx=rx, registry=registry)

    assert seen_open == [1]  # the register entry exists while the POST is in flight
    assert rows[0]["latitude"] == pytest.approx(_LAT)


# ---------------------------------------------------------------------------
# Data starvation watchdog and dead-token detector stay untouched
# ---------------------------------------------------------------------------


async def test_phone_timeout_does_not_feed_starvation_or_dead_token_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    noted: list[Any] = []
    monkeypatch.setattr(
        rx, "note_accepted_without_push", lambda *a, **k: noted.append((a, k))
    )
    _install_nova(monkeypatch, _Nova())

    rows = await _locate(
        rx=rx, registry=_registry_module().PhoneRequestRegistry(), timeout_s=0.01
    )

    assert rows == []
    assert noted == []
    assert _ENTRY_A not in rx._entry_last_locate_sent_monotonic

    # A pure phone account with a timed-out request is not starved ...
    now = time.monotonic()
    rx.pcs[_ENTRY_A] = cast(Any, object())
    monkeypatch.setattr(rx, "_is_started", lambda _pc: True)
    monkeypatch.setattr(rx, "_session_age_s", lambda _pc: 1000.0)
    rx._entry_last_activity_monotonic[_ENTRY_A] = now - 1.0
    assert rx._is_data_starved(_ENTRY_A, now) is False
    # ... while the same state with a tracker locate stamp would be (control).
    rx._entry_last_locate_sent_monotonic[_ENTRY_A] = now - 100_000.0
    assert rx._is_data_starved(_ENTRY_A, now) is True


# ---------------------------------------------------------------------------
# Acceptance contract
# ---------------------------------------------------------------------------


def _wrapped_network_failure() -> NovaError:
    """What the transport raises for a network failure once its attempts are used up."""
    try:
        raise aiohttp.ClientConnectionError("down")
    except aiohttp.ClientConnectionError as cause:
        wrapped = NovaError("Nova API request failed after retries: down")
        wrapped.__cause__ = cause
        return wrapped


@pytest.mark.parametrize(
    ("raised", "stage", "status"),
    [
        (NovaRateLimitError("rate limited"), "phone_rate_limited", 429),
        (NovaHTTPError(503, "unavailable"), "phone_server_error", 503),
        (aiohttp.ClientConnectionError("down"), "phone_network_error", None),
        (_wrapped_network_failure(), "phone_network_error", None),
        (
            NovaError("Nova API request failed after retries"),
            "phone_request_failed",
            None,
        ),
        (ValueError("phone action 30 is not allowed"), "phone_request_failed", None),
    ],
)
async def test_acceptance_contract_maps_each_failure_to_its_own_stage(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, stage: str, status: int | None
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()

    def _raise(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise raised

    _install_nova(monkeypatch, _Nova(_raise))
    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await _locate(rx=rx, registry=registry)
    assert info.value.stage == stage
    assert info.value.status == status
    assert stage in _phone_locate().PHONE_LOCATE_NOT_ACCEPTED_STAGES
    assert len(registry) == 0
    assert rx._phone_request_callbacks == {}


async def test_acceptance_contract_passes_nova_auth_error_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    auth = NovaAuthError(403, "forbidden")

    def _raise(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise auth

    _install_nova(monkeypatch, _Nova(_raise))
    with pytest.raises(NovaAuthError) as info:
        await _locate(rx=rx, registry=registry)
    assert info.value is auth
    assert len(registry) == 0


async def test_unexpected_error_propagates_but_closes_the_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broad handler: an unexpected error is not disguised as a refused request."""
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()

    def _raise(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise RuntimeError("odd")

    _install_nova(monkeypatch, _Nova(_raise))
    with pytest.raises(RuntimeError):
        await _locate(rx=rx, registry=registry)
    assert len(registry) == 0
    assert rx._phone_request_callbacks == {}


async def test_acceptance_contract_receiver_without_phone_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nova = _Nova()
    _install_nova(monkeypatch, nova)
    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await _locate(
            rx=cast(Any, object()), registry=_registry_module().PhoneRequestRegistry()
        )
    assert info.value.stage == "phone_receiver_unavailable"
    assert nova.calls == []


# ---------------------------------------------------------------------------
# API layer: fail closed without a target, tracker path untouched
# ---------------------------------------------------------------------------


async def test_api_phone_without_target_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    nova = _Nova()
    _install_nova(monkeypatch, nova)
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    api = GoogleFindMyAPI(cache=_Cache(_ENTRY_A))

    with pytest.raises(LocationRequestNotAcceptedError) as info:
        await api.async_get_device_location(PHONE_ID_PREFIX + "0" * 32, "Unknown phone")

    assert info.value.stage == "phone_target_unknown"
    assert nova.calls == []
    assert rx._phone_request_callbacks == {}


async def test_api_tracker_ids_keep_the_tracker_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def _tracker(
        device_id: str, _name: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        seen.append(device_id)
        return []

    monkeypatch.setattr(api_module, "get_location_data_for_device", _tracker)
    nova = _Nova()
    _install_nova(monkeypatch, nova)
    api = GoogleFindMyAPI(cache=_Cache(_ENTRY_A))
    _require(api, "_phone_targets")[_PHONE_A1] = _ANDROID_ID

    assert await api.async_get_device_location("tracker-canonic-id", "Tag") == {}
    assert seen == ["tracker-canonic-id"]
    assert nova.calls == []


# ---------------------------------------------------------------------------
# Measurement channel: DEBUG line and the coordinator counter
# ---------------------------------------------------------------------------


async def test_measurement_channel_names_action_trigger_and_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    _install_nova(monkeypatch, _answering_nova(rx))
    observed: list[tuple[int, str]] = []
    caplog.set_level(logging.DEBUG)

    stamps: dict[str, float] = {}
    with locate_clock.stamp_on_dispatch(stamps, _PHONE_A1, kind="manual"):
        await _locate(
            rx=rx,
            registry=_registry_module().PhoneRequestRegistry(),
            observer=lambda n, t: observed.append((n, t)),
        )

    assert observed == [(4, "manual")]
    assert any(
        r.getMessage() == f"Phone action=4 trigger=manual entry={_ENTRY_A}"
        for r in caplog.records
    )


async def test_coordinator_counts_phone_action_per_trigger() -> None:
    from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord.stats = {"phone_action_4_auto": 0, "phone_action_4_manual": 0}
    coord._is_on_hass_loop = lambda: True  # type: ignore[method-assign]
    coord._schedule_stats_persist = lambda: None  # type: ignore[method-assign]
    count = _require(coord, "_count_phone_action")
    count(4, "auto")
    count(4, "manual")
    count(4, "manual")
    count(4, "unknown")  # no counter for an unknown trigger, nothing raised
    assert coord.stats == {"phone_action_4_auto": 1, "phone_action_4_manual": 2}


# ---------------------------------------------------------------------------
# Regression: FCM routing with a canonical id is unchanged
# ---------------------------------------------------------------------------


async def test_push_with_canonical_id_still_reaches_the_tracker_callback() -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    update = DeviceUpdate_pb2.DeviceUpdate()
    update.fcmMetadata.requestUuid = "uuid-tracker"
    ident = update.deviceMetadata.identifierInformation
    ident.type = DeviceUpdate_pb2.IDENTIFIER_SPOT
    ident.canonicIds.canonicId.add().id = "tracker-canonic-id"
    push_hex = update.SerializeToString().hex()
    got: list[tuple[str, str]] = []
    rx.location_update_callbacks["tracker-canonic-id"] = lambda cid, hx: got.append(
        (cid, hx)
    )
    phone_got: list[str] = []
    _require(rx, "register_phone_request")(_ENTRY_A, "uuid-tracker", phone_got.append)

    await _push(rx, _ENTRY_A, push_hex)

    assert got == [("tracker-canonic-id", push_hex)]
    assert phone_got == []  # a push with a canonical id never takes the phone switch


async def test_push_without_canonical_id_and_without_request_keeps_the_old_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    caplog.set_level(logging.DEBUG)
    await _push(rx, _ENTRY_A, _push_hex("uuid-none", _ANDROID_ID, None))
    assert any(
        "FCM response has no canonical id" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Log hygiene: no Android ID, key, payload or coordinates in records
# ---------------------------------------------------------------------------


def _record_texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    texts: list[str] = []
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(str(record.args))
        if record.exc_text:
            texts.append(record.exc_text)
    return texts


def _assert_clean(texts: list[str], keys: list[bytes], payloads: list[str]) -> None:
    forbidden = [
        str(_ANDROID_ID),
        hex(_ANDROID_ID),
        hex(_ANDROID_ID)[2:],
        str(_LAT),
        str(_LON),
    ]
    forbidden += [k.hex() for k in keys] + [base64.b64encode(k).decode() for k in keys]
    forbidden += payloads
    for text in texts:
        for value in forbidden:
            assert value not in text, f"{value!r} appeared in a log record"


async def test_log_hygiene_success_and_failure_paths(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rx = _receiver({_ENTRY_A: _TOKEN_A})
    registry = _registry_module().PhoneRequestRegistry()
    caplog.set_level(logging.DEBUG)
    nova = _answering_nova(rx)
    _install_nova(monkeypatch, nova)
    await _locate(rx=rx, registry=registry)

    def _fail(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise NovaHTTPError(500, "server")

    failing = _Nova(_fail)
    _install_nova(monkeypatch, failing)
    with pytest.raises(LocationRequestNotAcceptedError):
        await _locate(rx=rx, registry=registry)
    _install_nova(monkeypatch, _Nova())
    await _locate(rx=rx, registry=registry, timeout_s=0.01)

    keys = [_request_parts(p)[1] for p, _ in nova.calls + failing.calls]
    payloads = [p for p, _ in nova.calls + failing.calls]
    assert keys and payloads
    _assert_clean(_record_texts(caplog), keys, payloads)

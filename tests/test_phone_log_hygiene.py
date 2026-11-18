# tests/test_phone_log_hygiene.py
"""Log hygiene over every path of phones without a canonical ID.

Two layers:

* A static guard over the phone modules (``NovaApi/ExecuteAction/LocatePhone``
  and ``phone_identity.py``): no log call there may pass a leaf named like the
  Android ID, a key, ciphertext, a raw push or payload, a token, a location or
  the Wi-Fi name. These modules may use the strict names, which the
  package-wide guard in ``test_guard_logging_identifiers.py`` cannot (a bare
  ``android_id`` is the integration's own GSF ID in ``Auth/``).
* A run-time sweep with one log capture at DEBUG over the device list, a locate
  request with its push (success with status, HTTP 500, timeout, a push that
  does not decrypt), ring, stop, the coordinator's status store and a sensor
  update. Sentinels are created at run time: the Android IDs in decimal and hex,
  the per-entry HMAC key and every per-request key in hex and Base64, the push
  token, the payloads, the coordinates and the Wi-Fi name with and without its
  quotes. The phone row id (``aphone-…``) is an opaque HMAC value and may
  appear.
* The same run-time sweep over a phone listed with a canonical ID that offers
  the phone locate action: the device list that keys the target table by the
  canonical ID, a locate over the real API whose answer carries the canonical
  ID and reaches the open request before the canonical-id extraction, HTTP 500,
  timeout, the coordinator's status store under the canonical ID and a sensor
  update. The canonical ID is class (b) and may appear; the SIM name of the
  push is a sentinel too (nothing outside the decoder reads it).

* A shape check over the same records: no text may hold anything that looks
  like an Android ID (a 64-bit number as 17 to 20 decimal digits or 16 hex
  digits standing alone, with or without `0x`). It does not depend on the list
  of sentinels, so an ID the sweep did not register is found as well, as long
  as it is written in one of these shapes: unpadded hex shorter than 16 digits
  and decimal values below 10^15 are not seen. In the other direction a
  16-digit number (a microsecond timestamp, `0.30000000000000004`, a
  nanosecond timestamp, `token_hex(8)`) matches as well; none of the swept
  paths logs one. Like the sentinels it sees only the paths the sweep drives; a
  path nobody drives is the business of the static guards.

Every layer carries a positive control, so a checker that sees nothing cannot
pass as clean.
"""

from __future__ import annotations

import ast
import base64
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest

from custom_components import googlefindmy
from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_request_registry import (
    PhoneRequestRegistry,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaHTTPError
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.sensor import (
    PHONE_BATTERY_DESCRIPTION,
    PHONE_WIFI_SSID_DESCRIPTION,
    GoogleFindMyPhoneBatterySensor,
    GoogleFindMyPhoneWifiSensor,
)
from tests import test_phone_locate_flow as flow
from tests import test_phone_owned_canonic_route as route
from tests import test_phone_ring_paths as ring
from tests import test_phone_without_canonicid_decoder as listing
from tests.test_coordinator_automatic_polling import _DispatchingAPI, _make_coordinator
from tests.test_phone_status_sensors import _battery, _sim, _status_push, _wifi

_PACKAGE = Path(googlefindmy.__file__).resolve().parent
_PHONE_MODULES = (
    *sorted((_PACKAGE / "NovaApi" / "ExecuteAction" / "LocatePhone").glob("*.py")),
    _PACKAGE / "phone_identity.py",
)
_SSID = "Beispiel-Testnetz"

# Leaves that never belong in a log call of a phone module. `status` is not
# here: in `phone_locate.py` it is the HTTP status of a refusal.
_STRICT_NAME = re.compile(
    r"android_id|ssid|wifi|sim|(^|_)key(s)?$|^key|ciphertext|^iv$|push_hex|payload"
    r"|token|envelope|^fix$|latitude|longitude|plaintext|record$",
    re.IGNORECASE,
)
_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


# ---------------------------------------------------------------------------
# Static layer
# ---------------------------------------------------------------------------


def _leaves(node: ast.AST) -> list[str]:
    """Names reachable in a log argument; ``type(x).__name__`` is safe."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "type"
    ):
        return []
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        found.extend(_leaves(child))
    if isinstance(node, ast.Name):
        found.append(node.id)
    elif isinstance(node, ast.Attribute):
        found.append(node.attr)
    return found


def _offenders(source: str, name: str) -> tuple[list[tuple[str, int, str]], int]:
    offenders: list[tuple[str, int, str]] = []
    scanned = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOG_LEVELS:
            continue
        if "LOG" not in ast.unparse(func.value).upper():
            continue
        scanned += 1
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            for leaf in _leaves(argument):
                if _STRICT_NAME.search(leaf):
                    offenders.append((name, argument.lineno, leaf))
    return offenders, scanned


def test_phone_modules_never_log_a_secret_leaf() -> None:
    offenders: list[tuple[str, int, str]] = []
    scanned = 0
    for path in _PHONE_MODULES:
        found, count = _offenders(path.read_text(encoding="utf-8"), path.name)
        offenders.extend(found)
        scanned += count
    # Vacuum guard: the phone modules carry well over ten log calls.
    assert scanned > 10, f"only {scanned} log calls scanned"
    assert offenders == [], offenders


def test_static_layer_positive_control() -> None:
    source = "\n".join(
        [
            "_LOGGER.debug('x %s', android_id)",
            "_LOGGER.debug('x %s', request.key.hex())",
            "_LOGGER.warning('x %s', status.wifi_ssid)",
            "_LOGGER.debug('x %s', type(err).__name__)",
        ]
    )
    found, scanned = _offenders(source, "probe.py")
    assert scanned == 4
    assert sorted({leaf for _n, _l, leaf in found}) == [
        "android_id",
        "key",
        "wifi_ssid",
    ]


# ---------------------------------------------------------------------------
# Run-time layer
# ---------------------------------------------------------------------------


def _texts(caplog: pytest.LogCaptureFixture) -> list[str]:
    texts: list[str] = []
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(repr(record.args))
        if record.exc_text:
            texts.append(record.exc_text)
        if record.exc_info and record.exc_info[1] is not None:
            texts.append(repr(record.exc_info[1]))
    return texts


def _id_forms(value: int) -> list[str]:
    return [str(value), f"{value:x}", f"{value:X}", f"{value:016x}"]


def _key_forms(key: bytes) -> list[str]:
    return [key.hex(), key.hex().upper(), base64.b64encode(key).decode()]


def _leaks(texts: list[str], sentinels: list[str]) -> list[str]:
    return [s for s in sentinels if any(s in text for text in texts)]


# An Android ID by its shape: a 64-bit number written as 17 to 20 decimal
# digits or as 16 hex digits (also after `0x`), not embedded in a longer word. Row ids
# (`aphone-` plus 32 hex digits), UUID groups, hashes and second or
# millisecond timestamps have other lengths. A 16-digit number (a microsecond
# timestamp) would match; none of the swept paths logs one.
_ID_SHAPED = re.compile(
    r"(?<![0-9A-Za-z])(?:0[xX][0-9a-fA-F]{16}|[0-9]{17,20}|[0-9a-fA-F]{16})"
    r"(?![0-9A-Za-z])"
)


def _id_shaped(texts: list[str]) -> list[str]:
    return sorted({m.group() for text in texts for m in _ID_SHAPED.finditer(text)})


def test_the_shape_check_tells_an_android_id_from_other_values() -> None:
    aid = route._android_id()
    for form in (
        str(aid),
        f"{aid:016x}",
        f"{aid:016X}",
        f"0x{aid:016x}",
        f"{aid:#018x}",
        str(2**64 - 1),
    ):
        assert _id_shaped([f"probe {form}"]) == [form], form
        assert _id_shaped([f"probe id={form}, next"]) == [form], form
        assert _id_shaped([f"({form},)"]) == [form], form
    harmless = [
        "aphone-" + "0123456789abcdef" * 2,
        "observed_at=1731958000.123 expires=1731958000123",
        "request 3f2b6c1e-8a4d-4e7b-9c15-0a1b2c3d4e5f",
        "sha256=" + "ab" * 32,
        "entry 01JCZX9Q8R7S6T5V4W3X2Y1Z0A",
        "canonical=cid-hygiene accuracy=12 serial=123456789012345",
        "frame=0x40 payload_len=10",
    ]
    assert _id_shaped(harmless) == []
    # Stated limit: a 16-digit number has the shape of the hex form.
    assert _id_shaped(["stamp=1731958000123456"]) == ["1731958000123456"]


def _update_phone_sensors(coord: Any, device_id: str) -> None:
    """Drive one coordinator update of both phone sensors for ``device_id``."""
    for cls, description in (
        (GoogleFindMyPhoneBatterySensor, PHONE_BATTERY_DESCRIPTION),
        (GoogleFindMyPhoneWifiSensor, PHONE_WIFI_SSID_DESCRIPTION),
    ):
        entity = cls.__new__(cls)
        entity.coordinator = coord
        entity._device_id = device_id
        entity._attr_native_value = None
        entity._last_observation_wall = None
        entity.entity_description = description
        entity.coordinator_has_device = lambda: True
        entity.refresh_device_label_from_coordinator = lambda **_kw: None
        entity.async_write_ha_state = lambda: None
        entity._handle_coordinator_update()
        assert entity.native_value in (55, _SSID)


def _owned_status_push(
    request_uuid: str, android_id: int, canonic: str, **fields: bytes
) -> str:
    """Hex ``DeviceUpdate`` of an owned phone's answer: status record plus canonical ID."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    update.ParseFromString(
        bytes.fromhex(_status_push(request_uuid, android_id, **fields))
    )
    ids = update.deviceMetadata.identifierInformation.phoneInformation.canonicIds
    ids.canonicId.add().id = canonic
    return update.SerializeToString().hex()


@pytest.mark.asyncio
async def test_no_record_of_any_phone_path_carries_a_sentinel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sentinels: list[str] = [_SSID, f'"{_SSID}"', str(flow._LAT), str(flow._LON)]

    # 1. Device list with a phone row (decoder, HMAC row id).
    list_android_id = listing._random_android_id()
    rows = listing._rows(
        listing._listing(listing._phone("Phone", list_android_id)), listing._KEY_A
    )
    assert any(str(r.get("id", "")).startswith("aphone-") for r in rows)
    sentinels += _id_forms(list_android_id) + _key_forms(listing._KEY_A)

    # 2. Locate: success with status, HTTP 500, timeout, push that does not decrypt.
    rx = flow._receiver({flow._ENTRY_A: flow._TOKEN_A})
    registry = PhoneRequestRegistry()
    statuses: list[PhoneStatus] = []

    async def _answer(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, android_id = flow._request_parts(payload)
        push = _status_push(
            request_uuid,
            android_id,
            envelope=flow._envelope(key),
            wifi=_wifi(f'"{_SSID}"'.encode()),
            battery=_battery(55),
        )
        await flow._push(rx, flow._ENTRY_A, push)

    answering = flow._Nova(_answer)
    flow._install_nova(monkeypatch, answering)
    rows = await flow._phone_locate().locate_phone(
        phone_id=flow._PHONE_A1,
        android_id=flow._ANDROID_ID,
        entry_id=flow._ENTRY_A,
        receiver=rx,
        registry=registry,
        session=None,
        cache=flow._Cache(flow._ENTRY_A),
        namespace=flow._ENTRY_A,
        timeout_s=2.0,
        status_sink=lambda _p, s: statuses.append(s),
    )
    assert rows and statuses and statuses[0].wifi_ssid == _SSID

    def _fail(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise NovaHTTPError(500, "server")

    failing = flow._Nova(_fail)
    flow._install_nova(monkeypatch, failing)
    with pytest.raises(flow.LocationRequestNotAcceptedError):
        await flow._locate(rx=rx, registry=registry)

    flow._install_nova(monkeypatch, flow._Nova())
    assert await flow._locate(rx=rx, registry=registry, timeout_s=0.01) == []

    async def _wrong_key(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, _key, android_id = flow._request_parts(payload)
        push = flow._push_hex(request_uuid, android_id, flow._envelope(b"\x07" * 16))
        await flow._push(rx, flow._ENTRY_A, push)

    wrong = flow._Nova(_wrong_key)
    flow._install_nova(monkeypatch, wrong)
    assert await flow._locate(rx=rx, registry=registry) == []

    calls = answering.calls + failing.calls + wrong.calls
    sentinels += _id_forms(flow._ANDROID_ID) + [flow._TOKEN_A]
    for payload, _kwargs in calls:
        sentinels.append(payload)
        sentinels += _key_forms(flow._request_parts(payload)[1])
    sentinels += _key_forms(b"\x07" * 16)

    # 3. Ring and stop, ready and not ready, and a refused stop.
    wire = ring._Wire()
    ring._install_wire(monkeypatch, wire)
    api = ring._api(monkeypatch, ring._Receiver())
    await api.async_play_sound(ring._PHONE)
    await api.async_stop_sound(ring._PHONE)
    await ring._api(monkeypatch, ring._Receiver(ready=False)).async_play_sound(
        ring._PHONE
    )
    ring._install_wire(monkeypatch, ring._Wire(error=NovaHTTPError(500, "x")))
    await api.async_stop_sound(ring._PHONE)
    sentinels += _id_forms(ring._ANDROID_ID) + [ring._TOKEN]

    # 4. Coordinator status store and a sensor update.
    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    coord.async_update_listeners = lambda: None
    coord.data = [{"id": flow._PHONE_A1, "name": "Phone"}]
    status = PhoneStatus(battery_pct=55, wifi_ssid=_SSID, observed_at=time.time())
    coord._store_phone_status(flow._PHONE_A1, status)
    _update_phone_sensors(coord, flow._PHONE_A1)

    texts = _texts(caplog)
    assert len(caplog.records) > 10, "the sweep logged almost nothing"
    # Each path logged: the sweep reached what it claims to cover.
    joined = "\n".join(texts)
    for marker in (
        "Phone action=4",
        "stage=phone_server_error",
        "No phone location push",
        "did not yield a location",
        "Phone action=1",
        "Phone action=5",
        "stage=phone_fcm_not_ready",
        "Phone status updated for",
    ):
        assert marker in joined, marker
    assert _leaks(texts, sentinels) == []
    assert _id_shaped(texts) == []

    # Positive control: one record with a sentinel is found by the same checks.
    logging.getLogger("custom_components.googlefindmy.probe").debug(
        "probe %s", flow._ANDROID_ID
    )
    assert _leaks(_texts(caplog), sentinels) == [str(flow._ANDROID_ID)]
    assert _id_shaped(_texts(caplog)) == [str(flow._ANDROID_ID)]


_OWNED_CANONIC = "cid-hygiene"
_SIM_NAME = "Beispiel-SIM"


@pytest.mark.asyncio
async def test_no_record_of_the_owned_phone_path_carries_a_sentinel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sweep over a phone listed with a canonical ID that offers action 4.

    The target table is filled by the device list (not injected), the locate
    runs over the real API and the real receiver, and the answer carries the
    canonical ID, so it takes the pre-delivery to the open request.
    """
    caplog.set_level(logging.DEBUG)
    aid = route._android_id()
    sentinels: list[str] = [
        _SSID,
        f'"{_SSID}"',
        _SIM_NAME,
        str(flow._LAT),
        str(flow._LON),
        flow._TOKEN_A,
        *_id_forms(aid),
    ]

    # 1. Device list: the canonical ID becomes a key of the target table.
    rx = flow._receiver({flow._ENTRY_A: flow._TOKEN_A})
    monkeypatch.setattr(api_module, "_FCM_ReceiverGetter", lambda _entry: rx)
    cache = flow._Cache(flow._ENTRY_A)
    api = GoogleFindMyAPI(cache=cache)
    monkeypatch.setattr(api, "_decoder_token_cache", lambda: cache)
    rows = api._process_device_list_response(
        route._listing(
            route._phone(aid, phone_canonic=(_OWNED_CANONIC,)),
            route._tracker("cid-tag"),
        )
    )
    assert api._phone_targets == {_OWNED_CANONIC: aid}
    assert {r["id"]: r.get("locate_route") for r in rows} == {
        _OWNED_CANONIC: None,
        "cid-tag": None,
    }
    assert api.has_phone_target(_OWNED_CANONIC) is True

    # 2. Coordinator store bound as the sink, as at setup.
    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    coord.async_update_listeners = lambda: None
    coord.data = [{"id": _OWNED_CANONIC, "name": "Phone"}]
    api.set_phone_status_sink(coord._store_phone_status)

    # 3. Locate with status: the push carries the canonical ID, Wi-Fi and SIM name.
    async def _answer(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, android_id = flow._request_parts(payload)
        push = _owned_status_push(
            request_uuid,
            android_id,
            _OWNED_CANONIC,
            envelope=flow._envelope(key),
            wifi=_wifi(f'"{_SSID}"'.encode()),
            battery=_battery(55),
            sim=_sim(_SIM_NAME.encode()),
        )
        await flow._push(rx, flow._ENTRY_A, push)

    answering = flow._Nova(_answer)
    flow._install_nova(monkeypatch, answering)
    with route._labelled("manual"):
        best = await api.async_get_device_location(_OWNED_CANONIC, "Phone")
    assert best["latitude"] == pytest.approx(flow._LAT)
    status = coord.get_phone_status(_OWNED_CANONIC)
    assert status is not None and status.wifi_ssid == _SSID
    assert status.battery_pct == 55

    # 4. HTTP 500 and timeout over the same route.
    def _fail(_payload: str, _kwargs: dict[str, Any]) -> None:
        raise NovaHTTPError(500, "server")

    failing = flow._Nova(_fail)
    flow._install_nova(monkeypatch, failing)
    with (
        route._labelled("manual"),
        pytest.raises(flow.LocationRequestNotAcceptedError) as info,
    ):
        await api.async_get_device_location(_OWNED_CANONIC, "Phone")
    assert info.value.stage == "phone_server_error"

    real_locate = api_module.locate_phone

    async def _short(**kwargs: Any) -> Any:
        return await real_locate(**{**kwargs, "timeout_s": 0.05})

    monkeypatch.setattr(api_module, "locate_phone", _short)
    silent = flow._Nova()
    flow._install_nova(monkeypatch, silent)
    with route._labelled("manual"):
        assert await api.async_get_device_location(_OWNED_CANONIC, "Phone") == {}
    assert coord.get_phone_status(_OWNED_CANONIC) is status

    # 4b. An automatic request takes the tracker path (stand-in) in the same sweep.
    tracker_asks: list[str] = []

    async def _tracker(device_id: str, *_a: Any, **_k: Any) -> list[Any]:
        tracker_asks.append(device_id)
        return []

    monkeypatch.setattr(api_module, "get_location_data_for_device", _tracker)
    with route._labelled("auto"):
        assert await api.async_get_device_location(_OWNED_CANONIC, "Phone") == {}
    assert tracker_asks == [_OWNED_CANONIC]

    # 5. A sensor update under the canonical ID.
    _update_phone_sensors(coord, _OWNED_CANONIC)

    for payload, _kwargs in answering.calls + failing.calls + silent.calls:
        sentinels.append(payload)
        sentinels += _key_forms(flow._request_parts(payload)[1])

    texts = _texts(caplog)
    assert len(caplog.records) > 10, "the sweep logged almost nothing"
    joined = "\n".join(texts)
    for marker in (
        "Phone action=4",
        "FCM push delivered to an open phone request",
        "stage=phone_server_error",
        "No phone location push",
        f"Phone status updated for {_OWNED_CANONIC}",
    ):
        assert marker in joined, marker
    assert _leaks(texts, sentinels) == []
    assert _id_shaped(texts) == []

    # Positive control: one record with a sentinel is found by the same checks.
    logging.getLogger("custom_components.googlefindmy.probe").debug("probe %s", aid)
    assert _leaks(_texts(caplog), sentinels) == [str(aid)]
    assert _id_shaped(_texts(caplog)) == [str(aid)]

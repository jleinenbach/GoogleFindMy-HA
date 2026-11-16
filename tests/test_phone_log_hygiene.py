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

Both layers carry a positive control, so a checker that sees nothing cannot
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
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_request_registry import (
    PhoneRequestRegistry,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from custom_components.googlefindmy.NovaApi.nova_request import NovaHTTPError
from custom_components.googlefindmy.sensor import (
    PHONE_BATTERY_DESCRIPTION,
    PHONE_WIFI_SSID_DESCRIPTION,
    GoogleFindMyPhoneBatterySensor,
    GoogleFindMyPhoneWifiSensor,
)
from tests import test_phone_locate_flow as flow
from tests import test_phone_ring_paths as ring
from tests import test_phone_without_canonicid_decoder as listing
from tests.test_coordinator_automatic_polling import _DispatchingAPI, _make_coordinator
from tests.test_phone_status_sensors import _battery, _status_push, _wifi

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
    for cls, description in (
        (GoogleFindMyPhoneBatterySensor, PHONE_BATTERY_DESCRIPTION),
        (GoogleFindMyPhoneWifiSensor, PHONE_WIFI_SSID_DESCRIPTION),
    ):
        entity = cls.__new__(cls)
        entity.coordinator = coord
        entity._device_id = flow._PHONE_A1
        entity._attr_native_value = None
        entity._last_observation_wall = None
        entity.entity_description = description
        entity.coordinator_has_device = lambda: True
        entity.refresh_device_label_from_coordinator = lambda **_kw: None
        entity.async_write_ha_state = lambda: None
        entity._handle_coordinator_update()
        assert entity.native_value in (55, _SSID)

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

    # Positive control: one record with a sentinel is found by the same check.
    logging.getLogger("custom_components.googlefindmy.probe").debug(
        "probe %s", flow._ANDROID_ID
    )
    assert _leaks(_texts(caplog), sentinels) == [str(flow._ANDROID_ID)]

# tests/test_ble_scanner_contracts.py
"""Contract tests for the optional HA-Bluetooth FMDN advertisement scanner.

Covers ``custom_components/googlefindmy/fmdn_finder/ble_scanner.py``:

* ``_is_fmdn_service_data`` payload-extraction invariants (the >=21 byte
  length gate, FEAA-before-FE2C priority, boundary at exactly 21 bytes).
* ``async_setup_ble_scanner`` terminal exits (import unavailable, domain
  bucket not ready, successful registration) plus the nested advertisement
  callback behaviour (no payload, no resolver, resolved match, rate-limited
  unresolved log).
* ``async_unload_ble_scanner`` terminal exits (bucket not ready, callable
  unsub invoked, missing unsub).

The HA ``bluetooth`` integration is not importable in the test environment
(missing transitive deps), so the success path installs a minimal fake
``homeassistant.components.bluetooth`` module and captures the registered
callback for direct invocation (DoD: import path exercised only via patch).
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    MODERN_FRAME_TYPE,
)
from custom_components.googlefindmy.fmdn_finder import ble_scanner
from custom_components.googlefindmy.fmdn_finder.ble_scanner import (
    DATA_BLE_SCANNER_UNSUB,
    FE2C_SERVICE_UUID,
    FEAA_SERVICE_UUID,
    MIN_FMDN_PAYLOAD_LENGTH,
    _is_fmdn_service_data,
    async_setup_ble_scanner,
    async_unload_ble_scanner,
)

_BT_MODULE = "homeassistant.components.bluetooth"


def _payload(
    frame: int = FMDN_FRAME_TYPE, length: int = MIN_FMDN_PAYLOAD_LENGTH
) -> bytes:
    """Build a payload of ``length`` bytes starting with ``frame``."""
    return bytes([frame]) + bytes(range(1, length))


# --------------------------------------------------------------------------- #
# _is_fmdn_service_data — payload extraction invariants                        #
# --------------------------------------------------------------------------- #


def test_is_fmdn_feaa_at_min_length_returns_payload() -> None:
    """Exactly 21 bytes under FEAA is accepted (length gate is >=, not >)."""
    data = _payload(length=MIN_FMDN_PAYLOAD_LENGTH)
    assert len(data) == 21
    payload, uuid = _is_fmdn_service_data({FEAA_SERVICE_UUID: data})
    assert payload == data
    assert uuid == FEAA_SERVICE_UUID


def test_is_fmdn_one_byte_below_min_is_rejected() -> None:
    """20 bytes (one below the gate) is rejected — boundary invariant."""
    data = _payload(length=MIN_FMDN_PAYLOAD_LENGTH - 1)
    assert len(data) == 20
    assert _is_fmdn_service_data({FEAA_SERVICE_UUID: data}) == (None, None)


def test_is_fmdn_fe2c_when_feaa_absent() -> None:
    """FE2C is used when FEAA is not present."""
    data = _payload(length=25)
    payload, uuid = _is_fmdn_service_data({FE2C_SERVICE_UUID: data})
    assert payload == data
    assert uuid == FE2C_SERVICE_UUID


def test_is_fmdn_feaa_takes_priority_over_fe2c() -> None:
    """When both UUIDs carry a valid payload, FEAA wins (iteration order)."""
    feaa = _payload(frame=FMDN_FRAME_TYPE, length=21)
    fe2c = _payload(frame=MODERN_FRAME_TYPE, length=30)
    payload, uuid = _is_fmdn_service_data(
        {FE2C_SERVICE_UUID: fe2c, FEAA_SERVICE_UUID: feaa}
    )
    assert uuid == FEAA_SERVICE_UUID
    assert payload == feaa


def test_is_fmdn_empty_service_data_returns_none() -> None:
    """No matching UUID -> (None, None)."""
    assert _is_fmdn_service_data({}) == (None, None)
    assert _is_fmdn_service_data(
        {"0000abcd-0000-1000-8000-00805f9b34fb": _payload()}
    ) == (
        None,
        None,
    )


def test_is_fmdn_normalizes_bytearray_to_bytes() -> None:
    """A bytearray payload is returned as immutable bytes."""
    data = bytearray(_payload(length=21))
    payload, _uuid = _is_fmdn_service_data({FEAA_SERVICE_UUID: data})
    assert isinstance(payload, bytes)
    assert not isinstance(payload, bytearray)


# --------------------------------------------------------------------------- #
# Fake bluetooth module + captured-callback fixture                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_bluetooth(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a minimal fake ``homeassistant.components.bluetooth`` module.

    Returns a namespace exposing the ``async_register_callback`` mock and a
    ``captured`` dict that the test fills with the registered callback once
    ``async_setup_ble_scanner`` runs.
    """
    captured: dict[str, object] = {}

    def _register(
        hass: object, callback: object, matcher: object, mode: object
    ) -> Mock:
        captured["callback"] = callback
        captured["matcher"] = matcher
        captured["mode"] = mode
        return Mock(name="unsub")

    register_mock = MagicMock(side_effect=_register)

    module = ModuleType(_BT_MODULE)
    module.BluetoothChange = object  # type: ignore[attr-defined]
    module.BluetoothServiceInfoBleak = object  # type: ignore[attr-defined]
    module.BluetoothScanningMode = SimpleNamespace(PASSIVE="passive")  # type: ignore[attr-defined]
    module.async_register_callback = register_mock  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, _BT_MODULE, module)
    return SimpleNamespace(register=register_mock, captured=captured)


def _service_info(
    service_data: dict[str, bytes],
    address: str = "AA:BB:CC:DD:EE:FF",
    rssi: int = -50,
) -> SimpleNamespace:
    return SimpleNamespace(service_data=service_data, address=address, rssi=rssi)


# --------------------------------------------------------------------------- #
# async_setup_ble_scanner — terminal exits                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_setup_returns_false_when_bluetooth_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ImportError on the bluetooth import -> non-fatal False."""
    # Setting the module to None in sys.modules forces ImportError on import.
    monkeypatch.setitem(sys.modules, _BT_MODULE, None)
    hass = SimpleNamespace(data={DOMAIN: {}})
    assert await async_setup_ble_scanner(hass) is False


@pytest.mark.asyncio
async def test_setup_returns_false_when_domain_bucket_missing(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """A domain bucket that is not a dict aborts setup with False."""
    hass = SimpleNamespace(data={})  # DOMAIN absent -> .get(DOMAIN) is None
    assert await async_setup_ble_scanner(hass) is False
    fake_bluetooth.register.assert_not_called()


@pytest.mark.asyncio
async def test_setup_registers_callback_and_stores_unsub(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """Happy path: registers a PASSIVE callback and stores the unsub."""
    bucket: dict[str, object] = {}
    hass = SimpleNamespace(data={DOMAIN: bucket})
    assert await async_setup_ble_scanner(hass) is True
    fake_bluetooth.register.assert_called_once()
    # PASSIVE mode requested (no active scan / no extra power draw).
    assert fake_bluetooth.captured["mode"] == "passive"
    # No matcher — the module filters inside the callback.
    assert fake_bluetooth.captured["matcher"] is None
    assert callable(bucket[DATA_BLE_SCANNER_UNSUB])


# --------------------------------------------------------------------------- #
# Nested advertisement callback behaviour                                      #
# --------------------------------------------------------------------------- #


async def _setup_and_capture(
    fake_bluetooth: SimpleNamespace, resolver: object | None
) -> tuple[dict[str, object], object]:
    """Run setup with an optional resolver in the bucket; return callback."""
    bucket: dict[str, object] = {}
    if resolver is not None:
        bucket[DATA_EID_RESOLVER] = resolver
    hass = SimpleNamespace(data={DOMAIN: bucket})
    assert await async_setup_ble_scanner(hass) is True
    return bucket, fake_bluetooth.captured["callback"]


@pytest.mark.asyncio
async def test_callback_ignores_non_fmdn_advertisement(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """No FMDN payload -> callback returns without touching the resolver."""
    resolver = Mock()
    _bucket, callback = await _setup_and_capture(fake_bluetooth, resolver)
    callback(_service_info({}), None)  # empty service_data -> payload None
    resolver.resolve_eid.assert_not_called()


@pytest.mark.asyncio
async def test_callback_returns_when_resolver_absent(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """A valid payload but no resolver in the bucket -> early return."""
    _bucket, callback = await _setup_and_capture(fake_bluetooth, resolver=None)
    # Must not raise even though there is no resolver.
    callback(_service_info({FEAA_SERVICE_UUID: _payload()}), None)


@pytest.mark.asyncio
async def test_callback_resolves_match_with_ble_address(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """A resolvable advertisement calls resolve_eid with the BLE address."""
    match = SimpleNamespace(device_id="device12345678", canonical_id="canon12345678")
    resolver = Mock()
    resolver.resolve_eid.return_value = match
    _bucket, callback = await _setup_and_capture(fake_bluetooth, resolver)

    payload = _payload(frame=MODERN_FRAME_TYPE, length=21)
    callback(
        _service_info({FEAA_SERVICE_UUID: payload}, address="11:22:33:44:55:66"), None
    )

    resolver.resolve_eid.assert_called_once()
    args, kwargs = resolver.resolve_eid.call_args
    assert args[0] == payload
    assert kwargs["ble_address"] == "11:22:33:44:55:66"


@pytest.mark.asyncio
async def test_callback_handles_unknown_frame_type(
    fake_bluetooth: SimpleNamespace,
) -> None:
    """A payload whose first byte is not a known frame type still resolves.

    Exercises the ``frame_type is None`` branch (payload[0] not in the frame
    set): resolution proceeds, the log falls back to frame 0.
    """
    match = SimpleNamespace(device_id="device12345678", canonical_id=None)
    resolver = Mock()
    resolver.resolve_eid.return_value = match
    _bucket, callback = await _setup_and_capture(fake_bluetooth, resolver)

    payload = _payload(frame=0x99, length=21)  # 0x99 is neither 0x40 nor 0x41
    callback(_service_info({FEAA_SERVICE_UUID: payload}), None)
    resolver.resolve_eid.assert_called_once()


@pytest.mark.asyncio
async def test_callback_rate_limits_unresolved_logs(
    fake_bluetooth: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolved advertisements are logged at most once per interval prefix."""
    resolver = Mock()
    resolver.resolve_eid.return_value = None  # never resolves
    _bucket, callback = await _setup_and_capture(fake_bluetooth, resolver)

    debug = Mock()
    monkeypatch.setattr(ble_scanner._LOGGER, "debug", debug)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ble_scanner.time, "monotonic", lambda: clock["t"])

    info = _service_info({FEAA_SERVICE_UUID: _payload()})
    callback(info, None)  # first sighting -> logs
    first = debug.call_count
    callback(info, None)  # immediately again -> suppressed
    assert debug.call_count == first  # no new log within interval

    clock["t"] += ble_scanner._UNRESOLVED_LOG_INTERVAL + 1.0
    callback(info, None)  # interval elapsed -> logs again
    assert debug.call_count == first + 1
    assert resolver.resolve_eid.call_count == 3  # resolver hit every time


# --------------------------------------------------------------------------- #
# async_unload_ble_scanner — terminal exits                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unload_returns_true_when_bucket_missing() -> None:
    """No domain bucket -> nothing to do, return True."""
    hass = SimpleNamespace(data={})
    assert await async_unload_ble_scanner(hass) is True


@pytest.mark.asyncio
async def test_unload_invokes_callable_unsub() -> None:
    """A callable unsub is popped and invoked exactly once."""
    unsub = Mock()
    bucket = {DATA_BLE_SCANNER_UNSUB: unsub}
    hass = SimpleNamespace(data={DOMAIN: bucket})
    assert await async_unload_ble_scanner(hass) is True
    unsub.assert_called_once()
    assert DATA_BLE_SCANNER_UNSUB not in bucket  # popped


@pytest.mark.asyncio
async def test_unload_tolerates_missing_unsub() -> None:
    """No stored unsub -> return True without calling anything."""
    bucket: dict[str, object] = {}
    hass = SimpleNamespace(data={DOMAIN: bucket})
    assert await async_unload_ble_scanner(hass) is True

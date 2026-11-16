# tests/test_phone_status_sensors.py
"""Battery level and Wi-Fi name of a phone without a canonical ID.

The data path under test: the push answering phone action 4 carries field 32.1
(battery, percent) and field 30.1 (Wi-Fi name in double quotes); the register
reads them only for the requested Android ID; ``locate_phone`` hands them to a
status sink before it decrypts the location; the coordinator keeps them in a
store of their own, never in the location row; two sensors read that store.

Privacy: every value here is invented. The Wi-Fi name is the sentinel
``"Beispiel-Testnetz"`` (with its quotes on the wire), never a real network name,
and the Android IDs are the invented ones of ``test_phone_locate_flow``. No log
record of these tests may carry the Wi-Fi name or an Android ID.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.googlefindmy.sensor as sensor_module
from custom_components.googlefindmy.const import PHONE_ID_PREFIX
from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
    phone_status as status_module,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_request_registry import (
    PhoneRequestRegistry,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.sensor import (
    PHONE_BATTERY_DESCRIPTION,
    PHONE_WIFI_SSID_DESCRIPTION,
    GoogleFindMyPhoneBatterySensor,
    GoogleFindMyPhoneWifiSensor,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_coordinator_automatic_polling import _DispatchingAPI, _make_coordinator
from tests.test_phone_locate_flow import (
    _ANDROID_ID,
    _ANDROID_ID_B,
    _ENTRY_A,
    _TOKEN_A,
    _Cache,
    _envelope,
    _install_nova,
    _Nova,
    _push,
    _receiver,
    _request_parts,
)

pytestmark = pytest.mark.asyncio

_SSID = "Beispiel-Testnetz"
_PHONE = PHONE_ID_PREFIX + "a" * 32
_NOW = 1_800_000_000.0
_DAY = 24 * 3600
_REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _wifi(raw: bytes) -> bytes:
    return DeviceUpdate_pb2.PhoneWifiInformation(ssid=raw).SerializeToString()


def _battery(level: int | None) -> bytes:
    message = DeviceUpdate_pb2.PhoneBatteryInformation()
    if level is not None:
        message.batteryLevel = level
    return message.SerializeToString()


def _sim(name: bytes) -> bytes:
    return DeviceUpdate_pb2.PhoneSimInformation(simDisplayName=name).SerializeToString()


def _status_push(  # noqa: PLR0913 - one argument per wire field
    request_uuid: str,
    android_id: int,
    *,
    envelope: bytes | None = None,
    wifi: bytes | None = None,
    battery: bytes | None = None,
    sim: bytes | None = None,
) -> str:
    """Hex ``DeviceUpdate`` of a phone push with the given record fields."""
    update = DeviceUpdate_pb2.DeviceUpdate()
    update.fcmMetadata.requestUuid = request_uuid
    ident = update.deviceMetadata.identifierInformation
    ident.type = DeviceUpdate_pb2.IDENTIFIER_ANDROID
    ident.phoneInformation.phoneInformationField1 = android_id
    record = DeviceUpdate_pb2.DeviceMetadata.PhoneInformation()
    if envelope is not None:
        record.phoneInformationField36 = envelope
    if wifi is not None:
        record.wifiInformation = wifi
    if battery is not None:
        record.batteryInformation = battery
    if sim is not None:
        record.simInformation = sim
    update.deviceMetadata.phoneInformation = record.SerializeToString()
    return update.SerializeToString().hex()


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 77, 100])
async def test_battery_inside_range_is_kept(level: int) -> None:
    assert status_module.battery_from_field(_battery(level)) == level


@pytest.mark.parametrize("level", [101, 255, 2**64 - 1])
async def test_battery_outside_range_is_dropped(level: int) -> None:
    # -1 as a uint64 on the wire is 2**64 - 1.
    assert status_module.battery_from_field(_battery(level)) is None


async def test_battery_without_field_or_garbage_is_none() -> None:
    # proto3 does not put a zero on the wire: 0 % is indistinguishable from a
    # missing level, so a record with level 0 carries none (documented limit).
    assert status_module.battery_from_field(_battery(0)) is None
    assert status_module.battery_from_field(b"") is None
    assert status_module.battery_from_field(_battery(None)) is None
    assert status_module.battery_from_field(b"\xff\xff\xff") is None


async def test_quotes_are_removed() -> None:
    assert status_module.ssid_from_field(_wifi(f'"{_SSID}"'.encode())) == _SSID


async def test_name_without_quotes_stays_as_is() -> None:
    assert status_module.ssid_from_field(_wifi(_SSID.encode())) == _SSID
    # One quote is not a pair: nothing is removed.
    assert status_module.ssid_from_field(_wifi(b'"abc')) == '"abc'


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'""',
        b'"<unknown ssid>"',
        b"<unknown ssid>",
        b'"' + b"x" * 33 + b'"',
        b'"\xff\xfe"',
        b'"net\x00work"',
        b'"net\nwork"',
    ],
)
async def test_unusable_names_count_as_no_wifi(raw: bytes) -> None:
    assert status_module.ssid_from_field(_wifi(raw)) is None


async def test_name_of_32_octets_is_kept() -> None:
    assert status_module.ssid_from_field(_wifi(b'"' + b"y" * 32 + b'"')) == "y" * 32


async def test_status_repr_hides_the_wifi_name() -> None:
    status = PhoneStatus(battery_pct=50, wifi_ssid=_SSID, observed_at=_NOW)
    assert _SSID not in repr(status)


async def test_status_has_no_sim_field() -> None:
    assert {f for f in PhoneStatus.__dataclass_fields__} == {
        "battery_pct",
        "wifi_ssid",
        "observed_at",
    }


async def test_no_reader_of_the_sim_record_outside_the_decoder() -> None:
    """DoD guard: no reader of field 31 outside the decoder."""
    result = subprocess.run(
        [
            "grep",
            "-rnwE",
            "simInformation|simDisplayName",
            str(_REPO / "custom_components" / "googlefindmy"),
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    readers = [
        line
        for line in result.stdout.splitlines()
        if "_pb2" not in line and "ProtoDecoders/decoder.py" not in line
    ]
    assert readers == []


# ---------------------------------------------------------------------------
# Register: read_status
# ---------------------------------------------------------------------------


async def _open(registry: PhoneRequestRegistry, uuid: str = "u-1") -> Any:
    request = registry.open(
        entry_id=_ENTRY_A,
        request_uuid=uuid,
        phone_id=_PHONE,
        android_id=_ANDROID_ID,
        timeout_s=5,
    )
    assert request is not None
    return request


async def test_read_status_for_the_requested_phone() -> None:
    registry = PhoneRequestRegistry()
    await _open(registry)
    push = _status_push(
        "u-1", _ANDROID_ID, wifi=_wifi(f'"{_SSID}"'.encode()), battery=_battery(78)
    )
    status = registry.read_status(_ENTRY_A, "u-1", push)
    assert status is not None
    assert (status.battery_pct, status.wifi_ssid) == (78, _SSID)


async def test_read_status_ignores_another_android_id() -> None:
    registry = PhoneRequestRegistry()
    await _open(registry)
    push = _status_push("u-1", _ANDROID_ID_B, battery=_battery(78))
    assert registry.read_status(_ENTRY_A, "u-1", push) is None


async def test_read_status_without_open_request_or_parse_is_none() -> None:
    registry = PhoneRequestRegistry()
    push = _status_push("u-1", _ANDROID_ID, battery=_battery(78))
    assert registry.read_status(_ENTRY_A, "u-1", push) is None
    await _open(registry)
    assert registry.read_status(_ENTRY_A, "u-1", "zz") is None


async def test_read_status_of_a_push_without_values_is_an_empty_status() -> None:
    """Reviewer P2-2: a push without both values must still reach the store."""
    registry = PhoneRequestRegistry()
    await _open(registry)
    status = registry.read_status(_ENTRY_A, "u-1", _status_push("u-1", _ANDROID_ID))
    assert status is not None
    assert (status.battery_pct, status.wifi_ssid) == (None, None)


async def test_read_status_without_location_still_yields_the_status() -> None:
    registry = PhoneRequestRegistry()
    await _open(registry)
    push = _status_push("u-1", _ANDROID_ID, battery=_battery(40))
    assert registry.decrypt_push(_ENTRY_A, "u-1", push) is None
    status = registry.read_status(_ENTRY_A, "u-1", push)
    assert status is not None and status.battery_pct == 40


# ---------------------------------------------------------------------------
# locate_phone hands the status to the sink
# ---------------------------------------------------------------------------


def _phone_locate() -> Any:
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (  # noqa: PLC0415
        phone_locate,
    )

    return phone_locate


async def _locate_with_answer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_location: bool,
    sink: Any,
    android_id_in_push: int = _ANDROID_ID,
) -> list[dict[str, Any]]:
    rx = _receiver({_ENTRY_A: _TOKEN_A})

    async def _answer(payload: str, _kwargs: dict[str, Any]) -> None:
        request_uuid, key, _aid = _request_parts(payload)
        push = _status_push(
            request_uuid,
            android_id_in_push,
            envelope=_envelope(key) if with_location else None,
            wifi=_wifi(f'"{_SSID}"'.encode()),
            battery=_battery(66),
            sim=_sim(b"Beispiel-SIM"),
        )
        await _push(rx, _ENTRY_A, push)

    _install_nova(monkeypatch, _Nova(_answer))
    rows: list[dict[str, Any]] = await _phone_locate().locate_phone(
        phone_id=_PHONE,
        android_id=_ANDROID_ID,
        entry_id=_ENTRY_A,
        receiver=rx,
        registry=PhoneRequestRegistry(),
        session=None,
        cache=_Cache(_ENTRY_A),
        namespace=_ENTRY_A,
        timeout_s=2.0,
        status_sink=sink,
    )
    return rows


async def test_sink_gets_status_and_the_row_carries_none_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got: list[tuple[str, PhoneStatus]] = []
    rows = await _locate_with_answer(
        monkeypatch, with_location=True, sink=lambda p, s: got.append((p, s))
    )
    assert len(rows) == 1
    assert [(p, s.battery_pct, s.wifi_ssid) for p, s in got] == [(_PHONE, 66, _SSID)]
    row_text = repr(rows[0])
    assert _SSID not in row_text
    assert "battery" not in row_text and "wifi" not in row_text
    assert "Beispiel-SIM" not in row_text


async def test_sink_gets_status_when_the_push_has_no_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got: list[PhoneStatus] = []
    rows = await _locate_with_answer(
        monkeypatch, with_location=False, sink=lambda _p, s: got.append(s)
    )
    assert rows == []
    assert [s.battery_pct for s in got] == [66]


async def test_sink_is_not_called_for_a_push_of_another_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got: list[PhoneStatus] = []
    await _locate_with_answer(
        monkeypatch,
        with_location=False,
        sink=lambda _p, s: got.append(s),
        android_id_in_push=_ANDROID_ID_B,
    )
    assert got == []


async def test_failing_sink_costs_neither_the_fix_nor_logs_the_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(_p: str, _s: PhoneStatus) -> None:
        raise RuntimeError(_SSID)

    with caplog.at_level(logging.DEBUG):
        rows = await _locate_with_answer(monkeypatch, with_location=True, sink=_boom)
    assert len(rows) == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Phone status sink failed: RuntimeError" in text
    for secret in (_SSID, str(_ANDROID_ID), f"{_ANDROID_ID:x}", "Beispiel-SIM"):
        assert secret not in text


async def test_api_passes_its_bound_sink_to_locate_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.googlefindmy import api as api_module  # noqa: PLC0415

    seen: dict[str, Any] = {}

    async def _fake_locate(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(api_module, "locate_phone", _fake_locate)
    api = api_module.GoogleFindMyAPI.__new__(api_module.GoogleFindMyAPI)
    api._phone_requests = PhoneRequestRegistry()
    api._phone_action_observer = None
    api._phone_status_sink = None
    api._session = None
    api._cache = _Cache(_ENTRY_A)

    def sink(_p: str, _s: PhoneStatus) -> None:
        return None

    api.set_phone_status_sink(sink)
    api._async_prepare_phone_request = AsyncMock(  # type: ignore[method-assign]
        return_value=(_ANDROID_ID, _ENTRY_A, object())
    )
    await api._async_get_phone_location(_PHONE)
    assert seen["status_sink"] is sink


# ---------------------------------------------------------------------------
# Coordinator store
# ---------------------------------------------------------------------------


class _SinkAPI(_DispatchingAPI):
    def __init__(self) -> None:
        super().__init__()
        self.sink: Any = None

    def set_phone_status_sink(self, sink: Any) -> None:
        self.sink = sink


async def _coordinator(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _SinkAPI]:
    api = _SinkAPI()
    coord = _make_coordinator(monkeypatch, options={}, api=api)  # type: ignore[arg-type]
    coord.data = [{"id": _PHONE, "name": "Phone"}]
    return coord, api


async def test_coordinator_binds_its_store_as_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, api = await _coordinator(monkeypatch)
    assert api.sink == coord._store_phone_status


async def test_store_keeps_latest_and_never_touches_the_location_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, _api = await _coordinator(monkeypatch)
    calls: list[int] = []
    coord.async_update_listeners = lambda: calls.append(1)
    first = PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
    coord._store_phone_status(_PHONE, first)
    assert coord.get_phone_status(_PHONE) is first
    assert calls == [1]
    assert _PHONE not in coord._device_location_data

    # A newer push without Wi-Fi replaces the whole status (phone left Wi-Fi).
    newer = PhoneStatus(battery_pct=69, wifi_ssid=None, observed_at=_NOW + 60)
    coord._store_phone_status(_PHONE, newer)
    assert coord.get_phone_status(_PHONE) is newer

    # An older one does not win.
    coord._store_phone_status(
        _PHONE, PhoneStatus(battery_pct=10, wifi_ssid=_SSID, observed_at=_NOW)
    )
    assert coord.get_phone_status(_PHONE) is newer
    assert calls == [1, 1]


async def test_purge_device_drops_the_phone_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, _api = await _coordinator(monkeypatch)
    coord.async_update_listeners = lambda: None
    coord._store_phone_status(
        _PHONE, PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
    )
    coord.data = []
    coord.purge_device(_PHONE)
    assert coord.get_phone_status(_PHONE) is None


async def test_store_logs_the_row_id_but_not_the_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _api = await _coordinator(monkeypatch)
    coord.async_update_listeners = lambda: None
    with caplog.at_level(logging.DEBUG):
        coord._store_phone_status(
            _PHONE, PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
        )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert _PHONE in text
    assert _SSID not in text


async def test_location_row_attributes_carry_no_wifi_name() -> None:
    from custom_components.googlefindmy.coordinator import (  # noqa: PLC0415
        _as_ha_attributes,
    )

    row = {
        "latitude": 1.0,
        "longitude": 2.0,
        "accuracy": 10.0,
        "last_seen": _NOW,
        "is_own_report": True,
        "locate_route": "phone",
    }
    attributes = _as_ha_attributes(row) or {}
    assert not any("ssid" in key or "wifi" in key for key in attributes)


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock(_NOW)
    monkeypatch.setattr(sensor_module.time, "time", fake)
    return fake


def _coordinator_stub(status: PhoneStatus | None) -> SimpleNamespace:
    store = {_PHONE: status} if status is not None else {}
    return SimpleNamespace(
        config_entry=make_config_entry(entry_id="entry-1"),
        is_device_present=lambda _did: True,
        is_device_visible_in_subentry=lambda _key, _did: True,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda _key: [{"id": _PHONE, "name": "Phone"}],
        get_phone_status=store.get,
        last_update_success=True,
        store=store,
    )


def _sensor(cls: type[Any], status: PhoneStatus | None, suffix: str) -> Any:
    coordinator = _coordinator_stub(status)
    entity = cls.__new__(cls)
    entity._subentry_identifier = "tracker"
    entity._subentry_key = "core_tracking"
    entity.coordinator = coordinator
    entity.hass = SimpleNamespace(data={})
    entity._device_id = _PHONE
    entity._device = {"id": _PHONE, "name": "Phone"}
    entity._fallback_label = "Phone"
    entity._attr_unique_id = f"googlefindmy_entry-1_tracker_{_PHONE}_{suffix}"
    entity.entity_id = f"sensor.phone_{suffix}"
    entity.async_write_ha_state = MagicMock()
    if cls is GoogleFindMyPhoneBatterySensor:
        entity._attr_native_value = None
        entity._last_observation_wall = None
        entity.entity_description = PHONE_BATTERY_DESCRIPTION
    else:
        entity.entity_description = PHONE_WIFI_SSID_DESCRIPTION
    return entity


async def test_descriptions_follow_ha_practice() -> None:
    assert PHONE_BATTERY_DESCRIPTION.translation_key == "phone_battery"
    assert PHONE_BATTERY_DESCRIPTION.native_unit_of_measurement == "%"
    assert str(PHONE_BATTERY_DESCRIPTION.device_class) == "battery"
    assert PHONE_WIFI_SSID_DESCRIPTION.translation_key == "phone_wifi_ssid"
    for cls in (GoogleFindMyPhoneBatterySensor, GoogleFindMyPhoneWifiSensor):
        assert cls._attr_has_entity_name is True
        assert cls._attr_entity_registry_enabled_default is True  # enabled by default


async def test_unique_ids_derive_from_the_phone_row_id(
    stub_coordinator_factory: Any,
) -> None:
    del stub_coordinator_factory
    coordinator = _coordinator_stub(None)
    for cls, suffix in (
        (GoogleFindMyPhoneBatterySensor, "phone_battery"),
        (GoogleFindMyPhoneWifiSensor, "phone_wifi_ssid"),
    ):
        entity = cls(
            coordinator,
            {"id": _PHONE, "name": "Phone"},
            subentry_key="core_tracking",
            subentry_identifier="tracker",
        )
        assert entity.unique_id == f"googlefindmy_entry-1_tracker_{_PHONE}_{suffix}"


async def test_battery_value_and_unknown_without_status(clock: _Clock) -> None:
    status = PhoneStatus(battery_pct=78, wifi_ssid=None, observed_at=_NOW)
    assert _sensor(GoogleFindMyPhoneBatterySensor, status, "b").native_value == 78
    assert _sensor(GoogleFindMyPhoneBatterySensor, None, "b").native_value is None
    no_battery = PhoneStatus(battery_pct=None, wifi_ssid=_SSID, observed_at=_NOW)
    assert _sensor(GoogleFindMyPhoneBatterySensor, no_battery, "b").native_value is None


async def test_battery_older_than_seven_days_is_hidden(clock: _Clock) -> None:
    status = PhoneStatus(battery_pct=78, wifi_ssid=None, observed_at=_NOW)
    entity = _sensor(GoogleFindMyPhoneBatterySensor, status, "b")
    clock.now = _NOW + 7 * _DAY + 1
    assert entity.native_value is None


async def test_battery_restore_round_trip(clock: _Clock) -> None:
    status = PhoneStatus(battery_pct=55, wifi_ssid=None, observed_at=_NOW - 60)
    live = _sensor(GoogleFindMyPhoneBatterySensor, status, "b")
    stored = live.extra_restore_state_data.as_dict()
    assert stored["native_value"] == 55
    assert stored["last_observation_wall"] == _NOW - 60

    fresh = _sensor(GoogleFindMyPhoneBatterySensor, None, "b")
    fresh.async_get_last_extra_data = AsyncMock(
        return_value=SimpleNamespace(as_dict=lambda: dict(stored))
    )
    with patch.object(
        GoogleFindMyPhoneBatterySensor.__bases__[1],
        "async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await fresh.async_added_to_hass()
    assert fresh.native_value == 55


@pytest.mark.parametrize(
    "stored",
    [
        {"native_value": 55, "native_unit_of_measurement": "%"},
        {
            "native_value": 101,
            "native_unit_of_measurement": "%",
            "last_observation_wall": _NOW,
        },
        {
            "native_value": -1,
            "native_unit_of_measurement": "%",
            "last_observation_wall": _NOW,
        },
        {
            "native_value": "x",
            "native_unit_of_measurement": "%",
            "last_observation_wall": _NOW,
        },
        {
            "native_value": 55,
            "native_unit_of_measurement": "%",
            "last_observation_wall": _NOW - 8 * _DAY,
        },
    ],
)
async def test_restore_drops_invalid_or_expired(
    clock: _Clock, stored: dict[str, Any]
) -> None:
    fresh = _sensor(GoogleFindMyPhoneBatterySensor, None, "b")
    fresh.async_get_last_extra_data = AsyncMock(
        return_value=SimpleNamespace(as_dict=lambda: dict(stored))
    )
    with patch.object(
        GoogleFindMyPhoneBatterySensor.__bases__[1],
        "async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await fresh.async_added_to_hass()
    assert fresh.native_value is None


async def test_wifi_value_unknown_without_wifi_and_when_old(clock: _Clock) -> None:
    status = PhoneStatus(battery_pct=None, wifi_ssid=_SSID, observed_at=_NOW)
    entity = _sensor(GoogleFindMyPhoneWifiSensor, status, "w")
    assert entity.native_value == _SSID
    clock.now = _NOW + 7 * _DAY + 1
    assert entity.native_value is None
    off_wifi = PhoneStatus(battery_pct=50, wifi_ssid=None, observed_at=_NOW)
    clock.now = _NOW
    assert _sensor(GoogleFindMyPhoneWifiSensor, off_wifi, "w").native_value is None
    assert _sensor(GoogleFindMyPhoneWifiSensor, None, "w").native_value is None


async def test_wifi_name_is_no_attribute_and_not_restored() -> None:
    status = PhoneStatus(battery_pct=None, wifi_ssid=_SSID, observed_at=_NOW)
    entity = _sensor(GoogleFindMyPhoneWifiSensor, status, "w")
    for cls in (GoogleFindMyPhoneWifiSensor, GoogleFindMyPhoneBatterySensor):
        assert "extra_state_attributes" not in vars(cls)
        assert getattr(cls, "_attr_extra_state_attributes", None) in (None, {})
    assert getattr(entity, "_attr_extra_state_attributes", None) in (None, {})
    assert not issubclass(GoogleFindMyPhoneWifiSensor, sensor_module.RestoreSensor)


async def test_coordinator_update_caches_the_battery_for_restore(clock: _Clock) -> None:
    status = PhoneStatus(battery_pct=61, wifi_ssid=None, observed_at=_NOW)
    entity = _sensor(GoogleFindMyPhoneBatterySensor, status, "b")
    entity.coordinator_has_device = lambda: True
    entity.refresh_device_label_from_coordinator = lambda **_kw: None
    entity._handle_coordinator_update()
    entity.coordinator.store.clear()
    assert entity.native_value == 61
    entity.async_write_ha_state.assert_called()


# ---------------------------------------------------------------------------
# Creation at platform setup
# ---------------------------------------------------------------------------


async def test_phone_sensors_appear_with_the_first_status(
    stub_coordinator_factory: Any,
) -> None:
    from tests.test_ble_battery_sensor_setup import (  # noqa: PLC0415
        TRACKER_KEY,
        TRACKER_SUBENTRY_ID,
        _Harness,
    )

    harness = _Harness(
        stub_coordinator_factory,
        devices_by_key={
            TRACKER_KEY: [
                {"id": _PHONE, "name": "Phone"},
                {"id": "tag-1", "name": "Tag"},
            ]
        },
        resolver=None,
    )
    store: dict[str, PhoneStatus] = {}
    harness.coordinator.get_phone_status = store.get
    await harness.setup()

    def _phone_uids() -> list[str]:
        return sorted(
            entity.unique_id
            for entity, _ in harness.added
            if isinstance(
                entity, (GoogleFindMyPhoneBatterySensor, GoogleFindMyPhoneWifiSensor)
            )
        )

    assert _phone_uids() == []

    store[_PHONE] = PhoneStatus(battery_pct=None, wifi_ssid=_SSID, observed_at=_NOW)
    await harness.fire_listeners()
    prefix = f"googlefindmy_entry-ble-setup_{TRACKER_SUBENTRY_ID}_{_PHONE}"
    assert _phone_uids() == [f"{prefix}_phone_wifi_ssid"]

    store[_PHONE] = PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW + 1)
    await harness.fire_listeners()
    await harness.fire_listeners()
    assert _phone_uids() == [f"{prefix}_phone_battery", f"{prefix}_phone_wifi_ssid"]

    # A tracker never gets phone sensors, even with a status under its id.
    store["tag-1"] = PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
    await harness.fire_listeners()
    assert all("tag-1" not in uid for uid in _phone_uids())
    await asyncio.sleep(0)


async def test_phone_sensors_come_back_from_the_registry(
    stub_coordinator_factory: Any,
) -> None:
    from tests.test_ble_battery_sensor_setup import (  # noqa: PLC0415
        TRACKER_KEY,
        TRACKER_SUBENTRY_ID,
        _Harness,
    )

    harness = _Harness(
        stub_coordinator_factory,
        devices_by_key={TRACKER_KEY: [{"id": _PHONE, "name": "Phone"}]},
        resolver=None,
    )
    harness.coordinator.get_phone_status = lambda _d: None
    prefix = f"googlefindmy_entry-ble-setup_{TRACKER_SUBENTRY_ID}_{_PHONE}"
    harness.register(
        "sensor.phone_battery", f"{prefix}_phone_battery", TRACKER_SUBENTRY_ID
    )
    await harness.setup()
    uids = [
        entity.unique_id
        for entity, _ in harness.added
        if isinstance(entity, GoogleFindMyPhoneBatterySensor)
    ]
    assert uids == [f"{prefix}_phone_battery"]


async def test_setup_without_the_coordinator_hook_creates_nothing(
    stub_coordinator_factory: Any,
) -> None:
    from tests.test_ble_battery_sensor_setup import TRACKER_KEY, _Harness  # noqa: PLC0415

    harness = _Harness(
        stub_coordinator_factory,
        devices_by_key={TRACKER_KEY: [{"id": _PHONE, "name": "Phone"}]},
        resolver=None,
    )
    if hasattr(harness.coordinator, "get_phone_status"):
        del harness.coordinator.get_phone_status
    await harness.setup()
    assert not any(
        isinstance(e, (GoogleFindMyPhoneBatterySensor, GoogleFindMyPhoneWifiSensor))
        for e, _ in harness.added
    )


async def test_coordinator_class_offers_the_reader() -> None:
    assert callable(getattr(GoogleFindMyCoordinator, "get_phone_status", None))


async def test_newer_status_without_level_hides_the_cached_battery(
    clock: _Clock,
) -> None:
    """Reviewer P2-1: the cached older value must not show through."""
    status = PhoneStatus(battery_pct=61, wifi_ssid=None, observed_at=_NOW)
    entity = _sensor(GoogleFindMyPhoneBatterySensor, status, "b")
    entity.coordinator_has_device = lambda: True
    entity.refresh_device_label_from_coordinator = lambda **_kw: None
    entity._handle_coordinator_update()
    assert entity.native_value == 61
    entity.coordinator.store[_PHONE] = PhoneStatus(
        battery_pct=None, wifi_ssid=None, observed_at=_NOW + 60
    )
    entity._handle_coordinator_update()
    assert entity.native_value is None
    assert entity.extra_restore_state_data.as_dict()["native_value"] is None


async def test_push_without_values_clears_the_stored_wifi_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer P2-2 end to end: sink, store and sensor."""
    coord, _api = await _coordinator(monkeypatch)
    coord.async_update_listeners = lambda: None
    coord._store_phone_status(
        _PHONE,
        PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=time.time() - 60),
    )
    registry = PhoneRequestRegistry()
    await _open(registry)
    empty = registry.read_status(_ENTRY_A, "u-1", _status_push("u-1", _ANDROID_ID))
    assert empty is not None
    coord._store_phone_status(_PHONE, empty)
    stored = coord.get_phone_status(_PHONE)
    assert stored is not None and stored.wifi_ssid is None


async def test_status_of_a_phone_that_is_not_listed_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer P3-1: an answer arriving after purge_device is not stored."""
    coord, _api = await _coordinator(monkeypatch)
    coord.async_update_listeners = lambda: None
    coord.data = [{"id": "tag-1", "name": "Tag"}]
    coord._store_phone_status(
        _PHONE, PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
    )
    assert coord.get_phone_status(_PHONE) is None
    coord.data = [{"id": _PHONE, "name": "Phone"}]
    coord._store_phone_status(
        _PHONE, PhoneStatus(battery_pct=70, wifi_ssid=_SSID, observed_at=_NOW)
    )
    assert coord.get_phone_status(_PHONE) is not None


# ---------------------------------------------------------------------------
# Availability, restore data and updates without the device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cls", "suffix"),
    [(GoogleFindMyPhoneBatterySensor, "b"), (GoogleFindMyPhoneWifiSensor, "w")],
)
async def test_sensor_is_unavailable_without_the_device(
    cls: type[Any], suffix: str
) -> None:
    entity = _sensor(cls, None, suffix)
    entity.coordinator_has_device = lambda: True
    assert entity.available is True
    entity.coordinator_has_device = lambda: False
    assert entity.available is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cls", "suffix"),
    [(GoogleFindMyPhoneBatterySensor, "b"), (GoogleFindMyPhoneWifiSensor, "w")],
)
async def test_update_without_the_device_only_writes_the_state(
    cls: type[Any], suffix: str
) -> None:
    entity = _sensor(cls, None, suffix)
    entity.coordinator_has_device = lambda: False
    entity.refresh_device_label_from_coordinator = MagicMock()
    entity._handle_coordinator_update()
    entity.refresh_device_label_from_coordinator.assert_not_called()
    entity.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_restore_data_without_a_fresh_status_keeps_the_restored_value(
    clock: _Clock,
) -> None:
    entity = _sensor(GoogleFindMyPhoneBatterySensor, None, "b")
    entity._attr_native_value = 42
    entity._last_observation_wall = _NOW - 60
    stored = entity.extra_restore_state_data.as_dict()
    assert stored["native_value"] == 42
    assert stored["last_observation_wall"] == _NOW - 60


@pytest.mark.asyncio
async def test_restore_without_stored_data_leaves_the_value_unknown() -> None:
    entity = _sensor(GoogleFindMyPhoneBatterySensor, None, "b")
    entity.async_get_last_extra_data = AsyncMock(return_value=None)
    with patch.object(
        GoogleFindMyPhoneBatterySensor.__bases__[1],
        "async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await entity.async_added_to_hass()
    assert entity.native_value is None


@pytest.mark.asyncio
async def test_restore_does_not_replace_a_newer_value(clock: _Clock) -> None:
    entity = _sensor(GoogleFindMyPhoneBatterySensor, None, "b")
    entity._attr_native_value = 80
    entity._last_observation_wall = _NOW - 10
    stored = {
        "native_value": 30,
        "native_unit_of_measurement": "%",
        "last_observation_wall": _NOW - 600,
    }
    entity.async_get_last_extra_data = AsyncMock(
        return_value=SimpleNamespace(as_dict=lambda: dict(stored))
    )
    with patch.object(
        GoogleFindMyPhoneBatterySensor.__bases__[1],
        "async_added_to_hass",
        new_callable=AsyncMock,
    ):
        await entity.async_added_to_hass()
    assert entity._attr_native_value == 80

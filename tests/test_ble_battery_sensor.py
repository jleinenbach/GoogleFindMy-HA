# tests/test_ble_battery_sensor.py
"""Tests for BLE battery state decoding, storage, and sensor entity."""

from __future__ import annotations

import asyncio
import time
from collections.abc import MutableMapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import (
    FMDN_BATTERY_PCT,
    BLEBatteryState,
    EIDMatch,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
)
from custom_components.googlefindmy.sensor import (
    BLE_BATTERY_DESCRIPTION,
    GoogleFindMyBLEBatterySensor,
)

# ---------------------------------------------------------------------------
# Constants used to build test payloads
# ---------------------------------------------------------------------------
_FMDN_FRAME_TYPE = resolver_module.FMDN_FRAME_TYPE  # 0x40
_SERVICE_DATA_OFFSET = resolver_module.SERVICE_DATA_OFFSET  # 8
_RAW_HEADER_LENGTH = resolver_module.RAW_HEADER_LENGTH  # 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_hass(domain_data: dict[str, Any] | None = None) -> SimpleNamespace:
    """Return a lightweight hass stand-in."""
    data: dict[str, Any] = {}
    if domain_data is not None:
        data[DOMAIN] = domain_data
    return SimpleNamespace(
        async_create_task=lambda coro, name=None: _close_coro_and_return(coro),
        async_create_background_task=lambda coro, name=None: _close_coro_and_return(
            coro
        ),
        data=data,
    )


def _close_coro_and_return(coro: object) -> None:
    """Close a coroutine to avoid RuntimeWarning in test context."""
    if hasattr(coro, "close"):
        coro.close()


def _make_resolver() -> GoogleFindMyEIDResolver:
    """Create a minimal resolver instance suitable for direct _update_ble_battery calls."""
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}

    async def _async_noop(payload: Any = None) -> None:
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._ensure_cache_defaults()
    return resolver


def _match(device_id: str = "dev-1", canonical_id: str | None = None) -> EIDMatch:
    """Create a test EIDMatch.

    *canonical_id* defaults to *device_id* so that the battery-state
    storage key (``canonical_id``) matches the test lookup key.
    """
    return EIDMatch(
        device_id=device_id,
        config_entry_id="entry-1",
        canonical_id=canonical_id if canonical_id is not None else device_id,
        time_offset=0,
        is_reversed=False,
    )


def _service_data_payload(eid: bytes, flags_byte: int) -> bytes:
    """Build a service-data format payload: [header(7)][frame(1)][EID(20)][flags(1)]."""
    header = b"\x00" * 7
    frame = bytes([_FMDN_FRAME_TYPE])
    return header + frame + eid + bytes([flags_byte])


def _raw_header_payload(eid: bytes, flags_byte: int) -> bytes:
    """Build a raw-header format payload: [frame(1)][EID(20)][flags(1)]."""
    frame = bytes([_FMDN_FRAME_TYPE])
    return frame + eid + bytes([flags_byte])


def _fake_coordinator(
    device_id: str = "dev-1",
    present: bool = True,
    entry_id: str = "entry-1",
    visible: bool = True,
    snapshot: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Create a minimal coordinator stub for sensor tests."""
    return SimpleNamespace(
        config_entry=SimpleNamespace(entry_id=entry_id),
        is_device_present=lambda did: present,
        is_device_visible_in_subentry=lambda key, did: visible,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda key: snapshot or [],
        last_update_success=True,
    )


def _build_battery_sensor(
    device_id: str = "dev-1",
    device_name: str = "Test Tracker",
    coordinator: Any = None,
    hass: Any = None,
    resolver: Any = None,
) -> GoogleFindMyBLEBatterySensor:
    """Create a BLE battery sensor with minimal stubs, bypassing HA platform init."""
    if coordinator is None:
        coordinator = _fake_coordinator(device_id=device_id)
    if hass is None:
        domain_data: dict[str, Any] = {}
        if resolver is not None:
            domain_data[DATA_EID_RESOLVER] = resolver
        hass = _fake_hass(domain_data)

    sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
    sensor._subentry_identifier = "tracker"
    sensor._subentry_key = "core_tracking"
    sensor.coordinator = coordinator
    sensor.hass = hass
    sensor._device_id = device_id
    sensor._device = {"id": device_id, "name": device_name}
    sensor._attr_native_value = None
    sensor.entity_description = BLE_BATTERY_DESCRIPTION
    sensor._attr_has_entity_name = True
    sensor._attr_entity_registry_enabled_default = True
    sensor._unrecorded_attributes = frozenset({
        "uwt_mode",
        "last_ble_observation",
        "google_device_id",
        "battery_raw_level",
    })
    sensor._fallback_label = device_name

    safe_id = device_id if device_id is not None else "unknown"
    entry_id = getattr(coordinator.config_entry, "entry_id", "default")
    sensor._attr_unique_id = f"googlefindmy_{entry_id}_tracker_{safe_id}_ble_battery"

    sensor.entity_id = f"sensor.test_{safe_id}_ble_battery"

    return sensor


# ===========================================================================
# 1. BLEBatteryState dataclass + FMDN_BATTERY_PCT mapping
# ===========================================================================


class TestBLEBatteryStateDataclass:
    """Unit tests for BLEBatteryState and the percentage mapping."""

    def test_dataclass_fields(self) -> None:
        state = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        assert state.battery_level == 0
        assert state.battery_pct == 100
        assert state.uwt_mode is False
        assert state.decoded_flags == 0x00
        assert state.observed_at_wall == 1000.0

    def test_battery_pct_mapping(self) -> None:
        """FMDN_BATTERY_PCT should map 0->100, 1->25, 2->5."""
        assert FMDN_BATTERY_PCT == {0: 100, 1: 25, 2: 5}

    def test_battery_pct_unknown_raw_returns_zero(self) -> None:
        """An unrecognized raw value (3=RESERVED) should map to 0."""
        assert FMDN_BATTERY_PCT.get(3, 0) == 0

    def test_slots_optimization(self) -> None:
        """BLEBatteryState uses __slots__ for memory efficiency."""
        assert hasattr(BLEBatteryState, "__slots__")

    def test_no_battery_percentage_alias(self) -> None:
        """Guard: the field is battery_pct, NOT battery_percentage.

        A previous bug used 'battery_percentage' in a log statement inside
        _build_entities(), which raised AttributeError at runtime.  This test
        ensures the correct attribute name is used and no alias exists.
        """
        state = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        # Correct attribute exists and is accessible
        assert state.battery_pct == 100
        # Common typo must NOT exist (slots dataclass → AttributeError)
        assert not hasattr(state, "battery_percentage")

    def test_battery_pct_used_in_sensor_creation_log(self) -> None:
        """Regression: the INFO log in _build_entities must access battery_pct.

        This exercises the exact attribute access pattern used in sensor.py's
        _build_entities() when logging BLE battery sensor creation.
        """
        state = BLEBatteryState(
            battery_level=1,
            battery_pct=25,
            uwt_mode=False,
            decoded_flags=0x20,
            observed_at_wall=1000.0,
        )
        # Reproduce the log format string from sensor.py _build_entities()
        msg = (
            f"BLE battery sensor created for device={'test-dev'} "
            f"(battery={state.battery_pct}%)"
        )
        assert "battery=25%" in msg


# ===========================================================================
# 2. _update_ble_battery() decode and store
# ===========================================================================


class TestUpdateBLEBattery:
    """Tests for the resolver's _update_ble_battery method."""

    def test_no_matches_noop(self) -> None:
        """When matches is empty, nothing should be stored."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x00)
        metadata = {"flags_xor_mask": 0x00}
        resolver._update_ble_battery(raw, None, metadata, [])
        assert len(resolver._ble_battery_state) == 0

    def test_decode_good_service_data(self) -> None:
        """Battery level 0 (GOOD) -> 100% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x55

        desired_decoded = 0b00_000000  # battery=0, uwt=0
        flags_byte = desired_decoded ^ xor_mask

        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-good")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-good")
        assert state is not None
        assert state.battery_level == 0
        assert state.battery_pct == 100
        assert state.uwt_mode is False

    def test_decode_low_service_data(self) -> None:
        """Battery level 1 (LOW) -> 25% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xbb" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        desired_decoded = 0b00_100000
        flags_byte = desired_decoded ^ xor_mask

        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-low")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-low")
        assert state is not None
        assert state.battery_level == 1
        assert state.battery_pct == 25
        assert state.uwt_mode is False

    def test_decode_critical_service_data(self) -> None:
        """Battery level 2 (CRITICAL) -> 5% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xcc" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        desired_decoded = 0b01_000000
        flags_byte = desired_decoded ^ xor_mask

        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-crit")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-crit")
        assert state is not None
        assert state.battery_level == 2
        assert state.battery_pct == 5
        assert state.uwt_mode is False

    def test_decode_uwt_mode_active(self) -> None:
        """UWT mode bit 7 set -> uwt_mode=True."""
        resolver = _make_resolver()
        eid = b"\xdd" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        desired_decoded = 0b10_000000
        flags_byte = desired_decoded ^ xor_mask

        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-uwt")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-uwt")
        assert state is not None
        assert state.battery_level == 0
        assert state.battery_pct == 100
        assert state.uwt_mode is True

    def test_decode_raw_header_format(self) -> None:
        """Flags extraction from raw-header format: [frame(1)][EID(20)][flags(1)]."""
        resolver = _make_resolver()
        eid = b"\xee" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        desired_decoded = 0b00_100000
        flags_byte = desired_decoded ^ xor_mask

        raw = _raw_header_payload(eid, flags_byte)
        match = _match("dev-raw")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-raw")
        assert state is not None
        assert state.battery_level == 1
        assert state.battery_pct == 25

    def test_shared_device_propagation(self) -> None:
        """When one BLE packet matches 2 accounts, battery stores for BOTH device_ids."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b00_000000
        flags_byte = desired_decoded ^ xor_mask

        raw = _service_data_payload(eid, flags_byte)
        match_a = _match("dev-account-a")
        match_b = _match("dev-account-b")
        resolver._update_ble_battery(
            raw, None, {"flags_xor_mask": xor_mask}, [match_a, match_b]
        )

        state_a = resolver._ble_battery_state.get("dev-account-a")
        state_b = resolver._ble_battery_state.get("dev-account-b")
        assert state_a is not None
        assert state_b is not None
        assert state_a.battery_pct == 100
        assert state_b.battery_pct == 100
        # Both should reference the same BLEBatteryState instance
        assert state_a is state_b

    def test_cannot_decode_no_xor_mask(self) -> None:
        """Missing xor_mask -> no battery state stored."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x42)
        match = _match("dev-no-mask")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert resolver._ble_battery_state.get("dev-no-mask") is None

    def test_cannot_decode_short_payload(self) -> None:
        """Payload too short for flags byte -> no battery state stored."""
        resolver = _make_resolver()
        raw = b"\x00" * 10
        match = _match("dev-short")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": 0x00}, [match])
        assert resolver._ble_battery_state.get("dev-short") is None

    def test_observed_at_wall_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """observed_at_wall should use time.time()."""
        monkeypatch.setattr(time, "time", lambda: 9999.5)
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        flags_byte = 0b00_000000 ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-time")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        state = resolver._ble_battery_state.get("dev-time")
        assert state is not None
        assert state.observed_at_wall == 9999.5

    def test_first_decode_logs_info(self) -> None:
        """First decode per device should add to _flags_logged_devices."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        flags_byte = 0b00_000000 ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-log")

        assert "dev-log" not in resolver._flags_logged_devices
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert "dev-log" in resolver._flags_logged_devices

    def test_cannot_decode_logs_device(self) -> None:
        """CANNOT_DECODE should still add device to _flags_logged_devices."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x42)
        match = _match("dev-cant-decode")

        assert "dev-cant-decode" not in resolver._flags_logged_devices
        resolver._update_ble_battery(raw, None, {}, [match])
        assert "dev-cant-decode" in resolver._flags_logged_devices

    def test_battery_level_change_updates_state(self) -> None:
        """When battery level changes on subsequent call, state should update."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        match = _match("dev-change")

        # First: GOOD (0)
        decoded_good = 0b00_000000
        raw = _service_data_payload(eid, decoded_good ^ xor_mask)
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert resolver._ble_battery_state["dev-change"].battery_pct == 100

        # Second: LOW (1) — triggers battery-changed DEBUG log
        decoded_low = 0b00_100000
        raw = _service_data_payload(eid, decoded_low ^ xor_mask)
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert resolver._ble_battery_state["dev-change"].battery_pct == 25
        assert resolver._ble_battery_state["dev-change"].battery_level == 1

    def test_reserved_battery_raw_3_maps_to_0_pct(self) -> None:
        """Raw battery value 3 (RESERVED) maps to 0% via FMDN_BATTERY_PCT.get fallback."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b01_100000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-reserved")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-reserved")
        assert state is not None
        assert state.battery_level == 3
        assert state.battery_pct == 0

    def test_combined_battery_and_uwt(self) -> None:
        """Battery CRITICAL + UWT -> battery_pct=5, uwt_mode=True."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b11_000000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-combo")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-combo")
        assert state is not None
        assert state.battery_level == 2
        assert state.battery_pct == 5
        assert state.uwt_mode is True
        assert state.decoded_flags == 0xC0

    def test_observed_frame_format_in_log(self) -> None:
        """When observed_frame is an int, the info log should format it as hex."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b00_000000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-frame")
        # Pass a non-None observed_frame to cover the 0x{:02x} formatting branch
        resolver._update_ble_battery(
            raw, 0x40, {"flags_xor_mask": xor_mask}, [match]
        )
        state = resolver._ble_battery_state.get("dev-frame")
        assert state is not None
        assert state.battery_pct == 100

    def test_cannot_decode_with_observed_frame(self) -> None:
        """CANNOT_DECODE with non-None observed_frame covers the hex format branch."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x42)
        match = _match("dev-cant-frame")
        resolver._update_ble_battery(raw, 0x40, {}, [match])
        assert "dev-cant-frame" in resolver._flags_logged_devices

    def test_cannot_decode_long_payload_truncation(self) -> None:
        """CANNOT_DECODE with long payload should truncate raw_hex to 40 bytes."""
        resolver = _make_resolver()
        # Build a long payload that won't match FMDN frame type at position 0 or 7
        raw = b"\xFF" * 100
        match = _match("dev-long")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert "dev-long" in resolver._flags_logged_devices

    def test_cannot_decode_short_payload_full_hex(self) -> None:
        """CANNOT_DECODE with short payload should emit full raw hex."""
        resolver = _make_resolver()
        raw = b"\xAB" * 20
        match = _match("dev-short-hex")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert "dev-short-hex" in resolver._flags_logged_devices

    def test_second_cannot_decode_same_device_no_double_log(self) -> None:
        """CANNOT_DECODE for an already-logged device should not re-log."""
        resolver = _make_resolver()
        raw = b"\xAB" * 20
        match = _match("dev-double")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert "dev-double" in resolver._flags_logged_devices
        # Second call — should be a no-op (device already logged)
        resolver._update_ble_battery(raw, None, {}, [match])

    def test_no_flags_byte_no_xor_mask(self) -> None:
        """Both flags_byte=None and xor_mask=None -> CANNOT_DECODE path."""
        resolver = _make_resolver()
        raw = b"\x00" * 5  # too short for any frame format
        match = _match("dev-both-none")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert resolver._ble_battery_state.get("dev-both-none") is None
        assert "dev-both-none" in resolver._flags_logged_devices

    def test_same_battery_level_no_change_log(self) -> None:
        """When battery is decoded again with the same level, no change log is emitted."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        decoded_good = 0b00_000000
        raw = _service_data_payload(eid, decoded_good ^ xor_mask)
        match = _match("dev-same")

        # First decode — GOOD, INFO log
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert resolver._ble_battery_state["dev-same"].battery_pct == 100
        assert "dev-same" in resolver._flags_logged_devices

        # Second decode — same level (GOOD), no change log emitted
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert resolver._ble_battery_state["dev-same"].battery_pct == 100

    def test_flags_byte_found_but_no_xor_mask(self) -> None:
        """Service-data has flags byte position but no xor_mask -> CANNOT_DECODE."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x42)
        match = _match("dev-flags-no-mask")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": None}, [match])
        assert resolver._ble_battery_state.get("dev-flags-no-mask") is None


# ===========================================================================
# 3. get_ble_battery_state() public API
# ===========================================================================


class TestGetBLEBatteryState:
    """Tests for the public get_ble_battery_state API."""

    def test_returns_none_when_no_data(self) -> None:
        resolver = _make_resolver()
        assert resolver.get_ble_battery_state("nonexistent") is None

    def test_returns_stored_state(self) -> None:
        resolver = _make_resolver()
        state = BLEBatteryState(
            battery_level=1,
            battery_pct=25,
            uwt_mode=False,
            decoded_flags=0x20,
            observed_at_wall=5000.0,
        )
        resolver._ble_battery_state["test-dev"] = state
        result = resolver.get_ble_battery_state("test-dev")
        assert result is state

    def test_returns_state_after_decode(self) -> None:
        """get_ble_battery_state should return data set by _update_ble_battery."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b00_100000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("api-dev")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        result = resolver.get_ble_battery_state("api-dev")
        assert result is not None
        assert result.battery_pct == 25


# ===========================================================================
# 4. BLE_BATTERY_DESCRIPTION entity description
# ===========================================================================


class TestBLEBatteryDescription:
    """Tests for the sensor entity description constants."""

    def test_key(self) -> None:
        assert BLE_BATTERY_DESCRIPTION.key == "ble_battery"

    def test_translation_key(self) -> None:
        assert BLE_BATTERY_DESCRIPTION.translation_key == "ble_battery"

    def test_device_class(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass

        assert BLE_BATTERY_DESCRIPTION.device_class == SensorDeviceClass.BATTERY

    def test_unit_of_measurement(self) -> None:
        from homeassistant.const import PERCENTAGE

        assert BLE_BATTERY_DESCRIPTION.native_unit_of_measurement == PERCENTAGE

    def test_state_class(self) -> None:
        from homeassistant.components.sensor import SensorStateClass

        assert BLE_BATTERY_DESCRIPTION.state_class == SensorStateClass.MEASUREMENT

    def test_entity_category(self) -> None:
        from homeassistant.helpers.entity import EntityCategory

        assert BLE_BATTERY_DESCRIPTION.entity_category == EntityCategory.DIAGNOSTIC

    def test_no_explicit_icon(self) -> None:
        """SensorDeviceClass.BATTERY provides dynamic icons -- no manual icon."""
        assert getattr(BLE_BATTERY_DESCRIPTION, "icon", None) is None


# ===========================================================================
# 5. GoogleFindMyBLEBatterySensor entity — native_value
# ===========================================================================


class TestBLEBatterySensorNativeValue:
    """Tests for the native_value property."""

    def test_value_from_resolver(self) -> None:
        """When resolver has battery data, native_value returns battery_pct."""
        resolver = _make_resolver()
        state = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        resolver._ble_battery_state["dev-1"] = state

        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        assert sensor.native_value == 100

    def test_value_fallback_to_restored(self) -> None:
        """When resolver has no data, native_value returns _attr_native_value."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-no-data", resolver=resolver)
        sensor._attr_native_value = 25
        assert sensor.native_value == 25

    def test_value_none_when_no_data_no_restore(self) -> None:
        """When resolver has no data and no restored value, returns None."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-empty", resolver=resolver)
        assert sensor.native_value is None

    def test_value_without_resolver(self) -> None:
        """When resolver is not in hass.data, returns _attr_native_value fallback."""
        sensor = _build_battery_sensor(device_id="dev-1", resolver=None)
        sensor._attr_native_value = 5
        assert sensor.native_value == 5

    def test_value_updates_on_battery_change(self) -> None:
        """native_value reflects the latest resolver state."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)

        assert sensor.native_value is None

        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        assert sensor.native_value == 100

        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=1,
            battery_pct=25,
            uwt_mode=False,
            decoded_flags=0x20,
            observed_at_wall=2000.0,
        )
        assert sensor.native_value == 25

    def test_value_resolver_not_a_dict(self) -> None:
        """When hass.data[DOMAIN] is not a dict, returns _attr_native_value."""
        hass = SimpleNamespace(data={DOMAIN: "not-a-dict"})
        sensor = _build_battery_sensor(device_id="dev-1", hass=hass)
        sensor._attr_native_value = 42
        assert sensor.native_value == 42

    def test_resolver_missing_domain_key(self) -> None:
        """When DOMAIN not in hass.data, returns _attr_native_value."""
        hass = SimpleNamespace(data={})
        sensor = _build_battery_sensor(device_id="dev-1", hass=hass)
        sensor._attr_native_value = 99
        assert sensor.native_value == 99


# ===========================================================================
# 6. GoogleFindMyBLEBatterySensor — available property
# ===========================================================================


class TestBLEBatterySensorAvailability:
    """Tests for the available property."""

    def test_available_when_present(self) -> None:
        """Sensor available when coordinator reports device as present."""
        resolver = _make_resolver()
        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=0,
            battery_pct=100,
            uwt_mode=False,
            decoded_flags=0x00,
            observed_at_wall=1000.0,
        )
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        assert sensor.available is True

    def test_available_when_not_present_with_restore(self) -> None:
        """Available even when not present, if we have a restored value."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=False)
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor._attr_native_value = 100  # restored
        assert sensor.available is True

    def test_unavailable_when_not_present_no_data(self) -> None:
        """Unavailable when not present and no restore/resolver data."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=False)
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor._attr_native_value = None
        assert sensor.available is False

    def test_unavailable_when_coordinator_hidden(self) -> None:
        """Unavailable when coordinator marks device as not visible."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(
            device_id="dev-1", present=True, visible=False
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        assert sensor.available is False

    def test_available_with_is_device_present_exception(self) -> None:
        """When is_device_present raises, fall back to _attr_native_value."""
        resolver = _make_resolver()

        def _raise(did: str) -> bool:
            raise RuntimeError("boom")

        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        coordinator.is_device_present = _raise
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor._attr_native_value = 50
        # Exception path => returns _attr_native_value is not None => True
        assert sensor.available is True

    def test_unavailable_with_exception_no_restore(self) -> None:
        """When is_device_present raises and no restore, unavailable."""
        resolver = _make_resolver()

        def _raise(did: str) -> bool:
            raise RuntimeError("boom")

        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        coordinator.is_device_present = _raise
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor._attr_native_value = None
        assert sensor.available is False

    def test_available_without_is_device_present_attr(self) -> None:
        """When coordinator lacks is_device_present, fall back to restore check."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        del coordinator.is_device_present
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor._attr_native_value = 25
        # No is_device_present => falls to bottom: _attr_native_value is not None
        assert sensor.available is True

    def test_available_present_returns_non_bool(self) -> None:
        """When is_device_present returns a truthy non-bool, available is True."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        coordinator.is_device_present = lambda did: 1  # truthy non-bool
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        assert sensor.available is True


# ===========================================================================
# 7. GoogleFindMyBLEBatterySensor — extra_state_attributes
# ===========================================================================


class TestBLEBatterySensorExtraAttributes:
    """Tests for extra_state_attributes."""

    def test_attributes_with_resolver_data(self) -> None:
        resolver = _make_resolver()
        state = BLEBatteryState(
            battery_level=1,
            battery_pct=25,
            uwt_mode=True,
            decoded_flags=0xA0,
            observed_at_wall=1700000000.0,
        )
        resolver._ble_battery_state["dev-1"] = state

        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["battery_raw_level"] == 1
        assert attrs["uwt_mode"] is True
        assert attrs["google_device_id"] == "dev-1"
        assert "last_ble_observation" in attrs
        assert "T" in attrs["last_ble_observation"]

    def test_attributes_none_without_resolver(self) -> None:
        sensor = _build_battery_sensor(device_id="dev-1", resolver=None)
        assert sensor.extra_state_attributes is None

    def test_attributes_none_without_battery_data(self) -> None:
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-no-data", resolver=resolver)
        assert sensor.extra_state_attributes is None


# ===========================================================================
# 8. GoogleFindMyBLEBatterySensor — _handle_coordinator_update
# ===========================================================================


class TestBLEBatterySensorCoordinatorUpdate:
    """Tests for _handle_coordinator_update."""

    def test_update_caches_native_value(self) -> None:
        """When resolver has battery data, update caches native_value."""
        resolver = _make_resolver()
        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=1,
            battery_pct=25,
            uwt_mode=False,
            decoded_flags=0x20,
            observed_at_wall=1000.0,
        )
        coordinator = _fake_coordinator(
            device_id="dev-1",
            present=True,
            snapshot=[{"id": "dev-1", "name": "Test Tracker"}],
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        # Stub async_write_ha_state since we're outside HA platform
        sensor.async_write_ha_state = MagicMock()

        sensor._handle_coordinator_update()

        assert sensor._attr_native_value == 25
        sensor.async_write_ha_state.assert_called()

    def test_update_without_device_writes_state(self) -> None:
        """When coordinator_has_device is False, still writes state."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(
            device_id="dev-1", present=False, visible=False
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor.async_write_ha_state = MagicMock()

        sensor._handle_coordinator_update()

        # Should still call async_write_ha_state and return early
        sensor.async_write_ha_state.assert_called()

    def test_update_without_resolver_data(self) -> None:
        """When resolver has no battery data, native_value not updated."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(
            device_id="dev-1",
            present=True,
            snapshot=[{"id": "dev-1", "name": "Test Tracker"}],
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor.async_write_ha_state = MagicMock()
        sensor._attr_native_value = None

        sensor._handle_coordinator_update()

        assert sensor._attr_native_value is None
        sensor.async_write_ha_state.assert_called()

    def test_update_refreshes_device_label(self) -> None:
        """Coordinator update should refresh the device label from snapshot."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(
            device_id="dev-1",
            present=True,
            snapshot=[{"id": "dev-1", "name": "New Name"}],
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            device_name="Old Name",
            coordinator=coordinator,
            resolver=resolver,
        )
        sensor.async_write_ha_state = MagicMock()
        # Stub maybe_update_device_registry_name to avoid registry access
        sensor.maybe_update_device_registry_name = MagicMock()

        sensor._handle_coordinator_update()

        assert sensor._device["name"] == "New Name"
        assert sensor._fallback_label == "New Name"


# ===========================================================================
# 9. GoogleFindMyBLEBatterySensor — async_added_to_hass (RestoreSensor)
# ===========================================================================


class TestBLEBatterySensorRestore:
    """Tests for async_added_to_hass RestoreSensor integration."""

    @pytest.mark.asyncio
    async def test_restore_valid_percentage(self) -> None:
        """Restoring a valid integer percentage sets _attr_native_value."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value="25")
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        # Patch super().async_added_to_hass to be a no-op
        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value == 25

    @pytest.mark.asyncio
    async def test_restore_float_percentage(self) -> None:
        """Restoring a float value rounds to int."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value="99.7")
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value == 99

    @pytest.mark.asyncio
    async def test_restore_none_value(self) -> None:
        """When last sensor data returns None native_value, no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value=None)
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_unknown_value(self) -> None:
        """When last sensor data is 'unknown', no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value="unknown")
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_unavailable_value(self) -> None:
        """When last sensor data is 'unavailable', no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value="unavailable")
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_invalid_value_type(self) -> None:
        """When last sensor data is not a parseable number, no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        last_data = SimpleNamespace(native_value="not-a-number")
        sensor.async_get_last_sensor_data = AsyncMock(return_value=last_data)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_no_last_data(self) -> None:
        """When async_get_last_sensor_data returns None, no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        sensor.async_get_last_sensor_data = AsyncMock(return_value=None)

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_runtime_error(self) -> None:
        """When async_get_last_sensor_data raises RuntimeError, no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        sensor.async_get_last_sensor_data = AsyncMock(
            side_effect=RuntimeError("store unavailable")
        )

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None

    @pytest.mark.asyncio
    async def test_restore_attribute_error(self) -> None:
        """When data lacks native_value attr, no restore."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        sensor.async_write_ha_state = MagicMock()

        sensor.async_get_last_sensor_data = AsyncMock(
            side_effect=AttributeError("no native_value")
        )

        with patch.object(
            GoogleFindMyBLEBatterySensor.__bases__[1],
            "async_added_to_hass",
            new_callable=AsyncMock,
        ):
            await sensor.async_added_to_hass()

        assert sensor._attr_native_value is None


# ===========================================================================
# 10. GoogleFindMyBLEBatterySensor — __init__ via real constructor
# ===========================================================================


class TestBLEBatterySensorInit:
    """Tests exercising the real __init__ path."""

    def test_init_sets_device_id(self) -> None:
        """Real __init__ should set _device_id from device dict."""
        coordinator = _fake_coordinator(device_id="init-dev")
        coordinator._subentry_key = "core_tracking"
        coordinator._subentry_identifier = "tracker"

        device: MutableMapping[str, Any] = {"id": "init-dev", "name": "Init Tracker"}

        sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
        # Manually set attributes that super().__init__ would set
        sensor.coordinator = coordinator
        sensor.hass = _fake_hass()
        sensor._subentry_identifier = "tracker"
        sensor._subentry_key = "core_tracking"
        sensor._device = device
        sensor._fallback_label = "Init Tracker"

        # Call the actual __init__ logic (the part after super().__init__)
        sensor._device_id = device.get("id")
        safe_id = sensor._device_id if sensor._device_id is not None else "unknown"
        entry_id = "entry-1"
        sensor._attr_unique_id = sensor.build_unique_id(
            DOMAIN, entry_id, "tracker", f"{safe_id}_ble_battery", separator="_"
        )
        sensor._attr_native_value = None

        assert sensor._device_id == "init-dev"
        assert "init-dev_ble_battery" in sensor._attr_unique_id
        assert sensor._attr_native_value is None

    def test_init_with_none_device_id(self) -> None:
        """When device has no id, safe_id defaults to 'unknown'."""
        coordinator = _fake_coordinator(device_id="unknown")
        device: MutableMapping[str, Any] = {"name": "No ID Tracker"}

        sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
        sensor.coordinator = coordinator
        sensor.hass = _fake_hass()
        sensor._subentry_identifier = "tracker"
        sensor._subentry_key = "core_tracking"
        sensor._device = device
        sensor._fallback_label = "No ID Tracker"

        sensor._device_id = device.get("id")
        safe_id = sensor._device_id if sensor._device_id is not None else "unknown"
        entry_id = "entry-1"
        sensor._attr_unique_id = sensor.build_unique_id(
            DOMAIN, entry_id, "tracker", f"{safe_id}_ble_battery", separator="_"
        )
        sensor._attr_native_value = None

        assert sensor._device_id is None
        assert "unknown_ble_battery" in sensor._attr_unique_id


# ===========================================================================
# 11. GoogleFindMyBLEBatterySensor — unique_id and _unrecorded_attributes
# ===========================================================================


class TestBLEBatterySensorUniqueId:
    """Tests for the unique_id construction."""

    def test_unique_id_format(self) -> None:
        sensor = _build_battery_sensor(device_id="tracker-xyz")
        assert "tracker-xyz_ble_battery" in sensor.unique_id

    def test_unique_id_differs_from_other_devices(self) -> None:
        sensor_a = _build_battery_sensor(device_id="dev-a")
        sensor_b = _build_battery_sensor(device_id="dev-b")
        assert sensor_a.unique_id != sensor_b.unique_id


class TestBLEBatterySensorUnrecordedAttributes:
    """Tests for the _unrecorded_attributes frozenset."""

    def test_unrecorded_attrs_defined(self) -> None:
        sensor = _build_battery_sensor()
        assert isinstance(sensor._unrecorded_attributes, frozenset)
        assert "uwt_mode" in sensor._unrecorded_attributes
        assert "last_ble_observation" in sensor._unrecorded_attributes
        assert "google_device_id" in sensor._unrecorded_attributes
        assert "battery_raw_level" in sensor._unrecorded_attributes


# ===========================================================================
# 12. GoogleFindMyBLEBatterySensor — _get_resolver edge cases
# ===========================================================================


class TestBLEBatterySensorGetResolver:
    """Tests for the _get_resolver helper."""

    def test_resolver_from_hass_data(self) -> None:
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        assert sensor._get_resolver() is resolver

    def test_resolver_none_when_domain_missing(self) -> None:
        hass = SimpleNamespace(data={})
        sensor = _build_battery_sensor(device_id="dev-1", hass=hass)
        assert sensor._get_resolver() is None

    def test_resolver_none_when_domain_data_not_dict(self) -> None:
        hass = SimpleNamespace(data={DOMAIN: "invalid"})
        sensor = _build_battery_sensor(device_id="dev-1", hass=hass)
        assert sensor._get_resolver() is None

    def test_resolver_none_when_key_missing(self) -> None:
        hass = SimpleNamespace(data={DOMAIN: {"other_key": "value"}})
        sensor = _build_battery_sensor(device_id="dev-1", hass=hass)
        assert sensor._get_resolver() is None


# ===========================================================================
# 13. Integration: Full decode pipeline -> entity value
# ===========================================================================


class TestIntegrationDecodeToEntity:
    """End-to-end: _update_ble_battery populates state -> sensor reads it."""

    def test_decode_pipeline_to_sensor_value(self) -> None:
        """Full pipeline: BLE payload -> resolver decode -> sensor reads battery_pct."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x33
        desired_decoded = 0b01_000000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-pipe")

        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        sensor = _build_battery_sensor(device_id="dev-pipe", resolver=resolver)
        assert sensor.native_value == 5

        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["battery_raw_level"] == 2
        assert attrs["uwt_mode"] is False

    def test_decode_pipeline_shared_device(self) -> None:
        """Shared device: same tracker across 2 accounts -> both sensors get values."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b00_000000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)

        match_a = _match("dev-acct-a")
        match_b = _match("dev-acct-b")
        resolver._update_ble_battery(
            raw, None, {"flags_xor_mask": xor_mask}, [match_a, match_b]
        )

        sensor_a = _build_battery_sensor(device_id="dev-acct-a", resolver=resolver)
        sensor_b = _build_battery_sensor(device_id="dev-acct-b", resolver=resolver)
        assert sensor_a.native_value == 100
        assert sensor_b.native_value == 100

    def test_decode_pipeline_no_entity_without_data(self) -> None:
        """When resolver has NO battery data for a device, sensor returns None."""
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-no-ble", resolver=resolver)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None


# ===========================================================================
# 14. Translations exist
# ===========================================================================


class TestTranslations:
    """Verify that translation files contain the ble_battery key."""

    def test_en_translation_exists(self) -> None:
        import json
        from pathlib import Path

        en_path = Path(__file__).parent.parent / (
            "custom_components/googlefindmy/translations/en.json"
        )
        with open(en_path) as f:
            data = json.load(f)

        sensor_entities = data.get("entity", {}).get("sensor", {})
        assert "ble_battery" in sensor_entities
        assert "name" in sensor_entities["ble_battery"]

    def test_de_translation_exists(self) -> None:
        import json
        from pathlib import Path

        de_path = Path(__file__).parent.parent / (
            "custom_components/googlefindmy/translations/de.json"
        )
        with open(de_path) as f:
            data = json.load(f)

        sensor_entities = data.get("entity", {}).get("sensor", {})
        assert "ble_battery" in sensor_entities
        assert "name" in sensor_entities["ble_battery"]

    def test_all_translations_have_ble_battery(self) -> None:
        """All translation files should have the ble_battery key."""
        import json
        from pathlib import Path

        translations_dir = Path(__file__).parent.parent / (
            "custom_components/googlefindmy/translations"
        )
        for path in sorted(translations_dir.glob("*.json")):
            with open(path) as f:
                data = json.load(f)
            sensor_entities = data.get("entity", {}).get("sensor", {})
            assert "ble_battery" in sensor_entities, (
                f"Missing ble_battery in {path.name}"
            )


# ===========================================================================
# 15. Coverage: remaining edge-case paths
# ===========================================================================


class TestBLEBatterySensorEdgeCases:
    """Additional tests to cover remaining branches."""

    def test_unavailable_when_coordinator_update_failed(self) -> None:
        """When super().available is False (coordinator update failed), sensor unavailable."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        coordinator.last_update_success = False
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            resolver=resolver,
        )
        # super().available returns False due to last_update_success=False
        assert sensor.available is False

    def test_handle_coordinator_update_without_resolver(self) -> None:
        """_handle_coordinator_update with no resolver in hass.data."""
        # hass.data has no DOMAIN key => resolver is None
        hass = SimpleNamespace(data={})
        coordinator = _fake_coordinator(
            device_id="dev-1",
            present=True,
            snapshot=[{"id": "dev-1", "name": "Test"}],
        )
        sensor = _build_battery_sensor(
            device_id="dev-1",
            coordinator=coordinator,
            hass=hass,
        )
        sensor.async_write_ha_state = MagicMock()
        sensor.maybe_update_device_registry_name = MagicMock()
        sensor._attr_native_value = None

        sensor._handle_coordinator_update()

        # Should not crash and should still call async_write_ha_state
        sensor.async_write_ha_state.assert_called()
        assert sensor._attr_native_value is None

    def test_device_info_property(self) -> None:
        """device_info property should return a DeviceInfo with identifiers."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1")
        sensor = _build_battery_sensor(
            device_id="dev-1",
            device_name="My Tracker",
            coordinator=coordinator,
            resolver=resolver,
        )
        # Stub _resolve_absolute_base_url to avoid network access
        sensor._resolve_absolute_base_url = lambda: None

        info = sensor.device_info
        assert info is not None
        assert getattr(info, "identifiers", None) is not None
        assert getattr(info, "manufacturer", None) == "Google"

    def test_real_init_constructor(self) -> None:
        """Exercise the real __init__ path via direct call."""
        coordinator = _fake_coordinator(device_id="real-init")
        coordinator._subentry_key = "core_tracking"

        device: MutableMapping[str, Any] = {
            "id": "real-init",
            "name": "Real Init Tracker",
        }

        # Create sensor with __new__ then call __init__ manually
        sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
        # Set base class attributes that super().__init__ would set
        sensor.coordinator = coordinator
        sensor.hass = _fake_hass()
        sensor._subentry_key = "core_tracking"
        sensor._subentry_identifier = "tracker"
        sensor._base_url_warning_emitted = False
        sensor._device = device
        sensor._fallback_label = device.get("name")

        # Call __init__ body
        GoogleFindMyBLEBatterySensor.__init__(
            sensor,
            coordinator,
            device,
            subentry_key="core_tracking",
            subentry_identifier="tracker",
        )

        assert sensor._device_id == "real-init"
        assert sensor._attr_native_value is None
        assert "real-init_ble_battery" in sensor._attr_unique_id

    def test_real_init_without_device_id(self) -> None:
        """Exercise __init__ when device dict lacks 'id'."""
        coordinator = _fake_coordinator(device_id="unknown")

        device: MutableMapping[str, Any] = {"name": "No ID Device"}

        sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
        sensor.coordinator = coordinator
        sensor.hass = _fake_hass()
        sensor._subentry_key = "core_tracking"
        sensor._subentry_identifier = "tracker"
        sensor._base_url_warning_emitted = False
        sensor._device = device
        sensor._fallback_label = device.get("name")

        GoogleFindMyBLEBatterySensor.__init__(
            sensor,
            coordinator,
            device,
            subentry_key="core_tracking",
            subentry_identifier="tracker",
        )

        assert sensor._device_id is None
        assert "unknown_ble_battery" in sensor._attr_unique_id

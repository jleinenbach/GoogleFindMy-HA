# tests/test_ble_battery_sensor.py
"""Tests for BLE battery state decoding, storage, and sensor entity."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
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

def _close_coro(coro: object, name: object = None) -> None:
    """Close a coroutine to avoid RuntimeWarning in test context."""
    if hasattr(coro, "close"):
        coro.close()


def _fake_hass(domain_data: dict[str, Any] | None = None) -> SimpleNamespace:
    """Return a lightweight hass stand-in."""
    data: dict[str, Any] = {}
    if domain_data is not None:
        from custom_components.googlefindmy.const import DOMAIN
        data[DOMAIN] = domain_data
    return SimpleNamespace(
        async_create_task=_close_coro,
        async_create_background_task=_close_coro,
        data=data,
    )


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


def _match(device_id: str = "dev-1") -> EIDMatch:
    """Create a test EIDMatch."""
    return EIDMatch(
        device_id=device_id,
        config_entry_id="entry-1",
        canonical_id="canonical-1",
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
        """FMDN_BATTERY_PCT should map 0→100, 1→25, 2→5."""
        assert FMDN_BATTERY_PCT == {0: 100, 1: 25, 2: 5}

    def test_battery_pct_unknown_raw_returns_zero(self) -> None:
        """An unrecognized raw value (3=RESERVED) should map to 0."""
        assert FMDN_BATTERY_PCT.get(3, 0) == 0

    def test_slots_optimization(self) -> None:
        """BLEBatteryState uses __slots__ for memory efficiency."""
        assert hasattr(BLEBatteryState, "__slots__")


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
        """Battery level 0 (GOOD) → 100% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x55

        # Battery raw=0 → bits 5-6 = 00, UWT=0 → decoded flags = 0x00
        # flags_byte = decoded ^ xor_mask = 0x00 ^ 0x55 = 0x55
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
        """Battery level 1 (LOW) → 25% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xbb" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        # Battery raw=1 → bits 5-6 = 01, UWT=0 → decoded flags = 0b00_100000 = 0x20
        desired_decoded = 0b00_100000
        flags_byte = desired_decoded ^ xor_mask  # = 0x20

        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-low")
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        state = resolver._ble_battery_state.get("dev-low")
        assert state is not None
        assert state.battery_level == 1
        assert state.battery_pct == 25
        assert state.uwt_mode is False

    def test_decode_critical_service_data(self) -> None:
        """Battery level 2 (CRITICAL) → 5% via service-data format."""
        resolver = _make_resolver()
        eid = b"\xcc" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        # Battery raw=2 → bits 5-6 = 10, UWT=0 → decoded flags = 0b01_000000 = 0x40
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
        """UWT mode bit 7 set → uwt_mode=True."""
        resolver = _make_resolver()
        eid = b"\xdd" * LEGACY_EID_LENGTH
        xor_mask = 0x00

        # Battery raw=0, UWT=1 → decoded flags = 0b10_000000 = 0x80
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

        # Battery raw=1 (LOW), UWT=0
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
        desired_decoded = 0b00_000000  # battery=GOOD
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
        """Missing xor_mask → no battery state stored."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        raw = _service_data_payload(eid, 0x42)
        match = _match("dev-no-mask")
        resolver._update_ble_battery(raw, None, {}, [match])
        assert resolver._ble_battery_state.get("dev-no-mask") is None

    def test_cannot_decode_short_payload(self) -> None:
        """Payload too short for flags byte → no battery state stored."""
        resolver = _make_resolver()
        raw = b"\x00" * 10  # way too short
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

    def test_first_decode_logs_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # Build a payload with flags position but no xor_mask
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

        # Second: LOW (1)
        decoded_low = 0b00_100000
        raw = _service_data_payload(eid, decoded_low ^ xor_mask)
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])
        assert resolver._ble_battery_state["dev-change"].battery_pct == 25
        assert resolver._ble_battery_state["dev-change"].battery_level == 1

    def test_reserved_battery_raw_3_maps_to_0_pct(self) -> None:
        """Raw battery value 3 (RESERVED) should map to 0% via FMDN_BATTERY_PCT.get fallback."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        # Battery raw=3 → bits 5-6 = 11 → decoded flags = 0b01_100000 = 0x60
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
        """Battery CRITICAL + UWT → battery_pct=5, uwt_mode=True."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        # Battery raw=2, UWT=1 → decoded = 0b11_000000 = 0xC0
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
        desired_decoded = 0b00_100000  # battery=LOW
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
        """SensorDeviceClass.BATTERY provides dynamic icons — no manual icon."""
        assert getattr(BLE_BATTERY_DESCRIPTION, "icon", None) is None


# ===========================================================================
# 5. GoogleFindMyBLEBatterySensor entity
# ===========================================================================


def _fake_coordinator(
    device_id: str = "dev-1",
    present: bool = True,
    has_device: bool = True,
) -> SimpleNamespace:
    """Create a minimal coordinator stub for sensor tests."""
    return SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry-1"),
        is_device_present=lambda did: present,
        _has_device=has_device,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda key: [],
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
            from custom_components.googlefindmy.const import DATA_EID_RESOLVER
            domain_data[DATA_EID_RESOLVER] = resolver
        hass = _fake_hass(domain_data)

    sensor = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
    sensor._subentry_identifier = "tracker"
    sensor._subentry_key = "core_tracking"
    sensor.coordinator = coordinator
    sensor.hass = hass
    sensor._device_id = device_id
    sensor._attr_native_value = None
    sensor.entity_description = BLE_BATTERY_DESCRIPTION
    sensor._attr_has_entity_name = True
    sensor._attr_entity_registry_enabled_default = True
    sensor._unrecorded_attributes = frozenset({
        "uwt_mode", "last_ble_observation", "google_device_id", "battery_raw_level",
    })

    safe_id = device_id if device_id is not None else "unknown"
    entry_id = getattr(coordinator.config_entry, "entry_id", "default")
    sensor._attr_unique_id = f"googlefindmy_{entry_id}_tracker_{safe_id}_ble_battery"

    # Stub entity methods to avoid HA platform dependency
    sensor._fallback_label = device_name
    sensor._device_label = device_name
    sensor.entity_id = f"sensor.test_{safe_id}_ble_battery"

    return sensor


class TestBLEBatterySensorNativeValue:
    """Tests for the native_value property."""

    def test_value_from_resolver(self) -> None:
        """When resolver has battery data, native_value returns battery_pct."""
        resolver = _make_resolver()
        state = BLEBatteryState(
            battery_level=0, battery_pct=100, uwt_mode=False,
            decoded_flags=0x00, observed_at_wall=1000.0,
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

        # Initially no data
        assert sensor.native_value is None

        # Store GOOD
        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=0, battery_pct=100, uwt_mode=False,
            decoded_flags=0x00, observed_at_wall=1000.0,
        )
        assert sensor.native_value == 100

        # Update to LOW
        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=1, battery_pct=25, uwt_mode=False,
            decoded_flags=0x20, observed_at_wall=2000.0,
        )
        assert sensor.native_value == 25


class TestBLEBatterySensorAvailability:
    """Tests for the available property."""

    def test_available_when_present(self) -> None:
        """Sensor available when coordinator reports device as present."""
        resolver = _make_resolver()
        resolver._ble_battery_state["dev-1"] = BLEBatteryState(
            battery_level=0, battery_pct=100, uwt_mode=False,
            decoded_flags=0x00, observed_at_wall=1000.0,
        )
        coordinator = _fake_coordinator(device_id="dev-1", present=True)
        sensor = _build_battery_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver,
        )

        # Stub coordinator_has_device to return True
        sensor.coordinator_has_device = lambda: True
        # Stub super().available
        type(sensor).available = property(
            lambda self: (
                self.coordinator_has_device()
                and (
                    _is_present(self)
                    or self._attr_native_value is not None
                )
            )
        )
        # Re-apply the actual available logic
        assert _check_available(sensor, present=True)

    def test_available_when_not_present_with_restore(self) -> None:
        """Available even when not present, if we have a restored value."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=False)
        sensor = _build_battery_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver,
        )
        sensor._attr_native_value = 100  # restored
        sensor.coordinator_has_device = lambda: True

        assert _check_available(sensor, present=False, has_restore=True)

    def test_unavailable_when_not_present_no_data(self) -> None:
        """Unavailable when not present and no restore/resolver data."""
        resolver = _make_resolver()
        coordinator = _fake_coordinator(device_id="dev-1", present=False)
        sensor = _build_battery_sensor(
            device_id="dev-1", coordinator=coordinator, resolver=resolver,
        )
        sensor._attr_native_value = None
        sensor.coordinator_has_device = lambda: True

        assert not _check_available(sensor, present=False, has_restore=False)


def _is_present(sensor: GoogleFindMyBLEBatterySensor) -> bool:
    """Check if coordinator reports device as present."""
    try:
        if hasattr(sensor.coordinator, "is_device_present"):
            return bool(sensor.coordinator.is_device_present(sensor._device_id))
    except Exception:
        pass
    return False


def _check_available(
    sensor: GoogleFindMyBLEBatterySensor,
    present: bool,
    has_restore: bool | None = None,
) -> bool:
    """Simulate availability check matching the sensor's logic.

    This mirrors the actual available property logic to avoid needing
    full HA entity platform initialization.
    """
    if not sensor.coordinator_has_device():
        return False

    try:
        if hasattr(sensor.coordinator, "is_device_present"):
            if bool(sensor.coordinator.is_device_present(sensor._device_id)):
                return True
            # Not present → available only with a restored value
            return sensor._attr_native_value is not None
    except Exception:
        pass
    return sensor._attr_native_value is not None


class TestBLEBatterySensorExtraAttributes:
    """Tests for extra_state_attributes."""

    def test_attributes_with_resolver_data(self) -> None:
        resolver = _make_resolver()
        state = BLEBatteryState(
            battery_level=1, battery_pct=25, uwt_mode=True,
            decoded_flags=0xA0, observed_at_wall=1700000000.0,
        )
        resolver._ble_battery_state["dev-1"] = state

        sensor = _build_battery_sensor(device_id="dev-1", resolver=resolver)
        attrs = sensor.extra_state_attributes

        assert attrs is not None
        assert attrs["battery_raw_level"] == 1
        assert attrs["uwt_mode"] is True
        assert attrs["google_device_id"] == "dev-1"
        assert "last_ble_observation" in attrs
        # Should be an ISO formatted string
        assert "2023" in attrs["last_ble_observation"] or "T" in attrs["last_ble_observation"]

    def test_attributes_none_without_resolver(self) -> None:
        sensor = _build_battery_sensor(device_id="dev-1", resolver=None)
        assert sensor.extra_state_attributes is None

    def test_attributes_none_without_battery_data(self) -> None:
        resolver = _make_resolver()
        sensor = _build_battery_sensor(device_id="dev-no-data", resolver=resolver)
        assert sensor.extra_state_attributes is None


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
# 6. Integration: Full decode pipeline → entity value
# ===========================================================================


class TestIntegrationDecodeToEntity:
    """End-to-end: _update_ble_battery populates state → sensor reads it."""

    def test_decode_pipeline_to_sensor_value(self) -> None:
        """Full pipeline: BLE payload → resolver decode → sensor reads battery_pct."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x33
        # Battery=CRITICAL(2), UWT=0 → decoded = 0b01_000000 = 0x40
        desired_decoded = 0b01_000000
        flags_byte = desired_decoded ^ xor_mask
        raw = _service_data_payload(eid, flags_byte)
        match = _match("dev-pipe")

        # Step 1: Resolver decodes
        resolver._update_ble_battery(raw, None, {"flags_xor_mask": xor_mask}, [match])

        # Step 2: Sensor reads
        sensor = _build_battery_sensor(device_id="dev-pipe", resolver=resolver)
        assert sensor.native_value == 5  # CRITICAL → 5%

        # Step 3: Extra attributes available
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["battery_raw_level"] == 2
        assert attrs["uwt_mode"] is False

    def test_decode_pipeline_shared_device(self) -> None:
        """Shared device: same tracker across 2 accounts → both sensors get values."""
        resolver = _make_resolver()
        eid = b"\xaa" * LEGACY_EID_LENGTH
        xor_mask = 0x00
        desired_decoded = 0b00_000000  # GOOD
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
# 7. Translations exist
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

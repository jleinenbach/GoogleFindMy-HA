# tests/test_ble_battery_freshness_static_eid.py
"""Freshness of the BLE battery value for a static counter-0 EID.

A DIY microcontroller tracker advertises the same payload for its whole life:
frame, counter-0 EID and hashed-flags byte never change while the battery
level stays the same. The battery sensor shows a value only while the last
sighting is at most 7 days old (``BLE_BATTERY_RESTORE_MAX_AGE_S``), so the
value survives only if every delivery of the unchanged payload refreshes the
sighting time.

These tests pin that contract on our side:

* Test 1: the resolver re-stores an unchanged state on every delivery made
  the way Bermuda calls it (``resolve_eid_all`` without ``observed_at`` and
  without ``ble_address``), so ``observed_at_wall`` moves with the clock.
* Test 2: fed hourly for 8 days, the sensor keeps a value throughout, and the
  value it caches for restore carries the latest sighting time.
* Test 3: once deliveries stop, the value expires 7 days plus 1 second after
  the last sighting.

Whether a feeder hands unchanged advertisements on at all is outside this
module; see ``docs/BLE_BATTERY_SENSOR.md``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.googlefindmy import eid_resolver as resolver_module
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EidVariant,
    compute_flags_xor_mask,
    generate_eid_variant,
)
from custom_components.googlefindmy.sensor import (
    BLE_BATTERY_DESCRIPTION,
    GoogleFindMyBLEBatterySensor,
)
from custom_components.googlefindmy.SpotApi.CreateBleDevice.config import (
    mcu_fast_pair_model_id,
)
from tests.helpers.config_entries_stub import make_config_entry

# Synthetic key, no relation to any real tracker.
EIK = hashlib.sha256(b"googlefindmy-test-freshness-static-eid").digest()
NOW = 1_780_000_000
PAIR_DATE = NOW - 30 * 24 * 60 * 60  # Registered a month ago.
LEGACY = EidVariant.LEGACY_SECP160R1_X20_BE
CANONICAL_ID = "can-fresh"
HOUR_S = 3600
DAY_S = 24 * HOUR_S
# The sensor's age limit, as a literal: the tests do not import the constant,
# so a change of the limit shows up here instead of passing silently.
MAX_AGE_S = 7 * DAY_S

# FMDN hashed flags: battery level in bits 1-2, UWT mode in bit 0.
BATTERY_LOW = 2
DECODED_FLAGS = (BATTERY_LOW << 1) | 0x01
EXPECTED_PCT = resolver_module.FMDN_BATTERY_PCT[BATTERY_LOW]


class _Clock:
    """Wall and monotonic clock that only move when the test says so."""

    def __init__(self) -> None:
        self.wall = float(NOW)
        self.monotonic = 50_000.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.monotonic += seconds

    @contextmanager
    def applied(self) -> Iterator[None]:
        """Patch both clocks of the ``time`` module the code under test reads."""
        with (
            patch("time.time", return_value=self.wall),
            patch("time.monotonic", return_value=self.monotonic),
        ):
            yield


def _identity() -> DeviceIdentity:
    return DeviceIdentity(
        registry_id="reg-fresh",
        canonical_id=CANONICAL_ID,
        identity_key=EIK,
        config_entry_id="entry-1",
        pair_date=PAIR_DATE,
        device_type=1,
        fast_pair_model_id=mcu_fast_pair_model_id,
    )


def _new_resolver() -> GoogleFindMyEIDResolver:
    """Resolver with a static-EID lookup; its mode channel reads ``hass.data``."""
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._ensure_cache_defaults()
    resolver.hass = SimpleNamespace(data={})
    items = resolver._collect_work_items([_identity()], now_unix=NOW)
    lookup, metadata, _invalid = resolver._build_lookup_sync(
        items, NOW, resolver._build_rotation_params()
    )
    resolver._lookup = lookup
    resolver._lookup_metadata = metadata
    return resolver


def _advertisement() -> bytes:
    """FEAA service data as the firmware sends it, unchanged on every send."""
    curve_len, curve_order, derivation = resolver_module._VARIANT_CURVE_PARAMS[LEGACY]
    mask = compute_flags_xor_mask(
        EIK,
        0,
        curve_byte_len=curve_len,
        curve_order=curve_order,
        derivation=derivation,
    )
    flags_byte = DECODED_FLAGS ^ mask
    # A 0x00 byte is the stock-firmware case, which stores no state at all;
    # the freshness tests would then run empty.
    assert flags_byte != 0x00
    return bytes([0x41]) + generate_eid_variant(EIK, 0, LEGACY) + bytes([flags_byte])


def _deliver(resolver: GoogleFindMyEIDResolver, clock: _Clock) -> None:
    """Deliver the advertisement the way Bermuda does: no time, no address."""
    with clock.applied():
        matches = resolver.resolve_eid_all(_advertisement())
    assert [match.device_id for match in matches] == ["reg-fresh"]


def _build_sensor(resolver: GoogleFindMyEIDResolver) -> GoogleFindMyBLEBatterySensor:
    """Battery sensor bypassing platform init, reading the given resolver."""
    coordinator = SimpleNamespace(
        config_entry=make_config_entry(entry_id="entry-1"),
        is_device_present=lambda _did: True,
        is_device_visible_in_subentry=lambda _key, _did: True,
        async_update_listeners=lambda: None,
        get_subentry_snapshot=lambda _key: [{"id": CANONICAL_ID, "name": "Tag"}],
        last_update_success=True,
    )
    domain_data: dict[str, Any] = {DATA_EID_RESOLVER: resolver}
    entity = GoogleFindMyBLEBatterySensor.__new__(GoogleFindMyBLEBatterySensor)
    entity._subentry_identifier = "tracker"
    entity._subentry_key = "core_tracking"
    entity.coordinator = coordinator
    entity.hass = SimpleNamespace(data={DOMAIN: domain_data})
    entity._device_id = CANONICAL_ID
    entity._device = {"id": CANONICAL_ID, "name": "Tag"}
    entity._attr_native_value = None
    entity._last_observation_wall = None
    entity.entity_description = BLE_BATTERY_DESCRIPTION
    entity._fallback_label = "Tag"
    entity._attr_unique_id = f"googlefindmy_entry-1_tracker_{CANONICAL_ID}_ble_battery"
    entity.entity_id = f"sensor.test_{CANONICAL_ID}_ble_battery"
    entity.async_write_ha_state = MagicMock()
    return entity


def _value(entity: GoogleFindMyBLEBatterySensor, clock: _Clock) -> int | None:
    with clock.applied():
        return entity.native_value


def _coordinator_tick(entity: GoogleFindMyBLEBatterySensor, clock: _Clock) -> None:
    with clock.applied():
        entity._handle_coordinator_update()


def test_unchanged_advertisement_refreshes_sighting_time() -> None:
    """Test 1: every delivery of the same payload re-stores the sighting."""
    resolver = _new_resolver()
    clock = _Clock()

    seen: list[float] = []
    for _ in range(3):
        _deliver(resolver, clock)
        state = resolver.get_ble_battery_state(CANONICAL_ID)
        assert state is not None
        assert state.battery_pct == EXPECTED_PCT
        seen.append(state.observed_at_wall)
        clock.advance(HOUR_S)

    assert seen == [float(NOW), float(NOW + HOUR_S), float(NOW + 2 * HOUR_S)]


def test_sensor_keeps_value_while_fed_hourly_for_eight_days() -> None:
    """Test 2: hourly deliveries keep the value past the 7-day limit."""
    resolver = _new_resolver()
    entity = _build_sensor(resolver)
    clock = _Clock()

    _deliver(resolver, clock)
    _coordinator_tick(entity, clock)
    for hour in range(1, 8 * 24 + 1):
        clock.advance(HOUR_S)
        # Just before the next delivery the last one is an hour old.
        assert _value(entity, clock) == EXPECTED_PCT, f"gap before hour {hour}"
        _deliver(resolver, clock)
        _coordinator_tick(entity, clock)
        assert _value(entity, clock) == EXPECTED_PCT, f"gap after hour {hour}"

    assert clock.wall - NOW == 8 * DAY_S
    # The value cached for restore carries the latest sighting, not the first:
    # with the resolver gone it must still count as fresh.
    assert entity._last_observation_wall == clock.wall
    entity.hass.data[DOMAIN].pop(DATA_EID_RESOLVER)
    assert _value(entity, clock) == EXPECTED_PCT


def test_sensor_value_expires_seven_days_after_last_delivery() -> None:
    """Test 3: without deliveries the 7-day limit still applies."""
    resolver = _new_resolver()
    entity = _build_sensor(resolver)
    clock = _Clock()

    _deliver(resolver, clock)
    _coordinator_tick(entity, clock)
    last_sighting = clock.wall

    clock.advance(MAX_AGE_S)
    assert _value(entity, clock) == EXPECTED_PCT
    clock.advance(1)
    assert clock.wall - last_sighting == MAX_AGE_S + 1
    assert _value(entity, clock) is None

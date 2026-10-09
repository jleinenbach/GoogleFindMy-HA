# tests/test_fmdn_finder_bermuda_listener.py
"""Tests for FMDN Finder bermuda_listener module.

Tests the Bermuda device_tracker listener that triggers FMDN location uploads
when area changes are detected on Bermuda tracker entities.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
    ATTR_AREA,
    BERMUDA_TRACKER_SUFFIX,
    _async_upload_semantic_location,
    async_setup_bermuda_listener,
    async_unload_bermuda_listener,
)


@pytest.mark.asyncio
async def test_setup_bermuda_listener(hass_mock: MagicMock) -> None:
    """Test Bermuda listener setup."""
    await async_setup_bermuda_listener(hass_mock)

    # Verify event listener was registered
    assert hass_mock.bus.async_listen.called


@pytest.mark.asyncio
async def test_unload_bermuda_listener(hass_mock: MagicMock) -> None:
    """Test Bermuda listener unload."""
    # Setup first
    await async_setup_bermuda_listener(hass_mock)

    # Now unload
    await async_unload_bermuda_listener(hass_mock)

    # Should call unsubscribe callback
    # (implementation depends on actual storage structure)


@pytest.mark.asyncio
async def test_bermuda_state_change_with_area_change(hass_mock: MagicMock) -> None:
    """Test that area change on Bermuda tracker triggers upload task."""
    from homeassistant.const import EVENT_STATE_CHANGED

    await async_setup_bermuda_listener(hass_mock)

    # Get the registered callback
    callback_calls = hass_mock.bus.async_listen.call_args_list
    assert len(callback_calls) > 0

    # Extract the callback function
    event_type, callback = callback_calls[0][0]
    assert event_type == EVENT_STATE_CHANGED

    # Simulate Bermuda tracker state change with area change
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "device_tracker.moto_tag_koffer_grun_bermuda_tracker_2",
        "old_state": MagicMock(
            attributes={
                ATTR_AREA: "Wohnzimmer",
                "scanner": "Scanner1",
            }
        ),
        "new_state": MagicMock(
            attributes={
                ATTR_AREA: "Windfang",
                "scanner": "Scanner2",
            }
        ),
    }

    # Trigger callback
    callback(mock_event)

    # Verify async_create_task was called (upload triggered)
    assert hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_ignores_non_bermuda_trackers(
    hass_mock: MagicMock,
) -> None:
    """Test that non-Bermuda tracker entities are ignored."""

    await async_setup_bermuda_listener(hass_mock)

    # Get the registered callback
    callback_calls = hass_mock.bus.async_listen.call_args_list
    callback = callback_calls[0][0][1]

    # Simulate non-Bermuda device_tracker state change
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "device_tracker.regular_device",  # No '_bermuda_tracker' suffix
        "new_state": MagicMock(
            attributes={
                ATTR_AREA: "Kitchen",
            }
        ),
        "old_state": MagicMock(
            attributes={
                ATTR_AREA: "Living Room",
            }
        ),
    }

    # Trigger callback
    callback(mock_event)

    # Should not create upload task
    assert not hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_ignores_unchanged_area(
    hass_mock: MagicMock,
) -> None:
    """Test that unchanged area does not trigger upload."""

    await async_setup_bermuda_listener(hass_mock)

    callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

    # Simulate state change with same area
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "device_tracker.pixel_buds_bermuda_tracker",
        "old_state": MagicMock(
            attributes={
                ATTR_AREA: "Office",
            }
        ),
        "new_state": MagicMock(
            attributes={
                ATTR_AREA: "Office",  # Same area
            }
        ),
    }

    callback(mock_event)

    # Should not create task for unchanged area
    assert not hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_ignores_missing_area(hass_mock: MagicMock) -> None:
    """Test that missing area attribute does not trigger upload."""

    await async_setup_bermuda_listener(hass_mock)

    callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

    # Simulate state change without area attribute
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "device_tracker.test_bermuda_tracker",
        "old_state": None,
        "new_state": MagicMock(
            attributes={
                "scanner": "Scanner1",
                # No area attribute
            }
        ),
    }

    callback(mock_event)

    # Should not create task without area
    assert not hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_handles_initial_area(hass_mock: MagicMock) -> None:
    """Test that initial area (from None) triggers upload."""

    await async_setup_bermuda_listener(hass_mock)

    callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

    # Simulate initial area detection (old_state is None or has no area)
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "device_tracker.moto_tag_bermuda_tracker",
        "old_state": None,  # First detection
        "new_state": MagicMock(
            attributes={
                ATTR_AREA: "Garage",
            }
        ),
    }

    callback(mock_event)

    # Should create upload task for initial detection
    assert hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_tracker_suffix_constant() -> None:
    """Test that Bermuda tracker suffix constant is correct."""
    assert BERMUDA_TRACKER_SUFFIX == "_bermuda_tracker"


@pytest.mark.asyncio
async def test_bermuda_state_change_ignores_sensors(hass_mock: MagicMock) -> None:
    """Test that sensor entities are ignored (only device_tracker)."""

    await async_setup_bermuda_listener(hass_mock)

    callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

    # Simulate sensor state change (not device_tracker)
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "sensor.bermuda_tracker_rssi",  # sensor, not device_tracker
        "old_state": MagicMock(attributes={}),
        "new_state": MagicMock(
            attributes={
                ATTR_AREA: "Living Room",
            }
        ),
    }

    callback(mock_event)

    # Should not create task for sensor entities
    assert not hass_mock.async_create_task.called


@pytest.fixture
def hass_mock() -> MagicMock:
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.data = {"googlefindmy": {}}
    hass.bus = MagicMock()
    hass.bus.async_listen = MagicMock(
        return_value=MagicMock()
    )  # Return unsubscribe callable

    # Mock async_create_task to close coroutines to avoid RuntimeWarning
    def _mock_create_task(coro, **kwargs):
        """Close coroutine to prevent 'never awaited' warning."""
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    hass.async_create_task = MagicMock(side_effect=_mock_create_task)
    return hass


# =============================================================================
# Tests for Congealment-based Device Matching
# =============================================================================
# CRITICAL: These tests verify that we correctly find GoogleFindMy devices
# via Bermuda's congealment mechanism (shared HA device).
#
# The matching MUST use:
#   1. Entity registry lookup by HA device_id
#   2. Filter entities by platform="googlefindmy" and domain="device_tracker"
#
# The matching MUST NOT use:
#   - Entity name matching (users can rename!)
#   - Device identifier matching (Bermuda doesn't add googlefindmy identifiers)
#   - MAC address matching (BLE MACs rotate)
# =============================================================================


@pytest.mark.asyncio
async def test_find_googlefindmy_device_via_congealment() -> None:
    """Test finding GoogleFindMy device when it shares HA device with Bermuda.

    This is the CRITICAL test for congealment. Bermuda attaches its entity
    to the SAME HA device as GoogleFindMy. We must find the GoogleFindMy
    entity by looking up ALL entities for the shared HA device.
    """
    from unittest.mock import patch

    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _async_find_googlefindmy_device,
    )

    # Setup mock hass with domain data
    # RuntimeData is a dataclass with .coordinator attribute
    hass = MagicMock()
    mock_coordinator = MagicMock()
    mock_runtime_data = MagicMock()
    mock_runtime_data.coordinator = mock_coordinator
    hass.data = {
        "googlefindmy": {
            "entries": {
                "test_config_entry_id": mock_runtime_data,
            }
        }
    }

    # Create mock entity registry entries for SAME HA device
    ha_device_id = "11b2838b4bb2ba2eb5f4f4b2c742cbf9"

    # GoogleFindMy entity (platform=googlefindmy)
    gfm_entity = MagicMock()
    gfm_entity.entity_id = "device_tracker.moto_tag_jens_schlusselbund"
    gfm_entity.domain = "device_tracker"
    gfm_entity.platform = "googlefindmy"
    gfm_entity.device_id = ha_device_id
    gfm_entity.config_entry_id = "test_config_entry_id"
    gfm_entity.unique_id = "test_config_entry_id:google_device_12345"

    # Bermuda entity (platform=bermuda) - same HA device!
    bermuda_entity = MagicMock()
    bermuda_entity.entity_id = (
        "device_tracker.moto_tag_jens_schlusselbund_bermuda_tracker_2"
    )
    bermuda_entity.domain = "device_tracker"
    bermuda_entity.platform = "bermuda"
    bermuda_entity.device_id = ha_device_id  # SAME device!

    # Mock entity registry
    mock_ent_reg = MagicMock()

    with (
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[gfm_entity, bermuda_entity],  # Both entities on same device
        ),
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    # Should find the GoogleFindMy entity
    assert result is not None
    assert result["device_id"] == "google_device_12345"
    assert result["config_entry_id"] == "test_config_entry_id"
    assert result["coordinator"] == mock_coordinator


@pytest.mark.asyncio
async def test_find_googlefindmy_device_no_gfm_entity_on_device() -> None:
    """Test that we return None when no GoogleFindMy entity exists on the device.

    This can happen for regular BLE devices tracked by Bermuda that are
    NOT GoogleFindMy/FMDN devices.
    """
    from unittest.mock import patch

    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _async_find_googlefindmy_device,
    )

    hass = MagicMock()
    mock_runtime_data = MagicMock()
    mock_runtime_data.coordinator = MagicMock()
    hass.data = {"googlefindmy": {"entries": {"config_entry": mock_runtime_data}}}

    ha_device_id = "some_other_device_id"

    # Only Bermuda entity, no GoogleFindMy entity
    bermuda_only_entity = MagicMock()
    bermuda_only_entity.entity_id = "device_tracker.tile_wallet_bermuda_tracker"
    bermuda_only_entity.domain = "device_tracker"
    bermuda_only_entity.platform = "bermuda"
    bermuda_only_entity.device_id = ha_device_id

    mock_ent_reg = MagicMock()

    with (
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[bermuda_only_entity],  # No GoogleFindMy entity
        ),
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    # Should return None - no GoogleFindMy device
    assert result is None


@pytest.mark.asyncio
async def test_find_googlefindmy_device_extracts_device_id_from_unique_id() -> None:
    """Test that Google device ID is correctly extracted from unique_id.

    unique_id format: "{config_entry_id}:{google_device_id}"
    We need to extract just the google_device_id part.
    """
    from unittest.mock import patch

    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _async_find_googlefindmy_device,
    )

    hass = MagicMock()
    mock_runtime_data = MagicMock()
    mock_runtime_data.coordinator = MagicMock()
    hass.data = {
        "googlefindmy": {
            "entries": {
                "entry_abc123": mock_runtime_data,
            }
        }
    }

    ha_device_id = "shared_device_id"

    gfm_entity = MagicMock()
    gfm_entity.entity_id = "device_tracker.pixel_buds"
    gfm_entity.domain = "device_tracker"
    gfm_entity.platform = "googlefindmy"
    gfm_entity.device_id = ha_device_id
    gfm_entity.config_entry_id = "entry_abc123"
    # unique_id with colon separator
    gfm_entity.unique_id = "entry_abc123:actual_google_device_id_xyz"

    mock_ent_reg = MagicMock()

    with (
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[gfm_entity],
        ),
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    assert result is not None
    # Should extract the part after the colon
    assert result["device_id"] == "actual_google_device_id_xyz"


@pytest.mark.asyncio
async def test_find_googlefindmy_device_ignores_non_device_tracker_entities() -> None:
    """Test that we only match device_tracker entities, not sensors etc.

    GoogleFindMy creates multiple entity types. We specifically need
    the device_tracker for location uploads.
    """
    from unittest.mock import patch

    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _async_find_googlefindmy_device,
    )

    hass = MagicMock()
    mock_runtime_data = MagicMock()
    mock_runtime_data.coordinator = MagicMock()
    hass.data = {
        "googlefindmy": {
            "entries": {
                "config_entry": mock_runtime_data,
            }
        }
    }

    ha_device_id = "device_with_sensors"

    # GoogleFindMy sensor (NOT device_tracker)
    gfm_sensor = MagicMock()
    gfm_sensor.entity_id = "sensor.moto_tag_battery"
    gfm_sensor.domain = "sensor"  # NOT device_tracker
    gfm_sensor.platform = "googlefindmy"
    gfm_sensor.device_id = ha_device_id

    # GoogleFindMy device_tracker (this is what we want)
    gfm_tracker = MagicMock()
    gfm_tracker.entity_id = "device_tracker.moto_tag"
    gfm_tracker.domain = "device_tracker"
    gfm_tracker.platform = "googlefindmy"
    gfm_tracker.device_id = ha_device_id
    gfm_tracker.config_entry_id = "config_entry"
    gfm_tracker.unique_id = "target_device_id"

    mock_ent_reg = MagicMock()

    with (
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_ent_reg,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[gfm_sensor, gfm_tracker],  # Sensor comes first
        ),
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    assert result is not None
    # Should find the device_tracker, not the sensor
    assert result["device_id"] == "target_device_id"


@pytest.mark.asyncio
async def test_semantic_upload_debug_log_masks_scanner_name(
    hass_mock: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """AGENTS.md section 5, class (c): the Bermuda scanner name is logged
    masked, while the uploader still receives it in full.

    Bermuda's `scanner` attribute is the scanner device's name and falls back
    to `bermuda_<slug of the MAC>` (`bermuda_device.make_name`), so the value
    is graded as a hardware address. Only the uploader boundary is mocked; the
    DEBUG line under test runs for real.
    """
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.googlefindmy.fmdn_finder.bermuda_listener",
    )
    upload_mock = AsyncMock()
    with patch(
        "custom_components.googlefindmy.fmdn_finder.location_uploader.async_process_fmdn_beacon_detection",
        upload_mock,
    ):
        await _async_upload_semantic_location(
            hass_mock,
            eid=b"\xab" * 20,
            area="Kitchen",
            config_entry_id="entry_1",
            scanner="AA:BB:CC:DD:EE:FF",
            google_device_id="google_dev_1",
        )

    assert "scanner=...E:FF" in caplog.text
    assert "AA:BB:CC:DD:EE:FF" not in caplog.text
    upload_mock.assert_awaited_once()
    assert upload_mock.await_args.kwargs["scanner_address"] == "AA:BB:CC:DD:EE:FF"


@pytest.mark.asyncio
async def test_find_googlefindmy_device_info_keeps_entity_id_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AGENTS.md section 5 (b): the per-upload INFO names the Google device id
    and the HA device id (both class b); the entity_id moves to DEBUG."""
    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _async_find_googlefindmy_device,
    )

    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.googlefindmy.fmdn_finder.bermuda_listener",
    )
    hass = MagicMock()
    runtime_data = MagicMock()
    hass.data = {"googlefindmy": {"entries": {"entry_1": runtime_data}}}
    ha_device_id = "11b2838b4bb2ba2eb5f4f4b2c742cbf9"
    entity_id = "device_tracker.moto_tag_jens_schlusselbund"
    gfm_entity = MagicMock()
    gfm_entity.entity_id = entity_id
    gfm_entity.domain = "device_tracker"
    gfm_entity.platform = "googlefindmy"
    gfm_entity.device_id = ha_device_id
    gfm_entity.config_entry_id = "entry_1"
    gfm_entity.unique_id = "entry_1:google_device_12345"

    with (
        patch(
            "homeassistant.helpers.entity_registry.async_get", return_value=MagicMock()
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[gfm_entity],
        ),
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    assert result is not None
    above = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert any("google_device_12345" in r.getMessage() for r in above)
    assert all(entity_id not in r.getMessage() for r in above)
    assert any(
        r.levelno == logging.DEBUG and entity_id in r.getMessage()
        for r in caplog.records
    )


# --- The finder encrypts for the locked variant on its own curve ----------

_LOCK_EIK = bytes(range(32))
_LOCK_NOW = 1_700_000_000
# Seconds since pair_date. With pair_date 0, as here, that is plain Unix time;
# the resolver rejects such an anchor, so these tests compare against
# generate_eid_variant() directly. The tests further down use a real anchor
# and compare against the resolver's lookup table.
_LOCK_COUNTER = _LOCK_NOW

# Variant whose EID the finder must produce for a lock, written out by hand:
# full-length variants map to themselves, truncated P-256 variants to the
# 32-byte variant with the same scalar derivation (encrypt() needs the full
# x-coordinate).
_UPLOAD_VARIANT_FOR_LOCK = {
    "legacy_secp160r1_x20_be": "legacy_secp160r1_x20_be",
    "modern_p256_x32_be": "modern_p256_x32_be",
    "modern_p256_x20_trunc_be": "modern_p256_x32_be",
    "modern_p256_x32_le_scalar": "modern_p256_x32_le_scalar",
    "modern_p256_x20_trunc_le": "modern_p256_x32_le_scalar",
    "spec_p256_x32_be": "spec_p256_x32_be",
    "spec_p256_x20_trunc_be": "spec_p256_x32_be",
}


def _hass_with_lock(variant: str | None) -> MagicMock:
    from custom_components.googlefindmy.const import DOMAIN
    from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

    resolver = MagicMock(spec=GoogleFindMyEIDResolver)
    resolver.locked_variant_value.side_effect = lambda registry_id: (
        variant if registry_id == "reg-1" else None
    )
    # No match and no lock projection: these tests pin the pair_date fallback.
    resolver.encryption_counter.return_value = None
    hass = MagicMock()
    hass.data = {DOMAIN: {"eid_resolver": resolver}}
    return hass


def _coordinator_with_identity() -> MagicMock:
    identity = MagicMock(
        canonical_id="entry:dev-1",
        identity_key=_LOCK_EIK,
        pair_date=0,
        registry_id="reg-1",
    )
    coordinator = MagicMock()
    coordinator.get_active_device_identities.return_value = [identity]
    return coordinator


async def _device_eid(variant: str | None) -> bytes | None:
    from custom_components.googlefindmy.fmdn_finder import bermuda_listener

    with patch.object(bermuda_listener.time, "time", return_value=_LOCK_NOW):
        return await bermuda_listener._async_get_device_eid(
            _hass_with_lock(variant), _coordinator_with_identity(), "dev-1"
        )


def test_upload_table_covers_every_variant() -> None:
    from custom_components.googlefindmy.FMDNCrypto.eid_generator import EidVariant

    assert set(_UPLOAD_VARIANT_FOR_LOCK) == {v.value for v in EidVariant}


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_variant", sorted(_UPLOAD_VARIANT_FOR_LOCK))
async def test_locked_variant_yields_encryptable_eid(
    lock_variant: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A P-256 lock yields that curve's EID; no WARNING fallback."""
    from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
        EidVariant,
        generate_eid_variant,
    )
    from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
        decrypt_foreign_report,
        encrypt,
    )

    caplog.set_level(logging.DEBUG)
    eid = await _device_eid(lock_variant)

    expected = generate_eid_variant(
        _LOCK_EIK, _LOCK_COUNTER, EidVariant(_UPLOAD_VARIANT_FOR_LOCK[lock_variant])
    )
    assert eid == expected
    assert len(eid) == (20 if lock_variant.startswith("legacy") else 32)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # The owner side decrypts what the finder encrypts for this EID.
    encrypted, sx = encrypt(b"payload", bytes(range(1, 33)), eid)
    result = decrypt_foreign_report([_LOCK_EIK], encrypted, sx, _LOCK_COUNTER)
    assert result.plaintext == b"payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_variant", [None, "no_such_variant"])
async def test_missing_or_unknown_lock_uses_legacy_variant(
    lock_variant: str | None,
) -> None:
    from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
        EidVariant,
        generate_eid_variant,
    )

    eid = await _device_eid(lock_variant)
    assert eid == generate_eid_variant(
        _LOCK_EIK, _LOCK_COUNTER, EidVariant.LEGACY_SECP160R1_X20_BE
    )


def test_truncated_variant_without_full_sibling_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import MappingProxyType

    from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
        _encryptable_eid,
    )
    from custom_components.googlefindmy.FMDNCrypto import eid_generator
    from custom_components.googlefindmy.FMDNCrypto.eid_generator import EidVariant

    trunc = EidVariant.SPEC_P256_X20_TRUNC_BE
    only_trunc = MappingProxyType(
        {
            v: d
            for v, d in eid_generator.VARIANT_DERIVATIONS.items()
            if v is not EidVariant.SPEC_P256_X32_BE
        }
    )
    monkeypatch.setattr(eid_generator, "VARIANT_DERIVATIONS", only_trunc)
    with pytest.raises(ValueError, match="No full-length variant"):
        _encryptable_eid(_LOCK_EIK, _LOCK_COUNTER, trunc)


# --- Counter in seconds, locks as loaded after a restart ------------------

_PAIR_DATE = 1_699_000_000


def _real_resolver() -> Any:
    from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(data={})
    resolver._ensure_cache_defaults()
    for attr in (
        "_lookup",
        "_lookup_metadata",
        "_locks",
        "_persisted_locks",
        "_known_offsets",
        "_known_advertisement_reversed",
        "_known_timebases",
        "_decryption_status",
        "_last_lock_confirmation",
        "_provisioning_warn_at",
    ):
        setattr(resolver, attr, {})
    return resolver


def _restarted_resolver(variant: str) -> Any:
    """Resolver whose only lock came from storage, as after an HA restart."""
    from custom_components.googlefindmy.eid_resolver import EIDGenerationLock

    resolver = _real_resolver()
    stored = EIDGenerationLock(
        device_id="reg-1",
        canonical_id="dev-1",
        variant=variant,
        advertisement_reversed=False,
        eid_length=32,
    ).to_dict()

    async def _load() -> list[dict[str, Any]]:
        return [stored]

    resolver._store = SimpleNamespace(async_load=_load)
    return resolver


def _resolver_lookup_metadata() -> dict[bytes, dict[str, Any]]:
    """EIDs the resolver expects for the device at ``_LOCK_NOW``, unlocked."""
    from custom_components.googlefindmy.coordinator import DeviceIdentity

    resolver = _real_resolver()
    identity = DeviceIdentity(
        registry_id="reg-1",
        canonical_id="dev-1",
        identity_key=_LOCK_EIK,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry",
        fast_pair_model_id=None,
        pair_date=_PAIR_DATE,
    )
    resolver._cached_identities = [identity]
    work_items = resolver._collect_work_items([identity], now_unix=_LOCK_NOW)
    _lookup, metadata, _ids = resolver._build_lookup_sync(
        work_items, _LOCK_NOW, resolver._build_rotation_params()
    )
    return metadata


async def _finder_eid_with(resolver: Any) -> bytes | None:
    from custom_components.googlefindmy.const import DOMAIN
    from custom_components.googlefindmy.fmdn_finder import bermuda_listener

    hass = MagicMock()
    hass.data = {DOMAIN: {"eid_resolver": resolver}}
    coordinator = _coordinator_with_identity()
    coordinator.get_active_device_identities.return_value[0].pair_date = _PAIR_DATE
    with patch.object(bermuda_listener.time, "time", return_value=_LOCK_NOW):
        return await bermuda_listener._async_get_device_eid(hass, coordinator, "dev-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_variant", sorted(_UPLOAD_VARIANT_FOR_LOCK))
async def test_finder_eid_is_in_the_resolvers_current_window(
    lock_variant: str,
) -> None:
    """The finder's EID is one the resolver expects right now.

    The comparison value comes from the resolver's lookup table, not from a
    formula in this test: the EID must be listed for the same variant, the
    ``pair_date`` basis and the rotation window that contains ``_LOCK_NOW``.
    The old ``// 1024`` counter produced an EID the table does not contain.
    """
    resolver = _restarted_resolver(lock_variant)
    await resolver._async_load_locks()
    eid = await _finder_eid_with(resolver)

    metadata = _resolver_lookup_metadata()
    assert eid in metadata
    meta = metadata[eid]
    elapsed = _LOCK_NOW - _PAIR_DATE
    assert meta["variant"] == _UPLOAD_VARIANT_FOR_LOCK[lock_variant]
    assert meta["timestamp_basis"] == "pair_date"
    assert meta["rotation_timestamp"] == elapsed - elapsed % 1024


@pytest.mark.asyncio
async def test_finder_uses_a_lock_loaded_after_restart() -> None:
    """A lock loaded from storage is seen, not only new ones."""
    resolver = _restarted_resolver("spec_p256_x32_be")
    await resolver._async_load_locks()
    assert resolver._persisted_locks == {}  # what the old code read

    eid = await _finder_eid_with(resolver)

    assert eid is not None
    assert len(eid) == 32


# --- The counter the device used: newest match, lock after a restart -------

_SECRETS_DATE = _PAIR_DATE + 50_000
# The counter choice does not depend on the variant; one 20-byte and one
# 32-byte representative cover both EID lengths.
_COUNTER_VARIANTS = ["legacy_secp160r1_x20_be", "spec_p256_x32_be"]


def _identity_two_anchors() -> Any:
    from custom_components.googlefindmy.coordinator import DeviceIdentity

    return DeviceIdentity(
        registry_id="reg-1",
        canonical_id="dev-1",
        identity_key=_LOCK_EIK,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry",
        fast_pair_model_id=None,
        pair_date=_PAIR_DATE,
        secrets_creation_date=_SECRETS_DATE,
    )


def _built_resolver(now: int) -> Any:
    """Resolver with the lookup built for the two-anchor device at ``now``."""
    from tests.test_ble_battery_sensor import _make_resolver

    resolver = _make_resolver()
    identity = _identity_two_anchors()
    resolver._cached_identities = [identity]
    work_items = resolver._collect_work_items([identity], now_unix=now)
    lookup, metadata, _ids = resolver._build_lookup_sync(
        work_items, now, resolver._build_rotation_params()
    )
    resolver._lookup, resolver._lookup_metadata = lookup, metadata
    return resolver


def _pick_eid(
    metadata: dict[bytes, dict[str, Any]], variant: str, basis: str, window: int
) -> bytes:
    """The one lookup EID of ``variant`` under ``basis``, ``window`` periods on.

    ``time_offset`` is the window start minus the unaligned reference counter,
    so the current window has an offset in ``(-1024, 0]`` and window ``k`` one
    in ``((k - 1) * 1024, k * 1024]``.
    """
    hits = [
        eid
        for eid, meta in metadata.items()
        if meta["variant"] == variant
        and basis in meta["timestamp_bases"]
        and -(-meta["time_offset"] // 1024) == window
        and not meta["advertisement_reversed"]
    ]
    assert len(hits) == 1, hits
    return hits[0]


def _observe(resolver: Any, eid: bytes, at: int) -> None:
    """Feed ``eid`` as an advertisement seen at wall time ``at``."""
    from tests.test_ble_battery_sensor import (
        _modern_service_data_payload,
        _service_data_payload,
    )

    build = _modern_service_data_payload if len(eid) == 32 else _service_data_payload
    with (
        patch("time.time", return_value=float(at)),
        patch("time.monotonic", return_value=50_000.0),
    ):
        assert resolver.resolve_eid(build(eid, 0)) is not None


async def _finder_eid_at(resolver: Any, at: int) -> bytes | None:
    from custom_components.googlefindmy.const import DOMAIN
    from custom_components.googlefindmy.fmdn_finder import bermuda_listener

    hass = MagicMock()
    hass.data = {DOMAIN: {"eid_resolver": resolver}}
    coordinator = _coordinator_with_identity()
    coordinator.get_active_device_identities.return_value[0].pair_date = _PAIR_DATE
    with patch.object(bermuda_listener.time, "time", return_value=at):
        return await bermuda_listener._async_get_device_eid(hass, coordinator, "dev-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _COUNTER_VARIANTS)
@pytest.mark.parametrize(
    ("basis", "window"),
    [("secrets_creation_date", 0), ("pair_date", 2)],
    ids=["secrets_creation_date", "pair_date_two_windows_ahead"],
)
async def test_finder_encrypts_for_the_eid_the_device_advertised(
    variant: str, basis: str, window: int
) -> None:
    """The upload EID is the observed EID, not one recomputed from pair_date.

    The comparison value is the advertised EID itself. A match through
    ``secrets_creation_date`` or through a window two periods ahead of
    ``pair_date`` gives a different EID than ``now - pair_date``.
    """
    resolver = _built_resolver(_LOCK_NOW)
    observed = _pick_eid(resolver._lookup_metadata, variant, basis, window)
    _observe(resolver, observed, _LOCK_NOW)

    assert await _finder_eid_at(resolver, _LOCK_NOW) == observed


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _COUNTER_VARIANTS)
async def test_finder_advances_the_matched_window_by_whole_periods(
    variant: str,
) -> None:
    """Three periods after the sighting, the EID three windows later.

    The comparison value is the lookup built at the later time: the EID listed
    under the same basis in the current window. ``(_LOCK_NOW - _SECRETS_DATE) % 1024`` is
    752, so adding ``3 * 1024 + 7`` seconds stays three windows on.
    """
    observed = _pick_eid(
        _built_resolver(_LOCK_NOW)._lookup_metadata,
        variant,
        "secrets_creation_date",
        0,
    )
    resolver = _built_resolver(_LOCK_NOW)
    _observe(resolver, observed, _LOCK_NOW)
    later = _LOCK_NOW + 3 * 1024 + 7
    expected = _pick_eid(
        _built_resolver(later)._lookup_metadata, variant, "secrets_creation_date", 0
    )

    assert expected != observed
    assert await _finder_eid_at(resolver, later) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _COUNTER_VARIANTS)
async def test_finder_uses_the_lock_projection_after_a_restart(variant: str) -> None:
    """Without a match since start, the centre of the lock-tracking windows.

    The lock comes from storage with a rotation timestamp far from the
    ``pair_date`` windows. The comparison value is the lookup EID the
    resolver lists under ``lock_tracking`` in the projected window.
    """
    from custom_components.googlefindmy.eid_resolver import EIDGenerationLock

    resolver = _built_resolver(_LOCK_NOW)
    resolver._lookup, resolver._lookup_metadata = {}, {}
    resolver._locks["reg-1"] = EIDGenerationLock(
        device_id="reg-1",
        canonical_id="dev-1",
        variant=variant,
        advertisement_reversed=False,
        eid_length=32 if variant.startswith(("modern", "spec")) else 20,
        rotation_timestamp=5_000 * 1024,
        time_basis="secrets_creation_date",
        created_at=_LOCK_NOW - 3 * 1024 - 100,
    )
    identity = _identity_two_anchors()
    work_items = resolver._collect_work_items([identity], now_unix=_LOCK_NOW)
    _lookup, metadata, _ids = resolver._build_lookup_sync(
        work_items, _LOCK_NOW, resolver._build_rotation_params()
    )
    expected = _pick_eid(metadata, variant, "lock_tracking", 0)

    assert await _finder_eid_at(resolver, _LOCK_NOW) == expected

"""Tests for FMDN Finder bermuda_listener module.

Tests the Bermuda device_tracker listener that triggers FMDN location uploads
when area changes are detected on Bermuda tracker entities.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
    ATTR_AREA,
    BERMUDA_TRACKER_SUFFIX,
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
async def test_bermuda_state_change_ignores_non_bermuda_trackers(hass_mock: MagicMock) -> None:
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
async def test_bermuda_state_change_ignores_unchanged_area(hass_mock: MagicMock) -> None:
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
    hass.bus.async_listen = MagicMock(return_value=MagicMock())  # Return unsubscribe callable

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
    bermuda_entity.entity_id = "device_tracker.moto_tag_jens_schlusselbund_bermuda_tracker_2"
    bermuda_entity.domain = "device_tracker"
    bermuda_entity.platform = "bermuda"
    bermuda_entity.device_id = ha_device_id  # SAME device!

    # Mock entity registry
    mock_ent_reg = MagicMock()

    with patch(
        "homeassistant.helpers.entity_registry.async_get",
        return_value=mock_ent_reg,
    ), patch(
        "homeassistant.helpers.entity_registry.async_entries_for_device",
        return_value=[gfm_entity, bermuda_entity],  # Both entities on same device
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

    with patch(
        "homeassistant.helpers.entity_registry.async_get",
        return_value=mock_ent_reg,
    ), patch(
        "homeassistant.helpers.entity_registry.async_entries_for_device",
        return_value=[bermuda_only_entity],  # No GoogleFindMy entity
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

    with patch(
        "homeassistant.helpers.entity_registry.async_get",
        return_value=mock_ent_reg,
    ), patch(
        "homeassistant.helpers.entity_registry.async_entries_for_device",
        return_value=[gfm_entity],
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

    with patch(
        "homeassistant.helpers.entity_registry.async_get",
        return_value=mock_ent_reg,
    ), patch(
        "homeassistant.helpers.entity_registry.async_entries_for_device",
        return_value=[gfm_sensor, gfm_tracker],  # Sensor comes first
    ):
        result = await _async_find_googlefindmy_device(hass, ha_device_id)

    assert result is not None
    # Should find the device_tracker, not the sensor
    assert result["device_id"] == "target_device_id"

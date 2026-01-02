"""Tests for FMDN Finder bermuda_listener module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.googlefindmy.fmdn_finder.bermuda_listener import (
    ATTR_FMDN_EID,
    async_setup_bermuda_listener,
    async_unload_bermuda_listener,
)


@pytest.mark.asyncio
async def test_setup_bermuda_listener(hass_mock):
    """Test Bermuda listener setup."""
    await async_setup_bermuda_listener(hass_mock)

    # Verify event listener was registered
    assert hass_mock.bus.async_listen.called


@pytest.mark.asyncio
async def test_unload_bermuda_listener(hass_mock):
    """Test Bermuda listener unload."""
    # Setup first
    await async_setup_bermuda_listener(hass_mock)

    # Now unload
    await async_unload_bermuda_listener(hass_mock)

    # Should call unsubscribe callback
    # (implementation depends on actual storage structure)


@pytest.mark.asyncio
async def test_bermuda_state_change_with_valid_fmdn_eid(hass_mock):
    """Test that valid FMDN beacon triggers location upload."""
    from homeassistant.const import EVENT_STATE_CHANGED

    # Mock location uploader
    mock_uploader = AsyncMock()

    with patch(
        "custom_components.googlefindmy.fmdn_finder.location_uploader.async_process_fmdn_beacon_detection",
        mock_uploader,
    ):
        await async_setup_bermuda_listener(hass_mock)

        # Get the registered callback
        callback_calls = hass_mock.bus.async_listen.call_args_list
        assert len(callback_calls) > 0

        # Extract the callback function
        event_type, callback = callback_calls[0][0]
        assert event_type == EVENT_STATE_CHANGED

        # Simulate Bermuda state change event
        mock_event = MagicMock()
        mock_event.data = {
            "entity_id": "sensor.bermuda_fmdn_device_pixel_buds",
            "new_state": MagicMock(
                attributes={
                    ATTR_FMDN_EID: "0123456789abcdef01234567" "89abcdef01234567",  # 40 hex chars = 20 bytes
                    "area": "living_room",
                    "rssi": -65,
                    "source": "AA:BB:CC:DD:EE:FF",
                    "scanner_device_id": "scanner_123",
                    "fmdn_device_id": "device_456",
                }
            ),
        }

        # Trigger callback
        callback(mock_event)

        # Verify async_create_task was called
        assert hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_ignores_non_fmdn_entities(hass_mock):
    """Test that non-FMDN entities are ignored."""

    await async_setup_bermuda_listener(hass_mock)

    # Get the registered callback
    callback_calls = hass_mock.bus.async_listen.call_args_list
    callback = callback_calls[0][0][1]

    # Simulate non-FMDN entity state change
    mock_event = MagicMock()
    mock_event.data = {
        "entity_id": "sensor.bermuda_regular_device",  # No 'fmdn' in name
        "new_state": MagicMock(attributes={}),
    }

    # Trigger callback
    callback(mock_event)

    # Should not create upload task
    assert not hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_validates_eid_length(hass_mock):
    """Test that invalid EID lengths are rejected."""

    mock_uploader = AsyncMock()

    with patch(
        "custom_components.googlefindmy.fmdn_finder.location_uploader.async_process_fmdn_beacon_detection",
        mock_uploader,
    ):
        await async_setup_bermuda_listener(hass_mock)

        callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

        # Simulate state change with invalid EID length (15 bytes instead of 20/32)
        mock_event = MagicMock()
        mock_event.data = {
            "entity_id": "sensor.bermuda_fmdn_device_test",
            "new_state": MagicMock(
                attributes={
                    ATTR_FMDN_EID: "0123456789abcdef01234567" "89ab",  # 30 hex chars = 15 bytes (invalid)
                    "area": "bedroom",
                }
            ),
        }

        callback(mock_event)

        # Should not create task for invalid EID length
        assert not hass_mock.async_create_task.called


@pytest.mark.asyncio
async def test_bermuda_state_change_accepts_32_byte_eid(hass_mock):
    """Test that 32-byte EID is accepted (FMDN spec allows 20 or 32)."""

    mock_uploader = AsyncMock()

    with patch(
        "custom_components.googlefindmy.fmdn_finder.location_uploader.async_process_fmdn_beacon_detection",
        mock_uploader,
    ):
        await async_setup_bermuda_listener(hass_mock)

        callback = hass_mock.bus.async_listen.call_args_list[0][0][1]

        # 32-byte EID (64 hex characters)
        eid_32_bytes = "0123456789abcdef" * 4

        mock_event = MagicMock()
        mock_event.data = {
            "entity_id": "sensor.bermuda_fmdn_device_test",
            "new_state": MagicMock(
                attributes={
                    ATTR_FMDN_EID: eid_32_bytes,
                    "area": "office",
                    "rssi": -55,
                }
            ),
        }

        callback(mock_event)

        # Should create upload task
        assert hass_mock.async_create_task.called


@pytest.fixture
def hass_mock():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.data = {}
    hass.bus = MagicMock()
    hass.bus.async_listen = MagicMock(return_value=MagicMock())  # Return unsubscribe callable
    hass.async_create_task = MagicMock()
    return hass

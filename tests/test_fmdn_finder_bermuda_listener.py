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
    hass.async_create_task = MagicMock()
    return hass

"""Tests for FMDN Finder location_uploader module."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.googlefindmy.fmdn_finder.location_uploader import (
    DEFAULT_HOME_ZONE_ACCURACY,
    LocationData,
    _calculate_accuracy_from_rssi,
    _haversine_distance,
    _location_from_zone_state,
    _should_upload_location,
    async_process_fmdn_beacon_detection,
)


def test_haversine_distance():
    """Test Haversine distance calculation."""
    # Same point
    distance = _haversine_distance(52.5200, 13.4050, 52.5200, 13.4050)
    assert distance == pytest.approx(0.0, abs=1.0)

    # Berlin to Munich (~500km)
    berlin_lat, berlin_lon = 52.5200, 13.4050
    munich_lat, munich_lon = 48.1351, 11.5820
    distance = _haversine_distance(berlin_lat, berlin_lon, munich_lat, munich_lon)
    assert 500_000 < distance < 550_000  # ~504km expected

    # Short distance (~111m)
    lat1, lon1 = 52.5200, 13.4050
    lat2, lon2 = 52.5210, 13.4050  # Move 0.001 degrees (~111m) north
    distance = _haversine_distance(lat1, lon1, lat2, lon2)
    assert 100 < distance < 120


def test_calculate_accuracy_from_rssi():
    """Test RSSI to accuracy conversion."""
    # Very close (strong signal)
    accuracy = _calculate_accuracy_from_rssi(-55, zone_accuracy=50)
    assert accuracy == 50  # Max of RSSI-based (2m) and zone (50m)

    # Medium distance
    accuracy = _calculate_accuracy_from_rssi(-75, zone_accuracy=30)
    assert accuracy == 30  # Max of RSSI-based (~5-10m) and zone (30m)

    # Far (weak signal)
    accuracy = _calculate_accuracy_from_rssi(-95, zone_accuracy=50)
    assert accuracy == 50  # Max of RSSI-based (30m) and zone (50m)

    # Very weak signal
    accuracy = _calculate_accuracy_from_rssi(-100, zone_accuracy=20)
    assert accuracy == 30  # RSSI-based (30m) > zone (20m)


@pytest.mark.asyncio
async def test_should_upload_location_first_upload(hass_mock):
    """Test that first upload is always allowed."""
    location = LocationData(
        latitude=52.5200, longitude=13.4050, accuracy=50, timestamp=time.time()
    )

    should_upload, reason = await _should_upload_location(hass_mock, "abc123", location)

    assert should_upload is True
    assert reason == "first_upload"


@pytest.mark.asyncio
async def test_should_upload_location_throttled_by_time(hass_mock):
    """Test upload throttling by time interval."""
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    # Simulate previous upload 60 seconds ago (less than MIN_UPLOAD_INTERVAL_SECONDS)
    old_location = LocationData(
        latitude=52.5200,
        longitude=13.4050,
        accuracy=50,
        timestamp=time.time() - 60,
    )

    cache_entry = UploadCacheEntry(
        eid_hex="abc123",
        location=old_location,
        timestamp=time.time() - 60,
    )

    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {"abc123": cache_entry}

    # Try to upload same location
    new_location = LocationData(
        latitude=52.5200, longitude=13.4050, accuracy=50, timestamp=time.time()
    )

    should_upload, reason = await _should_upload_location(
        hass_mock, "abc123", new_location
    )

    assert should_upload is False
    assert "too_soon" in reason


@pytest.mark.asyncio
async def test_should_upload_location_throttled_by_distance(hass_mock):
    """Test upload throttling by distance threshold."""
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    # Simulate previous upload 400 seconds ago (enough time)
    # but only moved 20 meters (less than MIN_UPLOAD_DISTANCE_METERS)
    old_location = LocationData(
        latitude=52.5200,
        longitude=13.4050,
        accuracy=50,
        timestamp=time.time() - 400,
    )

    cache_entry = UploadCacheEntry(
        eid_hex="abc123",
        location=old_location,
        timestamp=time.time() - 400,
    )

    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {"abc123": cache_entry}

    # Move ~20 meters north (0.0002 degrees ~= 22m)
    new_location = LocationData(
        latitude=52.5202,
        longitude=13.4050,
        accuracy=50,
        timestamp=time.time(),
    )

    should_upload, reason = await _should_upload_location(
        hass_mock, "abc123", new_location
    )

    assert should_upload is False
    assert "too_close" in reason


@pytest.mark.asyncio
async def test_should_upload_location_allowed_by_distance(hass_mock):
    """Test upload allowed when distance threshold met."""
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    # Simulate previous upload 400 seconds ago
    old_location = LocationData(
        latitude=52.5200,
        longitude=13.4050,
        accuracy=50,
        timestamp=time.time() - 400,
    )

    cache_entry = UploadCacheEntry(
        eid_hex="abc123",
        location=old_location,
        timestamp=time.time() - 400,
    )

    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {"abc123": cache_entry}

    # Move ~70 meters north (0.0006 degrees ~= 67m)
    new_location = LocationData(
        latitude=52.5206,
        longitude=13.4050,
        accuracy=50,
        timestamp=time.time(),
    )

    should_upload, reason = await _should_upload_location(
        hass_mock, "abc123", new_location
    )

    assert should_upload is True
    assert "distance_threshold_met" in reason


@pytest.mark.asyncio
async def test_should_upload_location_accuracy_improved(hass_mock):
    """Test upload allowed when accuracy improves significantly."""
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    # Simulate previous upload with 100m accuracy
    old_location = LocationData(
        latitude=52.5200,
        longitude=13.4050,
        accuracy=100,
        timestamp=time.time() - 400,
    )

    cache_entry = UploadCacheEntry(
        eid_hex="abc123",
        location=old_location,
        timestamp=time.time() - 400,
    )

    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {"abc123": cache_entry}

    # Same location but much better accuracy (70m = 70% of 100m, threshold is 80%)
    new_location = LocationData(
        latitude=52.5200,
        longitude=13.4050,
        accuracy=70,
        timestamp=time.time(),
    )

    should_upload, reason = await _should_upload_location(
        hass_mock, "abc123", new_location
    )

    assert should_upload is True
    assert "accuracy_improved" in reason


@pytest.fixture
def hass_mock():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = MagicMock()
    return hass


@pytest.mark.asyncio
async def test_process_fmdn_beacon_no_location(hass_mock):
    """Test beacon processing when no location is available."""
    with patch(
        "custom_components.googlefindmy.fmdn_finder.location_uploader._resolve_scanner_location",
        return_value=None,
    ):
        # Should return early without uploading
        await async_process_fmdn_beacon_detection(
            hass=hass_mock,
            eid=b"\\x01" * 20,
            area="living_room",
            rssi=-70,
            scanner_address="AA:BB:CC:DD:EE:FF",
            scanner_device_id="scanner_123",
            fmdn_device_id="device_456",
            entity_id="sensor.bermuda_fmdn_test",
        )

        # No upload should occur
        assert not hass_mock.async_create_task.called


_NO_RADIUS = object()


def _zone_state(radius: object = _NO_RADIUS, *, lat: float = 52.52, lon: float = 13.405):
    """Build a minimal zone State stub for ``_location_from_zone_state``.

    Passing ``radius=_NO_RADIUS`` omits the ``radius`` attribute entirely so the
    default-fallback path is exercised.
    """
    attributes: dict[str, object] = {"latitude": lat, "longitude": lon}
    if radius is not _NO_RADIUS:
        attributes["radius"] = radius
    return SimpleNamespace(attributes=attributes)


@pytest.mark.parametrize("radius", [10, 50, 100, 500])
def test_zone_accuracy_equals_radius(radius):
    """Contract: zone accuracy IS the zone radius (H1 regression).

    The previous ``max(int(radius), 100)`` floored accuracy up to 100 m and thus
    reported every zone smaller than 100 m (including the 50 m default home zone)
    as less accurate than it really is. Accuracy must equal the radius.
    """
    loc = _location_from_zone_state(_zone_state(radius), "home")
    assert loc.accuracy == radius
    assert loc.zone_name == "home"


def test_zone_accuracy_missing_radius_uses_default():
    """Terminal path: an absent ``radius`` attribute falls back to the default."""
    loc = _location_from_zone_state(_zone_state(), "home")
    assert loc.accuracy == DEFAULT_HOME_ZONE_ACCURACY


@pytest.mark.parametrize(
    ("radius", "expected"),
    [
        (50.0, 50),  # HA stores zone radius as float; whole values map cleanly
        (77.5, 77),  # fractional radius is truncated (int()), contract pinned
        (99.9, 99),
    ],
)
def test_zone_accuracy_float_radius_truncated(radius, expected):
    """Contract: float radii (HA's native storage type) truncate via ``int()``.

    Pins the intentional ``int()`` behaviour so a later ``round()`` change would
    surface as a deliberate contract update rather than a silent drift.
    """
    loc = _location_from_zone_state(_zone_state(radius), "home")
    assert loc.accuracy == expected


def test_zone_accuracy_not_floored_to_100():
    """H1 core: a sub-100 m zone is no longer inflated to 100 m."""
    loc = _location_from_zone_state(_zone_state(50), "home")
    # Regression guard against the reintroduction of the 100 m floor.
    assert loc.accuracy == 50
    assert loc.accuracy != 100


@pytest.mark.asyncio
async def test_zone_accuracy_improvement_branch_reachable(hass_mock):
    """H1 coupling: with accuracy == radius, the improvement branch can fire.

    Under the old floor both the previous and the new zone location reported
    100 m, so ``new < last * 0.8`` was never true. With accuracy tracking the
    radius, moving from a 100 m zone to a 50 m zone is a real improvement.
    """
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    prev_loc = _location_from_zone_state(_zone_state(100), "work")
    new_loc = _location_from_zone_state(_zone_state(50), "home")
    assert prev_loc.accuracy == 100
    assert new_loc.accuracy == 50

    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {
        "device_h1": UploadCacheEntry(
            eid_hex="device_h1",
            location=prev_loc,
            timestamp=time.time() - 400,
        )
    }

    should_upload, reason = await _should_upload_location(
        hass_mock, "device_h1", new_loc
    )
    assert should_upload is True
    assert "accuracy_improved" in reason

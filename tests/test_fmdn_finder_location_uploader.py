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


def _zone_state(
    radius: object = _NO_RADIUS, *, lat: float = 52.52, lon: float = 13.405
):
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


# ---------------------------------------------------------------------------
# H8: RSSI accuracy must be applied BEFORE the throttling decision so the value
# evaluated for accuracy_improved is the exact value cached and uploaded.
# Latent/regression-prevention (P2): the only production caller currently passes
# rssi=None, so the loop is dormant today; it becomes active as soon as a GPS-only
# detection path supplies a real RSSI. The H1 fix (removal of the 100 m floor)
# demasked it. See async_process_fmdn_beacon_detection step 2.
# ---------------------------------------------------------------------------


def _resolved_location(accuracy: int, *, zone_name: str = "home") -> LocationData:
    """A resolved scanner location with the given accuracy (zone radius)."""
    return LocationData(
        latitude=52.52,
        longitude=13.405,
        accuracy=accuracy,
        zone_name=zone_name,
        timestamp=time.time(),
    )


@pytest.mark.asyncio
async def test_rssi_adjustment_before_throttle_no_phantom_upload(hass_mock):
    """H8 regression: a weak-RSSI detection must not phantom-upload every window.

    Sequence: a prior identical detection cached the RSSI-raised accuracy (20 m).
    The next identical detection resolves the raw zone radius (5 m). If the RSSI
    adjustment ran AFTER the throttle decision (the bug), the decision would see
    5 m vs. the cached 20 m, wrongly pass ``accuracy_improved`` (5 < 20*0.8=16)
    and re-upload. With the adjustment applied BEFORE the decision, it sees the
    same 20 m that was cached (max(5, RSSI-floor 20)) -> no improvement -> no
    upload. This test is mutation-sharp: reverting the fix turns it red.
    """
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    eid = b"\\xab" * 20
    eid_hex = eid.hex()

    # Prior upload cached the RSSI-raised accuracy (20 m) at the same position.
    hass_mock.data[DATA_FMDN_UPLOAD_CACHE] = {
        eid_hex: UploadCacheEntry(
            eid_hex=eid_hex,
            location=_resolved_location(20),
            timestamp=time.time() - 400,
            semantic_area=None,
        )
    }

    with (
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._resolve_scanner_location",
            return_value=_resolved_location(5),
        ),
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._encrypt_and_upload_location",
            return_value=True,
        ) as mock_upload,
    ):
        result = await async_process_fmdn_beacon_detection(
            hass=hass_mock,
            eid=eid,
            area=None,  # GPS-only path (the affected branch)
            rssi=-85,  # FAR band -> RSSI floor 20 m
            scanner_address="AA:BB:CC:DD:EE:FF",
            scanner_device_id="scanner_123",
            fmdn_device_id="device_456",
            entity_id="sensor.bermuda_fmdn_test",
        )

    assert result is False
    mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_rssi_adjustment_cached_value_equals_uploaded_value(hass_mock):
    """H8 invariant: the uploaded accuracy and the cached accuracy are identical.

    First upload (cache empty) with a 5 m zone and a weak RSSI (floor 20 m): the
    value handed to the encrypt/upload call and the value written to the cache
    must both be the RSSI-adjusted 20 m, not the raw 5 m.
    """
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    eid = b"\\xcd" * 20
    eid_hex = eid.hex()

    with (
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._resolve_scanner_location",
            return_value=_resolved_location(5),
        ),
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._encrypt_and_upload_location",
            return_value=True,
        ) as mock_upload,
    ):
        result = await async_process_fmdn_beacon_detection(
            hass=hass_mock,
            eid=eid,
            area=None,
            rssi=-85,  # FAR band -> RSSI floor 20 m
            scanner_address="AA:BB:CC:DD:EE:FF",
            scanner_device_id="scanner_123",
            fmdn_device_id="device_456",
            entity_id="sensor.bermuda_fmdn_test",
        )

    assert result is True
    # location is the 3rd positional arg of _encrypt_and_upload_location(hass, eid, location, area)
    uploaded_location = mock_upload.call_args.args[2]
    assert uploaded_location.accuracy == 20

    cached: UploadCacheEntry = hass_mock.data[DATA_FMDN_UPLOAD_CACHE][eid_hex]
    assert cached.location.accuracy == 20


@pytest.mark.asyncio
async def test_rssi_none_leaves_accuracy_unchanged(hass_mock):
    """H8 branch: the current production path (rssi=None) applies no adjustment.

    Covers the ``if rssi is not None`` false branch after the move: the uploaded
    and cached accuracy stay at the resolved zone accuracy (50 m).
    """
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    eid = b"\\xef" * 20
    eid_hex = eid.hex()

    with (
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._resolve_scanner_location",
            return_value=_resolved_location(50),
        ),
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._encrypt_and_upload_location",
            return_value=True,
        ) as mock_upload,
    ):
        result = await async_process_fmdn_beacon_detection(
            hass=hass_mock,
            eid=eid,
            area=None,
            rssi=None,  # production caller passes None today
            scanner_address="AA:BB:CC:DD:EE:FF",
            scanner_device_id="scanner_123",
            fmdn_device_id="device_456",
            entity_id="sensor.bermuda_fmdn_test",
        )

    assert result is True
    assert mock_upload.call_args.args[2].accuracy == 50
    cached: UploadCacheEntry = hass_mock.data[DATA_FMDN_UPLOAD_CACHE][eid_hex]
    assert cached.location.accuracy == 50


@pytest.mark.asyncio
async def test_rssi_adjustment_does_not_lower_accuracy(hass_mock):
    """H8 boundary: when the zone radius already exceeds the RSSI floor, ``max``
    keeps the larger zone value (a strong signal never fabricates precision).
    """
    from custom_components.googlefindmy.fmdn_finder.location_uploader import (
        DATA_FMDN_UPLOAD_CACHE,
        UploadCacheEntry,
    )

    eid = b"\\x11" * 20
    eid_hex = eid.hex()

    with (
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._resolve_scanner_location",
            return_value=_resolved_location(50),
        ),
        patch(
            "custom_components.googlefindmy.fmdn_finder.location_uploader._encrypt_and_upload_location",
            return_value=True,
        ) as mock_upload,
    ):
        result = await async_process_fmdn_beacon_detection(
            hass=hass_mock,
            eid=eid,
            area=None,
            rssi=-55,  # VERY_CLOSE band -> RSSI floor 2 m, below the 50 m zone
            scanner_address="AA:BB:CC:DD:EE:FF",
            scanner_device_id="scanner_123",
            fmdn_device_id="device_456",
            entity_id="sensor.bermuda_fmdn_test",
        )

    assert result is True
    assert mock_upload.call_args.args[2].accuracy == 50
    cached: UploadCacheEntry = hass_mock.data[DATA_FMDN_UPLOAD_CACHE][eid_hex]
    assert cached.location.accuracy == 50


@pytest.mark.parametrize(
    ("rssi", "expected_distance"),
    [
        # Just inside each band (strictly greater than the threshold).
        (-59, 2),  # > -60 -> very close
        (-69, 5),  # > -70 -> close
        (-79, 10),  # > -80 -> medium
        (-89, 20),  # > -90 -> far
        # Exactly on each threshold: the comparison is strict `>`, so the value
        # falls into the NEXT (weaker) band. These edges lock the boundaries the
        # terminal review flagged as untested.
        (-60, 5),  # == -60 -> not very close, drops to close
        (-70, 10),  # == -70 -> not close, drops to medium
        (-80, 20),  # == -80 -> not medium, drops to far
        (-90, 30),  # == -90 -> not far, drops to very far
        (-120, 30),  # far below the last threshold -> very far
    ],
)
def test_calculate_accuracy_from_rssi_band_boundaries(rssi, expected_distance):
    """Lock the exact RSSI-band edges (strict `>` semantics).

    zone_accuracy=1 keeps the zone floor below every RSSI estimate, so the
    returned value is the pure band distance and the boundary is observable.
    """
    assert _calculate_accuracy_from_rssi(rssi, zone_accuracy=1) == expected_distance

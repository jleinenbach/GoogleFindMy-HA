# tests/test_decoder_location_ranking.py
"""Regression tests for decoder location prioritization heuristics."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    _merge_semantics_if_near_ts,
    _select_best_location,
    get_devices_with_location,
)

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache


def test_decoder_prefers_newer_coordinates_over_owner_status() -> None:
    """A fresher aggregated report with coordinates outranks an older owner report."""

    older_owner = {
        "status": "OWNER",
        "is_own_report": True,
        "last_seen": 1_700_000_000,
        "latitude": 52.5200,
        "longitude": 13.4050,
        "accuracy": 120.0,
        "altitude": 35.0,
    }

    fresher_aggregated = {
        "status": "aggregated",
        "is_own_report": False,
        "last_seen": 1_700_000_500,
        "latitude": 48.8566,
        "longitude": 2.3522,
        "accuracy": 250.0,
        "altitude": 40.5,
        "_report_hint": "high_traffic",
    }

    best, _ = _select_best_location([older_owner, fresher_aggregated])

    assert best["status"] == "aggregated"
    assert best["last_seen"] == 1_700_000_500.0
    assert best["altitude"] == 40.5


def test_decoder_promotes_newer_semantic_only_report() -> None:
    """Semantic-only refresh keeps coordinates but updates recency metadata."""

    coordinate_fix = {
        "status": "aggregated",
        "last_seen": 1_700_000_000,
        "latitude": 52.5200,
        "longitude": 13.4050,
        "accuracy": 120.0,
    }

    semantic_only = {
        "status": "semantic_only",
        "last_seen": 1_700_000_900,
        "semantic_name": "Gym",
        "_report_hint": "semantic_only",
    }

    best, normed = _select_best_location([coordinate_fix, semantic_only])
    assert best is not None

    merged = _merge_semantics_if_near_ts(best, normed)

    assert merged["latitude"] == 52.52
    assert merged["longitude"] == 13.405
    assert merged["last_seen"] == 1_700_000_900.0
    assert merged["semantic_name"] == "Gym"

    devices_list = DeviceUpdate_pb2.DevicesList()
    device = devices_list.deviceMetadata.add()
    device.userDefinedDeviceName = "Tracker"
    canonic = device.identifierInformation.canonicIds.canonicId.add()
    canonic.id = "device-123"

    # Ensure the proto advertises report availability so decrypt is invoked.
    reports = device.information.locationInformation.reports
    recent_location = reports.recentLocationAndNetworkLocations.recentLocation
    recent_location.semanticLocation.locationName = "seed"

    with patch(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations.decrypt_location_response_locations",
        return_value=[coordinate_fix, semantic_only],
    ):
        rows = get_devices_with_location(
            devices_list,
            cache=cast("TokenCache", object()),
        )

    assert len(rows) == 1
    row = rows[0]
    assert row["device_id"] == "device-123"
    assert row["latitude"] == 52.52
    assert row["longitude"] == 13.405
    assert row["last_seen"] == 1_700_000_900.0
    assert row["semantic_name"] == "Gym"


def test_semantic_report_outranks_older_coordinate_candidate() -> None:
    """A fresher semantic report without coords takes selection precedence."""

    coordinate_fix = {
        "status": "aggregated",
        "last_seen": 1_700_000_000,
        "latitude": 37.7749,
        "longitude": -122.4194,
        "accuracy": 30.0,
    }

    semantic_only = {
        "status": "semantic_only",
        "last_seen": 1_700_000_950,
        "semantic_name": "Office",
    }

    best, normed = _select_best_location([coordinate_fix, semantic_only])

    assert best["status"] == "semantic_only"
    assert "latitude" not in best or best["latitude"] is None
    assert best["last_seen"] == 1_700_000_950.0

    merged = _merge_semantics_if_near_ts(best, normed)

    assert merged["latitude"] == 37.7749
    assert merged["longitude"] == -122.4194
    assert merged["last_seen"] == 1_700_000_950.0
    assert merged["semantic_name"] == "Office"

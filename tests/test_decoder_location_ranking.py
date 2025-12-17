# tests/test_decoder_location_ranking.py
"""Regression tests for decoder location prioritization heuristics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    _merge_semantics_if_near_ts,
    _select_best_location,
    get_devices_with_location,
)

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache


MERGED_LATITUDE = 37.7749
MERGED_LONGITUDE = -122.4194
MERGED_LAST_SEEN = 1_700_000_950.0


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


def test_decoder_logs_decryption_failures(caplog: pytest.LogCaptureFixture) -> None:
    """Decryption errors surface as warnings instead of silently dropping context."""

    devices_list = DeviceUpdate_pb2.DevicesList()
    device = devices_list.deviceMetadata.add()
    device.userDefinedDeviceName = "Tracker"
    canonic = device.identifierInformation.canonicIds.canonicId.add()
    canonic.id = "device-456"

    reports = device.information.locationInformation.reports
    reports.recentLocationAndNetworkLocations.recentLocation.semanticLocation.locationName = (
        "seed"
    )

    with patch(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations.decrypt_location_response_locations",
        side_effect=ValueError("boom"),
    ), caplog.at_level(logging.WARNING):
        rows = get_devices_with_location(
            devices_list,
            cache=cast("TokenCache", object()),
        )

    assert len(rows) == 1
    assert rows[0]["device_id"] == "device-456"
    assert any(
        "Failed to decrypt location for device 'Tracker': boom" in message
        for message in caplog.messages
    )


def test_device_list_preserves_anchor_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metadata from decrypted device list payloads must survive filtering."""

    devices_list = DeviceUpdate_pb2.DevicesList()
    device = devices_list.deviceMetadata.add()
    device.userDefinedDeviceName = "Tracker"
    canonic = device.identifierInformation.canonicIds.canonicId.add()
    canonic.id = "device-anchor"

    reports = device.information.locationInformation.reports
    reports.recentLocationAndNetworkLocations.recentLocation.semanticLocation.locationName = (
        "seed"
    )

    anchor_payload = {
        "pair_date": 1_700_000_100,
        "secrets_creation_date": 1_700_000_200,
        "identity_key": b"\xaa" * 32,
        "identity_key_candidates": [b"\xaa" * 32, b"\xbb" * 32],
        "encrypted_identity_key_candidates": [b"\x01\x02"],
        "device_registration": {"pairDate": 1_700_000_300},
        "encrypted_user_secrets": {"creationDate": 1_700_000_400},
        "time_anchors_debug": {"source": "device_list"},
        "metadata_only": True,
    }

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations.decrypt_location_response_locations",
        lambda *args, **kwargs: [anchor_payload],
    )

    rows = get_devices_with_location(devices_list, cache=cast("TokenCache", object()))

    assert len(rows) == 1
    row = rows[0]

    assert row["pair_date"] == anchor_payload["pair_date"]
    assert row["secrets_creation_date"] == anchor_payload["secrets_creation_date"]
    assert row["identity_key"] == anchor_payload["identity_key"]
    assert row["identity_key_candidates"] == anchor_payload["identity_key_candidates"]
    assert row["encrypted_identity_key_candidates"] == anchor_payload[
        "encrypted_identity_key_candidates"
    ]
    assert row["device_registration"] == anchor_payload["device_registration"]
    assert row["encrypted_user_secrets"] == anchor_payload["encrypted_user_secrets"]
    assert row["time_anchors_debug"] == anchor_payload["time_anchors_debug"]
    assert row["metadata_only"] is True


def test_semantic_report_outranks_older_coordinate_candidate() -> None:
    """A fresher semantic report without coords takes selection precedence."""

    coordinate_fix = {
        "status": "aggregated",
        "last_seen": 1_700_000_000,
        "latitude": MERGED_LATITUDE,
        "longitude": MERGED_LONGITUDE,
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
    assert best["last_seen"] == MERGED_LAST_SEEN

    merged = _merge_semantics_if_near_ts(best, normed)

    assert merged["latitude"] == MERGED_LATITUDE
    assert merged["longitude"] == MERGED_LONGITUDE
    assert merged["last_seen"] == MERGED_LAST_SEEN
    assert merged["semantic_name"] == "Office"

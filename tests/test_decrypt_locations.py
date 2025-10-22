# tests/test_decrypt_locations.py
"""Regression tests for decrypting location payloads."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2


def test_async_decrypt_location_response_locations_allows_future_owner_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner reports within a realistic future drift window are preserved."""

    base_now = 1_700_000_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    location_proto = DeviceUpdate_pb2.Location()
    location_proto.latitude = int(52.0 * 1e7)
    location_proto.longitude = int(13.0 * 1e7)
    location_proto.altitude = 1337
    location_bytes = location_proto.SerializeToString()

    async def fake_identity_key(*_args, **_kwargs) -> bytes:
        return b"\x42" * 32

    async def fake_offload(*_args, **_kwargs) -> bytes:
        return location_bytes

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    owner_timestamp = int(base_now + 2 * 3600)
    reports.recentLocationTimestamp.seconds = owner_timestamp

    recent = reports.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = 5.0
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""
    encrypted_report.encryptedLocation = b"ignored"
    encrypted_report.isOwnReport = True

    result = asyncio.run(
        decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == owner_timestamp
    assert entry["is_own_report"] is True
    assert entry["altitude"] == 1337
    assert entry["accuracy"] == 5.0

# tests/test_decrypt_locations.py
"""Regression tests for decrypting location payloads."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2

ALTITUDE_METERS = 1337
ACCURACY_METERS = 5.0


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

    async def fake_identity_key(*_args, **_kwargs) -> list[bytes]:
        return [b"\x42" * 32]

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
    assert entry["altitude"] == ALTITUDE_METERS
    assert entry["accuracy"] == ACCURACY_METERS


def test_async_decrypt_location_response_locations_aligns_missing_network_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent timestamps stay aligned when historic timestamps are missing."""

    valid_location = DeviceUpdate_pb2.Location()
    valid_location.latitude = int(40.0 * 1e7)
    valid_location.longitude = int(-74.0 * 1e7)
    valid_location.altitude = ALTITUDE_METERS

    invalid_location = DeviceUpdate_pb2.Location()
    invalid_location.latitude = int(91.0 * 1e7)  # Out of bounds → dropped
    invalid_location.longitude = 0
    invalid_location.altitude = ALTITUDE_METERS

    def serialize_location(loc: DeviceUpdate_pb2.Location) -> bytes:
        return loc.SerializeToString()

    async def fake_identity_key(*_args, **_kwargs) -> list[bytes]:
        return [b"\x01" * 32]

    async def fake_offload_aes(
        _identity_key: bytes, encrypted_location: bytes
    ) -> bytes:
        if encrypted_location == b"recent":
            return serialize_location(valid_location)
        return serialize_location(invalid_location)

    async def fake_offload_foreign(
        _identity_key: bytes,
        encrypted_location: bytes,
        *_args: object,
        **_kwargs: object,
    ) -> bytes:
        return await fake_offload_aes(_identity_key, encrypted_location)

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)
    monkeypatch.setattr(
        decrypt_locations, "_offload_decrypt_foreign", fake_offload_foreign
    )
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda *_: False)

    update = DeviceUpdate_pb2.DeviceUpdate()
    update.deviceMetadata.information.deviceRegistration.SetInParent()

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations

    network_location = reports.networkLocations.add()
    network_location.status = Common_pb2.Status.LAST_KNOWN
    network_location.geoLocation.accuracy = ACCURACY_METERS
    network_enc = network_location.geoLocation.encryptedReport
    network_enc.publicKeyRandom = b""
    network_enc.encryptedLocation = b"network"
    network_enc.isOwnReport = False

    recent_timestamp = 1_700_000_321
    reports.recentLocationTimestamp.seconds = recent_timestamp
    recent_location = reports.recentLocation
    recent_location.SetInParent()
    recent_location.status = Common_pb2.Status.LAST_KNOWN
    recent_location.geoLocation.accuracy = ACCURACY_METERS
    recent_enc = recent_location.geoLocation.encryptedReport
    recent_enc.publicKeyRandom = b""
    recent_enc.encryptedLocation = b"recent"
    recent_enc.isOwnReport = True

    result = asyncio.run(
        decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == recent_timestamp
    assert entry["latitude"] == pytest.approx(40.0)
    assert entry["longitude"] == pytest.approx(-74.0)
    assert entry["is_own_report"] is True


def test_async_decrypt_location_response_locations_returns_metadata_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing and secrets anchors propagate even without reports."""

    base_now = 1_700_100_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x41" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 120)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 60)
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 32

    result = asyncio.run(
        decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is True
    assert entry["pair_date"] == int(base_now - 120)
    assert entry["secrets_creation_date"] == int(base_now - 60)
    registration_meta = entry.get("device_registration")
    assert isinstance(registration_meta, dict)
    assert registration_meta.get("pairDate") == int(base_now - 120)
    secrets_meta = entry.get("encrypted_user_secrets")
    assert isinstance(secrets_meta, dict)
    assert secrets_meta.get("creationDate") == int(base_now - 60)


def test_async_decrypt_location_response_locations_normalizes_anchor_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchor extraction normalizes millisecond-like inputs and preserves keys."""

    base_now = 1_700_200_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x51" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 300)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 150)
    registration.encryptedUserSecrets.creationDate.nanos = 500_000_000
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x10" * 32

    result = asyncio.run(
        decrypt_locations.async_decrypt_location_response_locations(
            update, cache=object()
        )
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is True
    assert entry["pair_date"] == int(base_now - 300)
    assert entry["secrets_creation_date"] == int(base_now - 150)
    assert entry["identity_key"] == b"\x51" * 32
    assert entry["identity_key_candidates"] == [b"\x51" * 32]
    assert entry["encrypted_identity_key"] == b"\x10" * 32
    assert decrypt_locations._parse_epoch_seconds(  # type: ignore[attr-defined]
        int((base_now - 75) * 1000), base_now
    ) == pytest.approx(base_now - 75)


def test_pair_date_microseconds_normalization_and_future_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PairDate with microseconds normalizes while future drift is discarded."""

    base_now = 1_700_300_000.0

    normalized_pair_date = decrypt_locations.normalize_pair_date_value(
        (base_now - 240) * 1_000_000,
        now_wall=base_now,
    )
    assert normalized_pair_date == int(base_now - 240)

    future_pair_date = decrypt_locations.normalize_pair_date_value(
        (base_now + decrypt_locations.MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S + 50)
        * 1_000_000,
        now_wall=base_now,
    )

    assert future_pair_date is None

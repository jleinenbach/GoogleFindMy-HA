# tests/test_decrypt_locations.py
"""Regression tests for decrypting location payloads."""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
)

pytestmark = pytest.mark.asyncio

ALTITUDE_METERS = 1337
ACCURACY_METERS = 5.0


async def test_async_decrypt_location_response_locations_allows_future_owner_timestamp(
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

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == owner_timestamp
    assert entry["is_own_report"] is True
    assert entry["altitude"] == ALTITUDE_METERS
    assert entry["accuracy"] == ACCURACY_METERS


async def test_async_decrypt_location_response_locations_aligns_missing_network_timestamps(
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

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["last_seen"] == recent_timestamp
    assert entry["latitude"] == pytest.approx(40.0)
    assert entry["longitude"] == pytest.approx(-74.0)
    assert entry["is_own_report"] is True


async def test_async_decrypt_location_response_locations_returns_metadata_when_empty(
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

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
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


async def test_async_decrypt_location_response_unwraps_60_byte_eik(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression Test: Unwrap 60-byte EIKs so HKDF/location decryption never sees "Key length 60"."""

    base_now = 1_700_100_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    caplog.set_level(logging.WARNING)

    encrypted_eik = b"\x99" * decrypt_locations.EIK_GCM_TOTAL_LEN
    # Moto Tag/Chipolo responses wrap the 32-byte identity key inside a 60-byte envelope.
    unwrapped_eik = b"\x42" * 32

    location_proto = DeviceUpdate_pb2.Location()
    location_proto.latitude = int(37.42 * 1e7)
    location_proto.longitude = int(-122.084 * 1e7)
    location_proto.altitude = ALTITUDE_METERS
    location_bytes = location_proto.SerializeToString()

    identity_key_calls = {"count": 0}

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        identity_key_calls["count"] += 1
        return [encrypted_eik]

    unwrap_calls: dict[str, object] = {}
    offload_calls: dict[str, bytes] = {}

    async def fake_get_owner_key(*, cache):  # type: ignore[no-untyped-def]
        unwrap_calls["cache"] = cache
        return OwnerKeyInfo(key=b"\xAA" * 32, version=3)

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    def fake_decrypt(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:
        unwrap_calls["owner_key"] = owner_key
        unwrap_calls["encrypted"] = encrypted_identity_key
        return unwrapped_eik

    async def fake_offload_aes(
        identity_key: bytes, encrypted_location: bytes
    ) -> bytes:
        offload_calls["identity_key"] = identity_key
        offload_calls["encrypted_location"] = encrypted_location
        return location_bytes

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(decrypt_locations, "async_get_owner_key", fake_get_owner_key)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", fake_decrypt)
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload_aes)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = int(base_now - 30)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 15)
    registration.encryptedUserSecrets.encryptedIdentityKey = encrypted_eik

    reports = update.deviceMetadata.information.locationInformation.reports
    report_group = reports.recentLocationAndNetworkLocations
    report_group.recentLocationTimestamp.seconds = int(base_now - 10)
    recent = report_group.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = ACCURACY_METERS
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""
    encrypted_report.encryptedLocation = b"ciphertext"
    encrypted_report.isOwnReport = True

    cache = object()
    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=cache
    )

    assert len(result) == 1
    entry = result[0]
    assert entry.get("metadata_only") is not True
    assert entry["identity_key"] == unwrapped_eik
    assert entry["identity_key_candidates"] == [unwrapped_eik]
    assert entry["encrypted_identity_key"] == encrypted_eik
    assert entry["pair_date"] == int(base_now - 30)
    assert entry["secrets_creation_date"] == int(base_now - 15)
    assert entry["accuracy"] == ACCURACY_METERS
    assert entry["altitude"] == ALTITUDE_METERS
    assert offload_calls["identity_key"] == unwrapped_eik
    assert len(offload_calls["identity_key"]) == 32
    assert offload_calls["encrypted_location"] == b"ciphertext"
    assert unwrap_calls["cache"] is cache
    assert unwrap_calls["owner_key"] == b"\xAA" * 32
    assert unwrap_calls["encrypted"] == encrypted_eik
    assert identity_key_calls["count"] == 0
    assert "[DIAG-ALERT] Key length" not in caplog.text


async def test_async_decrypt_location_response_locations_normalizes_anchor_units(
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

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
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


async def test_async_decrypt_location_response_reads_registration_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration anchors propagate even when deviceTypeInformation is absent."""

    base_now = 1_700_400_000.0
    monkeypatch.setattr(decrypt_locations.time, "time", lambda: base_now)

    async def fake_identity_key(*_args: object, **_kwargs: object) -> list[bytes]:
        return [b"\x61" * 32]

    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x22" * 32

    registration.pairDate = int(base_now - 500)
    registration.encryptedUserSecrets.creationDate.seconds = int(base_now - 250)

    reports = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    reports.recentLocationTimestamp.seconds = int(base_now - 100)
    semantic = reports.recentLocation
    semantic.status = Common_pb2.Status.SEMANTIC
    semantic.semanticLocation.locationName = "semantic-anchor"

    result = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=object()
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["pair_date"] == int(base_now - 500)
    assert entry["secrets_creation_date"] == int(base_now - 250)
    assert entry["identity_key"] == b"\x61" * 32
    type_meta = entry.get("device_type_information")
    assert not type_meta
    registration_meta = entry.get("device_registration")
    assert isinstance(registration_meta, dict)
    assert registration_meta.get("pairDate") == int(base_now - 500)


async def test_pair_date_microseconds_normalization_and_future_rejection(
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

# tests/test_moto_tag_regressions.py
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import custom_components.googlefindmy.coordinator as coordinator_module
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.FMDNCrypto import eid_generator
from custom_components.googlefindmy.KeyBackup import cloud_key_decryptor
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2


@pytest.mark.asyncio
async def test_moto_tag_decryption_unwraps_and_injects_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60-byte Moto Tag keys should be unwrapped and metadata propagated."""

    base_now = 1_700_000_000
    creation_seconds = base_now - 123
    encrypted_identity_key = b"\x99" * decrypt_locations.EIK_GCM_TOTAL_LEN
    # FIX: decrypted_identity_key must be exactly 32 bytes for hex conversion
    decrypted_identity_key = b"DecryptedMotoTagIdentityKey!!!!!"  # 32 bytes

    location_proto = DeviceUpdate_pb2.Location()
    location_proto.latitude = int(40.0 * 1e7)
    location_proto.longitude = int(9.0 * 1e7)
    location_proto.altitude = 25
    location_bytes = location_proto.SerializeToString()

    async def fake_identity_key(*_args, **_kwargs) -> list[bytes]:
        return [encrypted_identity_key]

    async def fake_offload(*_args, **_kwargs) -> bytes:
        return location_bytes

    decrypt_mock = MagicMock(return_value=decrypted_identity_key)

    async def fake_unwrap(identity_key: bytes, *, cache: object) -> bytes:
        decrypt_mock(identity_key)
        return decrypted_identity_key

    monkeypatch.setattr(cloud_key_decryptor, "decrypt_eik", decrypt_mock)
    monkeypatch.setattr(
        decrypt_locations, "async_retrieve_identity_key", fake_identity_key
    )
    monkeypatch.setattr(
        decrypt_locations, "_unwrap_encrypted_identity_key", fake_unwrap
    )
    monkeypatch.setattr(decrypt_locations, "_offload_decrypt_aes", fake_offload)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.pairDate = base_now - 300
    registration.encryptedUserSecrets.creationDate.seconds = creation_seconds
    registration.encryptedUserSecrets.ownerKeyVersion = 3
    registration.encryptedUserSecrets.encryptedIdentityKey = encrypted_identity_key

    reports = update.deviceMetadata.information.locationInformation.reports
    report_group = reports.recentLocationAndNetworkLocations
    report_group.recentLocationTimestamp.seconds = base_now - 60
    recent = report_group.recentLocation
    recent.status = Common_pb2.Status.LAST_KNOWN
    recent.geoLocation.accuracy = 3.0
    encrypted_report = recent.geoLocation.encryptedReport
    encrypted_report.publicKeyRandom = b""
    encrypted_report.encryptedLocation = b"payload"

    results = await decrypt_locations.async_decrypt_location_response_locations(
        update, cache=SimpleNamespace()
    )

    assert decrypt_mock.called
    assert results
    payload = results[0]
    # FIX: identity_key is now returned as hex string
    assert payload["identity_key"] == decrypted_identity_key.hex()
    assert payload.get("secrets_creation_date") is not None
    assert payload["secrets_creation_date"] == creation_seconds


def test_persistence_writes_moto_tag_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """Updated anchors should persist identity metadata to _device_location_data.

    Note: Previously this tested writing to DeviceRegistry custom_fields, but that
    parameter does not exist in Home Assistant's DeviceRegistry API. The fix stores
    EID metadata in the in-memory _device_location_data cache instead, and triggers
    an EID resolver refresh when identity_key is present.
    """
    # Track EID resolver refresh calls
    refresh_called = False
    created_tasks: list[object] = []

    async def mock_refresh() -> None:
        nonlocal refresh_called
        refresh_called = True

    def mock_create_task(coro: object) -> MagicMock:
        created_tasks.append(coro)
        # Close the coroutine to avoid warnings
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        return MagicMock()

    # Create a mock EID resolver with async_refresh
    mock_eid_resolver = MagicMock()
    mock_eid_resolver.async_refresh = mock_refresh

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {}

    # Set up hass with eid_resolver in hass.data[DOMAIN][DATA_EID_RESOLVER]
    # (the correct location for the domain-level singleton)
    hass_data: dict[str, Any] = {DOMAIN: {DATA_EID_RESOLVER: mock_eid_resolver}}
    coordinator.hass = SimpleNamespace(async_create_task=mock_create_task, data=hass_data)

    payload = {
        "identity_key": b"\xaa" * 32,
        "secrets_creation_date": 1_700_000_123,
    }

    coordinator._persist_anchor_metadata(
        "dev-anchor", payload, clear_metadata_only=False
    )

    # Verify data is stored in _device_location_data (the correct location)
    assert "dev-anchor" in coordinator._device_location_data
    cached_data = coordinator._device_location_data["dev-anchor"]
    assert cached_data["identity_key"] == b"\xaa" * 32
    assert cached_data["secrets_creation_date"] == 1_700_000_123

    # Verify EID resolver refresh was triggered (task was created)
    assert len(created_tasks) == 1, "EID resolver refresh should be triggered"


def test_little_endian_generation_registers_variants() -> None:
    """Little endian EIDs should differ from big endian ones and be cached."""

    identity_key = bytes.fromhex(
        "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
    )
    timestamp = 1_700_000_000

    be_eid = eid_generator.generate_eid_variant(
        identity_key, timestamp, eid_generator.EidVariant.MODERN_P256_X32_BE
    )
    le_eid = eid_generator.generate_eid_variant(
        identity_key, timestamp, eid_generator.EidVariant.MODERN_P256_X32_LE_SCALAR
    )

    assert be_eid != le_eid
    assert len(be_eid) == len(le_eid)

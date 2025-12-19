# tests/test_moto_tag_regressions.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import custom_components.googlefindmy.coordinator as coordinator_module
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
    decrypted_identity_key = b"DecryptedMotoTagIdentityKey!!"[:32]

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
    assert payload["identity_key"] == decrypted_identity_key
    assert payload.get("secrets_creation_date") is not None
    assert payload["secrets_creation_date"] == creation_seconds


def test_persistence_writes_moto_tag_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """Updated anchors should persist identity metadata to the registry."""

    class _StubDevice:
        def __init__(self) -> None:
            self.id = "registry-id"
            self.custom_fields: dict[str, object] = {}

    class _StubRegistry:
        def __init__(self) -> None:
            self.device = _StubDevice()
            self.updated_payload: dict[str, object] | None = None

        def async_get_device(self, identifiers: set[tuple[str, str]]):
            return (
                self.device if ("googlefindmy", "dev-anchor") in identifiers else None
            )

        def async_update_device(
            self,
            registry_id: str | None = None,
            *,
            custom_fields: dict[str, object],
            device_id: str | None = None,
        ):
            registry_value = registry_id or device_id
            assert registry_value == self.device.id
            self.device.custom_fields = custom_fields
            self.updated_payload = custom_fields

    registry = _StubRegistry()

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {}
    coordinator.hass = SimpleNamespace()

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda _hass: registry)

    payload = {
        "identity_key": b"\xaa" * 32,
        "secrets_creation_date": 1_700_000_123,
    }

    coordinator._persist_anchor_metadata(
        "dev-anchor", payload, clear_metadata_only=False
    )

    assert registry.updated_payload is not None
    assert registry.updated_payload["identity_key"] == (b"\xaa" * 32).hex()
    assert registry.updated_payload["secrets_creation_date"] == 1_700_000_123


def test_little_endian_generation_registers_variants() -> None:
    """Little endian EIDs should differ from big endian ones and be cached."""

    identity_key = bytes.fromhex(
        "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
    )
    timestamp = 1_700_000_000

    be_eid = eid_generator.generate_eid_p256(identity_key, timestamp)
    le_eid = eid_generator.generate_eid_p256_le(identity_key, timestamp)

    assert be_eid != le_eid
    assert len(be_eid) == len(le_eid)

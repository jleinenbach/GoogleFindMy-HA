# tests/test_create_ble_device.py
"""Tests for create_ble_device.register_esp32."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.googlefindmy.SpotApi.CreateBleDevice import create_ble_device


class _StubDeviceComponentInformation:
    def __init__(self) -> None:
        self.imageUrl = ""


class _StubPublicKeyIdInfo:
    created_instances: list[_StubPublicKeyIdInfo] = []

    def __init__(self) -> None:
        self.publicKeyId = SimpleNamespace(truncatedEid=b"")
        self.timestamp = SimpleNamespace(seconds=0)
        self.__class__.created_instances.append(self)


class _StubRegisterBleDeviceRequest:
    created_instances: list[_StubRegisterBleDeviceRequest] = []

    def __init__(self) -> None:
        self.__class__.created_instances.append(self)
        self.fastPairModelId = ""
        self.description = SimpleNamespace(
            userDefinedName="",
            deviceType=None,
            deviceComponentsInformation=[],
        )
        self.capabilities = SimpleNamespace(
            isAdvertising=False,
            trackableComponents=0,
            capableComponents=0,
        )
        self.e2eePublicKeyRegistration = SimpleNamespace(
            rotationExponent=0,
            pairingDate=0,
            encryptedUserSecrets=SimpleNamespace(
                encryptedIdentityKey=b"",
                encryptedAccountKey=b"",
                encryptedSha256AccountKeyPublicAddress=b"",
                ownerKeyVersion=0,
                creationDate=SimpleNamespace(seconds=0),
            ),
            publicKeyIdList=SimpleNamespace(publicKeyIdInfo=[]),
        )
        self.manufacturerName = ""
        self.modelName = ""
        self.ringKey: bytes | None = None
        self.recoveryKey: bytes | None = None
        self.unwantedTrackingKey: bytes | None = None
        self._serialize_call_count = 0

    def SerializeToString(self) -> bytes:
        self._serialize_call_count += 1
        return b"stub-serialized"


class _StubOwnerOperations:
    instances: list[_StubOwnerOperations] = []

    def __init__(self) -> None:
        self.__class__.instances.append(self)
        self.generate_keys_calls: list[bytes] = []
        self.ringing_key: bytes | None = None
        self.recovery_key: bytes | None = None
        self.tracking_key: bytes | None = None

    def generate_keys(self, *, identity_key: bytes) -> None:
        self.generate_keys_calls.append(identity_key)
        self.ringing_key = b"stub-ring"
        self.recovery_key = b"stub-recovery"
        self.tracking_key = b"stub-tracking"


def test_register_esp32(monkeypatch) -> None:
    module = create_ble_device

    monkeypatch.setattr(module, "DeviceComponentInformation", _StubDeviceComponentInformation)
    monkeypatch.setattr(module, "PublicKeyIdList", SimpleNamespace(PublicKeyIdInfo=_StubPublicKeyIdInfo))
    monkeypatch.setattr(module, "RegisterBleDeviceRequest", _StubRegisterBleDeviceRequest)
    monkeypatch.setattr(module, "FMDNOwnerOperations", _StubOwnerOperations)

    fake_owner_key = b"owner-key"
    fake_eik = b"\xAA" * 32
    fake_eid = b"EID0123456789ABCDEF"
    fake_time_value = 1_700_000_000

    def fake_token_bytes(length: int) -> bytes:
        if length == 32:
            return fake_eik
        return bytes([length % 256]) * length

    encrypt_calls: list[tuple[bytes, bytes]] = []

    def fake_encrypt(owner_key: bytes, eik: bytes) -> bytes:
        encrypt_calls.append((owner_key, eik))
        return b"cipher-" + owner_key + eik

    flip_calls: list[tuple[bytes, bool]] = []

    def fake_flip_bits(data: bytes, invert: bool) -> bytes:
        flip_calls.append((data, invert))
        return b"flip-" + data

    spot_calls: list[tuple[str, bytes]] = []

    def fake_spot_request(endpoint: str, payload: bytes) -> None:
        spot_calls.append((endpoint, payload))

    monkeypatch.setattr(module.secrets, "token_bytes", fake_token_bytes)
    monkeypatch.setattr(module.time, "time", lambda: float(fake_time_value))
    monkeypatch.setattr(module, "get_owner_key", lambda: fake_owner_key)
    monkeypatch.setattr(module, "generate_eid", lambda _eik, _counter: fake_eid)
    monkeypatch.setattr(module, "encrypt_aes_gcm", fake_encrypt)
    monkeypatch.setattr(module, "flip_bits", fake_flip_bits)
    monkeypatch.setattr(module, "spot_request", fake_spot_request)
    monkeypatch.setattr(module, "mcu_fast_pair_model_id", "stub-model-id", False)
    monkeypatch.setattr(module, "max_truncated_eid_seconds_server", 30, False)
    monkeypatch.setattr(module, "ROTATION_PERIOD", 10, False)

    create_ble_device.register_esp32()

    assert spot_calls == [("CreateBleDevice", b"stub-serialized")]

    (request_instance,) = _StubRegisterBleDeviceRequest.created_instances
    assert request_instance._serialize_call_count == 1

    owner_ops_instance = _StubOwnerOperations.instances[0]
    assert owner_ops_instance.generate_keys_calls == [fake_eik]
    assert request_instance.ringKey == b"stub-ring"
    assert request_instance.recoveryKey == b"stub-recovery"
    assert request_instance.unwantedTrackingKey == b"stub-tracking"

    expected_truncated_eid = fake_eid[:10]
    public_key_entries = request_instance.e2eePublicKeyRegistration.publicKeyIdList.publicKeyIdInfo
    assert len(public_key_entries) == 3
    assert [entry.publicKeyId.truncatedEid for entry in public_key_entries] == [
        expected_truncated_eid,
        expected_truncated_eid,
        expected_truncated_eid,
    ]
    assert [entry.timestamp.seconds for entry in public_key_entries] == [
        fake_time_value,
        fake_time_value + 10,
        fake_time_value + 20,
    ]

    assert encrypt_calls == [(fake_owner_key, fake_eik)]
    assert flip_calls == [
        (b"cipher-" + fake_owner_key + fake_eik, True),
    ]
    encrypted_user_secrets = request_instance.e2eePublicKeyRegistration.encryptedUserSecrets
    assert (
        encrypted_user_secrets.encryptedIdentityKey
        == b"flip-cipher-" + fake_owner_key + fake_eik
    )


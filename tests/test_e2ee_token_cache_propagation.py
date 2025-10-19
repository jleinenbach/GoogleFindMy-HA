# tests/test_e2ee_token_cache_propagation.py
"""Regression tests for TokenCache propagation in E2EE helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_async_retrieve_identity_key_threads_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure owner-key retrieval receives the entry TokenCache."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    owner_calls: dict[str, object] = {}

    async def fake_async_get_owner_key(*, cache, **kwargs):  # type: ignore[no-untyped-def]
        owner_calls["owner_cache"] = cache
        return b"\x01" * 32

    async def fake_async_get_eid_info(*, cache):  # type: ignore[no-untyped-def]
        owner_calls["eid_cache"] = cache
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(ownerKeyVersion=1)
        )

    monkeypatch.setattr(
        decrypt_locations, "async_get_owner_key", fake_async_get_owner_key
    )
    monkeypatch.setattr(
        decrypt_locations, "async_get_eid_info", fake_async_get_eid_info
    )
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", lambda *_: b"\x02" * 32)
    monkeypatch.setattr(decrypt_locations, "flip_bits", lambda data, _: data)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda _: False)

    class DummyEncrypted:
        encryptedIdentityKey = b"payload"
        ownerKeyVersion = 1

    class DummyRegistration:
        encryptedUserSecrets = DummyEncrypted()
        fastPairModelId = None

    cache = object()
    result = asyncio.run(
        decrypt_locations.async_retrieve_identity_key(
            DummyRegistration(), cache=cache
        )
    )

    assert result == b"\x02" * 32
    assert owner_calls["owner_cache"] is cache
    # Success path does not consult async_get_eid_info; ensure no unexpected call
    assert "eid_cache" not in owner_calls


def test_async_retrieve_identity_key_error_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even on failure the TokenCache must be propagated to diagnostics."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    caches: dict[str, object] = {}

    async def fake_async_get_owner_key(*, cache, **kwargs):  # type: ignore[no-untyped-def]
        caches["owner_cache"] = cache
        return b"\x01" * 32

    async def fake_async_get_eid_info(*, cache):  # type: ignore[no-untyped-def]
        caches["eid_cache"] = cache
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(ownerKeyVersion=2)
        )

    monkeypatch.setattr(
        decrypt_locations, "async_get_owner_key", fake_async_get_owner_key
    )
    monkeypatch.setattr(
        decrypt_locations, "async_get_eid_info", fake_async_get_eid_info
    )

    def _raise_decrypt(*_: object) -> bytes:
        raise ValueError("boom")

    monkeypatch.setattr(decrypt_locations, "decrypt_eik", _raise_decrypt)
    monkeypatch.setattr(decrypt_locations, "flip_bits", lambda data, _: data)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda _: False)

    class DummyEncrypted:
        encryptedIdentityKey = b"payload"
        ownerKeyVersion = 1

    class DummyRegistration:
        encryptedUserSecrets = DummyEncrypted()
        fastPairModelId = None

    cache = object()
    with pytest.raises(decrypt_locations.StaleOwnerKeyError):
        asyncio.run(
            decrypt_locations.async_retrieve_identity_key(
                DummyRegistration(), cache=cache
            )
        )

    assert caches["owner_cache"] is cache
    assert caches["eid_cache"] is cache


def test_async_get_eid_info_threads_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`async_get_eid_info` must forward the cache to the Spot helper."""

    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
        get_eid_info_request as module,
    )

    captured: dict[str, object] = {}

    async def fake_async_spot_request(scope, payload, *, cache):  # type: ignore[no-untyped-def]
        captured["scope"] = scope
        captured["payload"] = payload
        captured["cache"] = cache
        return b"response"

    class DummyResponse:
        def __init__(self) -> None:
            self.payload: bytes | None = None

        def ParseFromString(self, data: bytes) -> None:
            self.payload = data

    monkeypatch.setattr(
        "custom_components.googlefindmy.SpotApi.spot_request.async_spot_request",
        fake_async_spot_request,
    )
    monkeypatch.setattr(module, "_build_request_bytes", lambda: b"request")
    monkeypatch.setattr(
        module.DeviceUpdate_pb2,
        "GetEidInfoForE2eeDevicesResponse",
        DummyResponse,
    )

    cache = object()
    response = asyncio.run(module.async_get_eid_info(cache=cache))

    assert captured["scope"] == "GetEidInfoForE2eeDevices"
    assert captured["payload"] == b"request"
    assert captured["cache"] is cache
    assert isinstance(response, DummyResponse)
    assert response.payload == b"response"


def test_sync_decrypt_location_response_forwards_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synchronous facade must forward the TokenCache to the async helper."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    captured: dict[str, object] = {}

    async def fake_async_decrypt(device_update, *, cache):  # type: ignore[no-untyped-def]
        captured["device_update"] = device_update
        captured["cache"] = cache
        return ["ok"]

    monkeypatch.setattr(
        decrypt_locations,
        "async_decrypt_location_response_locations",
        fake_async_decrypt,
    )

    cache = object()
    device_update = object()

    result = decrypt_locations.decrypt_location_response_locations(
        device_update, cache=cache  # type: ignore[arg-type]
    )

    assert result == ["ok"]
    assert captured["device_update"] is device_update
    assert captured["cache"] is cache


def test_fcm_background_decode_uses_entry_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background FCM decoding must supply the entry TokenCache."""

    from custom_components.googlefindmy.Auth import fcm_receiver_ha

    receiver = fcm_receiver_ha.FcmReceiverHA()
    cache = object()
    receiver._entry_caches["entry"] = cache

    captured: dict[str, object] = {}

    def fake_parse(hex_string: str) -> str:
        captured["hex"] = hex_string
        return "parsed"

    def fake_sync_decrypt(device_update, *, cache):  # type: ignore[no-untyped-def]
        captured["device_update"] = device_update
        captured["cache"] = cache
        return [{"latitude": 1.0}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.ProtoDecoders.decoder.parse_device_update_protobuf",
        fake_parse,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations.decrypt_location_response_locations",
        fake_sync_decrypt,
    )

    result = receiver._decode_background_location("entry", "payload")

    assert result == {"latitude": 1.0}
    assert captured["hex"] == "payload"
    assert captured["device_update"] == "parsed"
    assert captured["cache"] is cache

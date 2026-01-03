from __future__ import annotations

import asyncio

import pytest

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.FMDNCrypto._lazy_crypto import (
    get_aesgcm_class,
    get_invalid_tag_exception,
)
from tests.helpers import DummyCache


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_refreshes_owner_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = DummyCache()
    calls: list[bool] = []
    decrypt_inputs: list[bytes] = []

    async def fake_async_get_owner_key(*, cache: object, force_refresh: bool = False, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(force_refresh)
        version = 2 if force_refresh else 1
        key = bytes([version]) * 32
        return eid_resolver.OwnerKeyInfo(key=key, version=version)

    def fake_decrypt_eik(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:  # noqa: ARG001
        decrypt_inputs.append(owner_key)
        return b"decoded"

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", fake_decrypt_eik)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)

    identity = DeviceIdentity(
        registry_id="reg-1",
        canonical_id="can-1",
        identity_key=None,
        encrypted_identity_key=b"\x01" * 32,
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-1",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == b"decoded"
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["key_source"] == "owner"
    assert calls == [False, True]
    assert decrypt_inputs == [bytes([2]) * 32]


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_unwraps_aes_gcm_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DummyCache()
    key = b"\x02" * 32
    nonce = b"\x00" * 12
    plaintext = b"\x99" * 32
    aad = b"reg-wrap"
    AESGCM = get_aesgcm_class()
    InvalidTag = get_invalid_tag_exception()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = nonce + ciphertext

    async def fake_async_get_owner_key(*, cache: object, **kwargs):  # type: ignore[no-untyped-def]
        return eid_resolver.OwnerKeyInfo(key=key, version=None)

    async def fake_async_get_shared_key(*, cache: object, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("shared key unavailable")

    def fake_decrypt_eik(owner_key: bytes, encrypted_identity_key: bytes) -> bytes:  # noqa: ARG001
        raise InvalidTag("wrapped")

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "async_get_shared_key", fake_async_get_shared_key)
    monkeypatch.setattr(eid_resolver, "decrypt_eik", fake_decrypt_eik)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)
    monkeypatch.setattr(eid_resolver, "flip_bits", lambda data, _: data)

    identity = DeviceIdentity(
        registry_id="reg-wrap",
        canonical_id="can-wrap",
        identity_key=None,
        encrypted_identity_key=envelope,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-wrap",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "aesgcm_envelope"
    assert result.metadata["aad_label"] == "registry_id"
    assert result.metadata["key_source"] == "owner"

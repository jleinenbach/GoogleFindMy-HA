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
from tests.helpers.owner_key_vault_fixtures import (
    OWNER_KEY_VERSIONS_FIELD,
    make_vault_world,
)


@pytest.mark.asyncio
async def test_try_decrypt_identity_key_refreshes_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = DummyCache()
    calls: list[bool] = []
    decrypt_inputs: list[bytes] = []

    async def fake_async_get_owner_key(
        *, cache: object, force_refresh: bool = False, **kwargs
    ):  # type: ignore[no-untyped-def]
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="owner-key vault fallback not implemented",
)
@pytest.mark.asyncio
async def test_try_decrypt_identity_key_gap_uses_vault_entry_without_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #246: tracker version 2, Spot serves 1, vault entry 2 in the bundle.

    The resolver must take the owner key of version 2 from the bundle field
    instead of forcing a Spot refresh. Spot keeps serving version 1 even on
    ``force_refresh``, which is the situation of the report. The shared key sits
    in the entry cache, so the real retrieval returns it through any binding.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    cache = DummyCache()
    world.seed_entry_cache(cache.values, world.bundle_field)
    assert OWNER_KEY_VERSIONS_FIELD in cache.values
    calls: list[bool] = []

    async def fake_async_get_owner_key(
        *,
        cache: object,
        username: str | None = None,
        eid_info_getter: object | None = None,
        shared_key_getter: object | None = None,
        force_refresh: bool = False,
    ) -> eid_resolver.OwnerKeyInfo:
        calls.append(force_refresh)
        return eid_resolver.OwnerKeyInfo(key=world.owner_keys[1], version=1)

    monkeypatch.setattr(eid_resolver, "async_get_owner_key", fake_async_get_owner_key)
    monkeypatch.setattr(eid_resolver, "is_mcu_tracker", lambda **_: False)

    identity = DeviceIdentity(
        registry_id="reg-gap",
        canonical_id="can-gap",
        identity_key=None,
        encrypted_identity_key=world.encrypted_eik(2),
        owner_key_version=2,
        device_type=None,
        config_entry_id="entry-gap",
    )

    resolver = object.__new__(eid_resolver.GoogleFindMyEIDResolver)
    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result is not None
    assert result.key == world.eiks[2]
    assert calls == [False]

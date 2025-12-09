from __future__ import annotations

import asyncio

import pytest

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.coordinator import DeviceIdentity
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

    assert result == b"decoded"
    assert calls == [False, True]
    assert decrypt_inputs == [bytes([2]) * 32]

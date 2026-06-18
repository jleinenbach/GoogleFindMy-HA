# tests/test_decrypt_owner_key_discriminator.py
"""Discriminator for stale-vs-missing shared key in owner-key decryption.

When ``async_get_owner_key`` fails, the root cause (wrong shared key vs. missing
shared key vs. generic failure) is otherwise collapsed into a single opaque
message. These tests pin ``_classify_owner_key_failure``, which restores the
discriminator as a typed ``DecryptionError`` subclass, and the Liskov property
that keeps every existing ``except DecryptionError`` handler working.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    SharedKeyMismatchError,
    SharedKeyMissingError,
    StaleOwnerKeyError,
    _classify_owner_key_failure,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2


def test_liskov_subclasses_are_decryption_errors() -> None:
    """All specific shared-key errors stay DecryptionError subclasses so existing
    ``except DecryptionError`` handlers keep catching them (no silent breakage)."""
    assert issubclass(SharedKeyMismatchError, DecryptionError)
    assert issubclass(SharedKeyMissingError, DecryptionError)
    assert issubclass(StaleOwnerKeyError, DecryptionError)


def test_t2_invalid_tag_maps_to_shared_key_mismatch() -> None:
    """InvalidTag (AES-GCM auth failure) => wrong/stale shared key."""
    result = _classify_owner_key_failure(InvalidTag(), context="initial lookup")
    assert isinstance(result, SharedKeyMismatchError)
    assert "InvalidTag" in str(result)
    assert "initial lookup" in str(result)


def test_t2_missing_runtime_maps_to_shared_key_missing() -> None:
    """RuntimeError mentioning 'missing or empty' => incomplete bundle."""
    exc = RuntimeError("Shared key is missing or empty; cannot decrypt owner key")
    result = _classify_owner_key_failure(exc, context="initial lookup")
    assert isinstance(result, SharedKeyMissingError)


def test_t2_generic_runtime_maps_to_plain_decryption_error() -> None:
    """Any other RuntimeError => generic DecryptionError (still escalates, but is
    not mis-labelled as a specific shared-key cause)."""
    result = _classify_owner_key_failure(RuntimeError("boom"), context="x")
    assert type(result) is DecryptionError


@pytest.mark.asyncio
async def test_owner_key_invalid_tag_propagates_as_shared_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: an InvalidTag from the owner-key lookup surfaces from
    async_retrieve_identity_key as SharedKeyMismatchError (the path that used to
    collapse into an opaque, swallowed DecryptionError)."""

    async def _boom(**_kwargs: object) -> object:
        raise InvalidTag()

    monkeypatch.setattr(_dl, "async_get_owner_key", _boom)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(registration, cache=object())


@pytest.mark.asyncio
async def test_owner_key_forced_refresh_invalid_tag_maps_to_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: when a newer owner-key version triggers a forced refresh and
    that refresh also fails with InvalidTag, the failure still surfaces as
    SharedKeyMismatchError (the forced-refresh wrap path)."""
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
        OwnerKeyInfo,
    )

    async def _owner(*, cache: object, force_refresh: bool = False, **_kw: object) -> object:
        if force_refresh:
            raise InvalidTag()
        return OwnerKeyInfo(key=b"\x00" * 32, version=1)

    monkeypatch.setattr(_dl, "async_get_owner_key", _owner)

    update = DeviceUpdate_pb2.DeviceUpdate()
    registration = update.deviceMetadata.information.deviceRegistration
    registration.encryptedUserSecrets.encryptedIdentityKey = b"\x00" * 60
    registration.encryptedUserSecrets.ownerKeyVersion = 5  # > cached version 1

    with pytest.raises(SharedKeyMismatchError):
        await _dl.async_retrieve_identity_key(registration, cache=object())

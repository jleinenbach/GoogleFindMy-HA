# tests/test_stale_owner_key_wording.py
"""Wording of the stale-owner-key failure in ``async_retrieve_identity_key``.

When a tracker's EIK was sealed with an owner-key version older than the current
account owner-key version and no retry succeeds, the decoder gives up with an ERROR
log and raises ``StaleOwnerKeyError``. The message must make clear that the fix is
local to this one tracker (re-pair it) and that no account re-authentication or new
``secrets.json`` is required, so the user does not needlessly reset the whole
integration.

The mismatch path is reached deterministically: owner/shared key lookups are stubbed,
the EIK decrypt is forced to fail (empty candidate list), and the EID metadata reports
a newer owner-key version than the tracker carries, with ``_retry=False`` to land on
the terminal error branch.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    StaleOwnerKeyError,
    async_retrieve_identity_key,
)


def _make_device_registration(owner_key_version: int) -> SimpleNamespace:
    """Build a registration whose EIK carries the given (stale) owner-key version."""
    encrypted_user_secrets = SimpleNamespace(
        encryptedIdentityKey=b"\x00" * 60,
        ownerKeyVersion=owner_key_version,
    )
    return SimpleNamespace(encryptedUserSecrets=encrypted_user_secrets)


@pytest.mark.asyncio
async def test_stale_owner_key_error_points_to_single_tracker_repair(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The terminal stale-owner-key path names the single-tracker remedy.

    The ERROR message must steer the user to re-pair only this tracker and explicitly
    rule out an account re-auth / new secrets bundle; ``StaleOwnerKeyError`` is still
    raised so the caller behaviour is unchanged.
    """
    tracker_version, current_version = 1, 2

    async def _fake_owner_key(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(key=b"\x01" * 32, version=tracker_version)

    async def _fake_shared_key(*_: object, **__: object) -> None:
        return None

    async def _fake_eid_info(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(
                ownerKeyVersion=current_version
            )
        )

    def _fail_decrypt(*_: object, **__: object) -> bytes:
        raise InvalidTag()

    monkeypatch.setattr(decrypt_locations, "async_get_owner_key", _fake_owner_key)
    monkeypatch.setattr(decrypt_locations, "async_get_shared_key", _fake_shared_key)
    monkeypatch.setattr(decrypt_locations, "async_get_eid_info", _fake_eid_info)
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", _fail_decrypt)
    monkeypatch.setattr(
        decrypt_locations,
        "_try_unwrap_aesgcm_envelope",
        lambda *_, **__: None,
    )

    cache = SimpleNamespace(get=_fake_shared_key, set=_fake_shared_key)
    device_registration = _make_device_registration(tracker_version)

    with caplog.at_level(logging.ERROR, logger="custom_components.googlefindmy"):
        with pytest.raises(StaleOwnerKeyError) as exc_info:
            await async_retrieve_identity_key(
                device_registration,
                cache=cache,  # type: ignore[arg-type]
                device_id="canonic-under-test",
                _retry=False,
            )

    error_messages = [
        rec.message for rec in caplog.records if rec.levelno == logging.ERROR
    ]
    assert len(error_messages) == 1
    sharpened = error_messages[0].lower()
    assert "single tracker" in sharpened
    assert "re-authentication" in sharpened
    assert "secrets.json" in sharpened

    assert "single tracker" in str(exc_info.value).lower()

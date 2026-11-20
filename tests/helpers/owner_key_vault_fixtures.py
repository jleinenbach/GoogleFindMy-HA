# tests/helpers/owner_key_vault_fixtures.py
"""Synthetic owner-key vault material for the owner-key version gap tests.

The factory builds a self-consistent "vault world" for issue #246 (a tracker
reports a newer ``ownerKeyVersion`` than the owner key Spot serves):

* one random 32-byte owner key per version,
* each owner key wrapped with one random shared key in the envelope that
  ``KeyBackup.cloud_key_decryptor.decrypt_owner_key`` expects
  (``IV(12) || CIPHERTEXT || TAG``, AES-GCM, no AAD),
* the bundle field ``owner_key_versions`` in the planned shape (decimal string
  version -> ``{"version", "epoch", "domain", "key"}`` with ``key`` holding the
  lowercase hex of the wrapped blob),
* one random 32-byte EIK per version, wrapped with the owner key of that
  version in the 60-byte AES-GCM form accepted by ``decrypt_eik``.

Expected values in tests are always the plaintexts fed in here, never an output
of the code under test. All key material comes from ``os.urandom``; there are no
constant or real keys. ``all_secret_strings`` lists the textual forms of every
secret so hygiene tests can assert that no log or console output contains a
16-character window of any of them.
"""

from __future__ import annotations

import base64
import os
from typing import Any, NamedTuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "GCM_IV_LEN",
    "KEY_LEN",
    "VAULT_DOMAIN",
    "VaultWorld",
    "make_vault_world",
    "wrap_aes_gcm",
]

GCM_IV_LEN = 12
KEY_LEN = 32
VAULT_DOMAIN = "finder_hw"
_EPOCH_BASE = 1_700_000_000


def wrap_aes_gcm(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` as ``IV(12) || CIPHERTEXT || TAG`` with a random IV."""
    iv = os.urandom(GCM_IV_LEN)
    return iv + AESGCM(key).encrypt(iv, plaintext, None)


def _text_forms(secret: bytes) -> tuple[str, ...]:
    """Return the textual renderings under which ``secret`` could leak."""
    standard = base64.b64encode(secret).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(secret).decode("ascii")
    return (
        secret.hex(),
        secret.hex().upper(),
        standard,
        standard.rstrip("="),
        urlsafe,
        urlsafe.rstrip("="),
        repr(secret)[2:-1],
    )


class VaultWorld(NamedTuple):
    """Self-consistent synthetic key material for one account."""

    shared_key: bytes
    owner_keys: dict[int, bytes]
    wrapped_owner_keys: dict[int, bytes]
    eiks: dict[int, bytes]
    encrypted_eiks: dict[int, bytes]
    bundle_field: dict[str, dict[str, Any]]
    spot_version: int

    def encrypted_eik(self, version: int) -> bytes:
        """Return the EIK blob encrypted with the owner key of ``version``."""
        return self.encrypted_eiks[version]

    def tracker_registration(self, version: int) -> Any:
        """Return a ``DeviceRegistration`` carrying ``ownerKeyVersion`` ``version``."""
        from custom_components.googlefindmy.ProtoDecoders import (  # noqa: PLC0415
            DeviceUpdate_pb2,
        )

        update = DeviceUpdate_pb2.DeviceUpdate()
        registration = update.deviceMetadata.information.deviceRegistration
        secrets_msg = registration.encryptedUserSecrets
        secrets_msg.encryptedIdentityKey = self.encrypted_eik(version)
        secrets_msg.ownerKeyVersion = version
        return registration

    def all_secret_strings(self) -> tuple[str, ...]:
        """Return hex, Base64 and ``repr`` forms of every secret in this world.

        Covered secrets: the shared key, every owner key, every wrapped owner
        key blob and every EIK. The ``repr`` form is ``repr(k)[2:-1]`` so that a
        leak through ``%r`` or ``repr(bytes)`` is caught as well.
        """
        secrets: list[bytes] = [
            self.shared_key,
            *self.owner_keys.values(),
            *self.wrapped_owner_keys.values(),
            *self.eiks.values(),
        ]
        forms: list[str] = []
        for secret in secrets:
            for form in _text_forms(secret):
                if form not in forms:
                    forms.append(form)
        return tuple(forms)


def make_vault_world(
    versions: tuple[int, ...] = (1, 2),
    spot_version: int = 1,
) -> VaultWorld:
    """Build random owner keys, vault blobs and EIK blobs for ``versions``.

    ``spot_version`` names the owner-key version a Spot double should serve; it
    must be one of ``versions`` so the double can return a matching key. The
    bundle field holds an entry for every version in ``versions``; tests that
    need a missing entry remove it from ``bundle_field`` themselves.
    """
    if not versions:
        raise ValueError("versions must not be empty")
    if spot_version not in versions:
        raise ValueError("spot_version must be one of versions")

    shared_key = os.urandom(KEY_LEN)
    owner_keys: dict[int, bytes] = {}
    wrapped_owner_keys: dict[int, bytes] = {}
    eiks: dict[int, bytes] = {}
    encrypted_eiks: dict[int, bytes] = {}
    bundle_field: dict[str, dict[str, Any]] = {}
    for version in versions:
        owner_key = os.urandom(KEY_LEN)
        wrapped = wrap_aes_gcm(shared_key, owner_key)
        eik = os.urandom(KEY_LEN)
        owner_keys[version] = owner_key
        wrapped_owner_keys[version] = wrapped
        eiks[version] = eik
        encrypted_eiks[version] = wrap_aes_gcm(owner_key, eik)
        bundle_field[str(version)] = {
            "version": version,
            "epoch": _EPOCH_BASE + version,
            "domain": VAULT_DOMAIN,
            "key": wrapped.hex(),
        }

    return VaultWorld(
        shared_key=shared_key,
        owner_keys=owner_keys,
        wrapped_owner_keys=wrapped_owner_keys,
        eiks=eiks,
        encrypted_eiks=encrypted_eiks,
        bundle_field=bundle_field,
        spot_version=spot_version,
    )

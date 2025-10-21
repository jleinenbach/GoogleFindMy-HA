# custom_components/googlefindmy/KeyBackup/cloud_key_decryptor.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Key backup / cryptographic helpers for Google Find My Device.

This module provides a small set of cryptographic utilities that are used to
derive keys (HKDF-SHA256), perform authenticated encryption/decryption with
AES-GCM, and decrypt a few specific key blobs used by the integration
(recovery/application/security-domain/shared/owner/account keys, and EIK).

Design goals (HA Best Practices / Platinum Quality targets):
- Keep the Home Assistant event loop responsive. The core primitives here are
  synchronous by design; higher layers may offload them via `asyncio.to_thread`
  if needed.
- Strong, explicit typing and clear docstrings (Args/Returns/Raises).
- Defensive input validation (IV lengths, ciphertext framing, block-size checks).
- No logging/printing of secrets at import time.
- Backwards-compatible behavior: no public API changes versus previous versions.

NOTE: Do not import this module just to run the sample block below. The
`if __name__ == "__main__":` section is only for local/manual testing.
"""

from __future__ import annotations

import secrets
from binascii import unhexlify

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.KeyBackup.lskf_hasher import (
    ascii_to_bytes,
    get_lskf_hash,
)
from custom_components.googlefindmy.example_data_provider import get_example_data

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = b"\x02\x00"
SECUREBOX = b"SECUREBOX"
SHARED_HKDF_AES_GCM = b"SHARED HKDF-SHA-256 AES-128-GCM"
P256_HKDF_AES_GCM = b"P256 HKDF-SHA-256 AES-128-GCM"

# AES-GCM standard IV size (bytes). CBC IV size is always 16 bytes for AES.
GCM_IV_LEN_DEFAULT = 12
CBC_IV_LEN = 16


# ---------------------------------------------------------------------------
# HKDF (SHA-256)
# ---------------------------------------------------------------------------
def derive_key_using_hkdf_sha256(input_key: bytes, salt: bytes, info: bytes) -> bytes:
    """Derive a 16-byte key using HKDF-SHA256.

    Args:
        input_key: Input keying material (IKM).
        salt: HKDF salt (non-secret).
        info: HKDF context/application info.

    Returns:
        A 16-byte derived key (suitable for AES-128-GCM).

    Raises:
        ValueError: If input types are invalid (implicitly via cryptography).
    """
    hkdf = HKDF(
        algorithm=SHA256(),
        length=16,  # 128-bit AES key
        salt=salt,
        info=info,
    )
    return hkdf.derive(input_key)


# ---------------------------------------------------------------------------
# AES-GCM / AES-CBC helpers
# ---------------------------------------------------------------------------
def _split_iv_and_ciphertext(
    encrypted_data_and_iv: bytes, iv_length: int
) -> tuple[bytes, bytes]:
    """Split IV and ciphertext from a blob where the IV is prepended.

    Args:
        encrypted_data_and_iv: Concatenation of IV || CIPHERTEXT[(|| TAG)].
        iv_length: Expected IV length in bytes.

    Returns:
        A tuple (iv, ciphertext_with_tag_or_plain).

    Raises:
        ValueError: If the provided buffer is shorter than the IV.
    """
    if iv_length <= 0:
        raise ValueError("IV length must be positive")

    if len(encrypted_data_and_iv) < iv_length:
        raise ValueError("Encrypted buffer shorter than IV length")

    iv = encrypted_data_and_iv[:iv_length]
    ciphertext = encrypted_data_and_iv[iv_length:]
    if not ciphertext:
        raise ValueError("Ciphertext is empty (no payload after IV)")
    return iv, ciphertext


def decrypt_aes_gcm(
    key: bytes,
    encrypted_data_and_iv: bytes,
    additional_data: bytes | None = None,
    iv_length: int = GCM_IV_LEN_DEFAULT,
) -> bytes:
    """Decrypt AES-GCM where the IV is prepended to the payload.

    Args:
        key: AES-GCM key (16/24/32 bytes for 128/192/256-bit).
        encrypted_data_and_iv: IV || CIPHERTEXT||TAG (TAG appended by AESGCM).
        additional_data: Optional AAD bound to the ciphertext.
        iv_length: Length of the IV prefix (defaults to 12 for GCM).

    Returns:
        The decrypted plaintext bytes.

    Raises:
        ValueError: If framing is invalid or key length is unsupported.
        cryptography.exceptions.InvalidTag: If authentication fails.
    """
    if len(key) not in (16, 24, 32):
        raise ValueError("AESGCM key must be 16, 24, or 32 bytes")

    iv, ciphertext = _split_iv_and_ciphertext(encrypted_data_and_iv, iv_length)

    # AESGCM expects ciphertext+tag in a single buffer.
    aes_gcm = AESGCM(key)
    return aes_gcm.decrypt(iv, ciphertext, additional_data)


def encrypt_aes_gcm(
    key: bytes,
    plaintext: bytes,
    additional_data: bytes | None = None,
    iv_length: int = GCM_IV_LEN_DEFAULT,
) -> bytes:
    """Encrypt with AES-GCM and prepend the IV to the ciphertext+tag.

    Args:
        key: AES-GCM key (16/24/32 bytes).
        plaintext: Plaintext to encrypt.
        additional_data: Optional AAD bound to the ciphertext.
        iv_length: Length of the randomly generated IV (default: 12).

    Returns:
        IV || CIPHERTEXT||TAG

    Raises:
        ValueError: If key length or IV length is invalid.
    """
    if len(key) not in (16, 24, 32):
        raise ValueError("AESGCM key must be 16, 24, or 32 bytes")
    if iv_length <= 0:
        raise ValueError("IV length must be positive")

    iv = secrets.token_bytes(iv_length)
    aes_gcm = AESGCM(key)
    ciphertext = aes_gcm.encrypt(iv, plaintext, additional_data)
    return iv + ciphertext


def decrypt_aes_cbc_no_padding(
    key: bytes, encrypted_data_and_iv: bytes, iv_length: int = CBC_IV_LEN
) -> bytes:
    """Decrypt AES-CBC without padding where the IV is prepended.

    Args:
        key: AES key (16/24/32 bytes).
        encrypted_data_and_iv: IV || CIPHERTEXT (must be block-size aligned).
        iv_length: IV size (AES-CBC uses 16 bytes).

    Returns:
        Decrypted bytes (may include padding bytes if present in the source).

    Raises:
        ValueError: If framing is invalid or ciphertext not block-size aligned.
    """
    iv, ciphertext = _split_iv_and_ciphertext(encrypted_data_and_iv, iv_length)
    if len(ciphertext) % algorithms.AES.block_size // 8 != 0:
        raise ValueError("AES-CBC ciphertext is not block-size aligned")

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


# ---------------------------------------------------------------------------
# Higher-level decrypt/derive helpers (feature-neutral)
# ---------------------------------------------------------------------------
def decrypt_aes_gcm_with_derived_key(
    encrypted_data: bytes,
    private_key: bytes,
    key_type_string: bytes,
    derive_with_public_key: bool = False,
) -> bytes:
    """Decrypt a blob using HKDF-derived AES-GCM key.

    Two modes are supported:
    - Shared-secret mode (derive_with_public_key=False): derive AES key from
      `private_key` using `SECUREBOX||VERSION` salt and `SHARED_HKDF_AES_GCM`
      info.
    - ECDH mode (derive_with_public_key=True): the first 65 bytes after VERSION
      are treated as an uncompressed P-256 public key. An ECDH shared secret is
      computed using `private_key` and that public key; HKDF derives the AES key
      using `P256_HKDF_AES_GCM` as info.

    Args:
        encrypted_data: VERSION || [pubkey?] || IV || CIPHERTEXT||TAG
        private_key: Private key material (raw bytes; see ECDH mode note).
        key_type_string: AAD passed to AES-GCM (binds context).
        derive_with_public_key: If True, use ECDH with embedded public key.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        ValueError: If VERSION header is invalid or framing is malformed.
        cryptography.exceptions.InvalidTag: If authentication fails.
    """
    if len(encrypted_data) < 2 or encrypted_data[:2] != VERSION:
        raise ValueError("Invalid version or data length")

    version_length = len(VERSION)
    ciphertext_offset = 65 if derive_with_public_key else 0
    ciphertext_and_iv = encrypted_data[version_length + ciphertext_offset :]

    hkdf_salt = SECUREBOX + VERSION
    hkdf_info = P256_HKDF_AES_GCM if derive_with_public_key else SHARED_HKDF_AES_GCM

    if derive_with_public_key:
        shared_public_key = encrypted_data[
            version_length : version_length + ciphertext_offset
        ]
        private_key = derive_shared_secret(private_key, shared_public_key)

    derived_key = derive_key_using_hkdf_sha256(private_key, hkdf_salt, hkdf_info)
    return decrypt_aes_gcm(derived_key, ciphertext_and_iv, key_type_string)


def derive_shared_secret(private_key_jwt: bytes, public_key: bytes) -> bytes:
    """Compute ECDH shared secret on P-256.

    Args:
        private_key_jwt: Buffer whose first 32 bytes contain the raw private key.
        public_key: Uncompressed P-256 public key (65 bytes), SEC1 format.

    Returns:
        ECDH shared secret bytes.

    Raises:
        ValueError: If input lengths are invalid or key decoding fails.
    """
    if len(private_key_jwt) < 32:
        raise ValueError("Private key buffer too short (need at least 32 bytes)")
    if len(public_key) != 65:
        raise ValueError("Public key must be 65 bytes (uncompressed SEC1)")

    private_key_bytes = private_key_jwt[:32]
    priv = ec.derive_private_key(
        int.from_bytes(private_key_bytes, "big"), ec.SECP256R1()
    )
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
    return priv.exchange(ec.ECDH(), pub)


def decrypt_recovery_key(lskf_hash: bytes, encrypted_recovery_key: bytes) -> bytes:
    """Decrypt locally encrypted recovery key using the LSKF hash."""
    return decrypt_aes_gcm_with_derived_key(
        encrypted_recovery_key,
        lskf_hash,
        ascii_to_bytes("V1 locally_encrypted_recovery_key"),
    )


def decrypt_application_key(
    recovery_key: bytes, encrypted_application_key: bytes
) -> bytes:
    """Decrypt application key using the recovery key."""
    return decrypt_aes_gcm_with_derived_key(
        encrypted_application_key,
        recovery_key,
        ascii_to_bytes("V1 encrypted_application_key"),
    )


def decrypt_security_domain_key(
    application_key: bytes, encrypted_security_domain_key: bytes
) -> bytes:
    """Decrypt security domain key using the application key."""
    return decrypt_aes_gcm(application_key, encrypted_security_domain_key)


def decrypt_shared_key(
    security_domain_key: bytes, encrypted_shared_key: bytes
) -> bytes:
    """Decrypt shared key using the security domain key (ECDH/HKDF mode)."""
    return decrypt_aes_gcm_with_derived_key(
        encrypted_shared_key, security_domain_key, ascii_to_bytes("V1 shared_key"), True
    )


def decrypt_owner_key(shared_key: bytes, encrypted_owner_key: bytes) -> bytes:
    """Decrypt owner key using the shared key."""
    return decrypt_aes_gcm(shared_key, encrypted_owner_key)


def decrypt_eik(owner_key: bytes, encrypted_eik: bytes) -> bytes:
    """Decrypt EIK (ephemeral identity key) using the owner key.

    Note:
        The EIK format may be AES-CBC (no padding) or AES-GCM; we select based
        on total length of the blob.

    Raises:
        ValueError: If the EIK blob length is unexpected.
    """
    if len(encrypted_eik) == 48:  # 16 IV + 32 CIPHERTEXT (CBC, no tag)
        return decrypt_aes_cbc_no_padding(owner_key, encrypted_eik, CBC_IV_LEN)
    if len(encrypted_eik) == 60:  # 12 IV + 48 CT||TAG (GCM)
        return decrypt_aes_gcm(owner_key, encrypted_eik, iv_length=GCM_IV_LEN_DEFAULT)
    raise ValueError("The encrypted EIK has invalid length")


def decrypt_account_key(owner_key: bytes, encrypted_account_key: bytes) -> bytes:
    """Decrypt per-tracker account key using the owner key.

    Raises:
        ValueError: If the account key blob length is unexpected.
    """
    if len(encrypted_account_key) == 32:  # 16 IV + 16 CT (CBC)
        return decrypt_aes_cbc_no_padding(owner_key, encrypted_account_key, CBC_IV_LEN)
    if len(encrypted_account_key) == 44:  # 12 IV + 32 CT||TAG (GCM)
        return decrypt_aes_gcm(
            owner_key, encrypted_account_key, iv_length=GCM_IV_LEN_DEFAULT
        )
    raise ValueError("The encrypted Account Key has invalid length")


# ---------------------------------------------------------------------------
# Local/manual test only (not executed on import)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # WARNING: This section is for local testing only. Do not run in production
    # and never commit real keys. Output goes to stdout for developer inspection.

    # Load sample data
    pin = get_example_data("sample_pin")
    pin_salt = unhexlify(get_example_data("sample_pin_salt"))
    encrypted_recovery_key = unhexlify(
        get_example_data("sample_locally_encrypted_recovery_key")
    )
    encrypted_application_key = unhexlify(
        get_example_data("sample_encrypted_application_key")
    )
    encrypted_security_domain_key = unhexlify(
        get_example_data("sample_encrypted_security_domain_key")
    )
    encrypted_shared_key = unhexlify(get_example_data("sample_encrypted_shared_key"))
    encrypted_owner_key = unhexlify(get_example_data("sample_encrypted_owner_key"))
    encrypted_eik = unhexlify(get_example_data("sample_encrypted_eik"))
    encrypted_account_key = unhexlify(get_example_data("sample_encrypted_account_key"))

    # Calculate keys
    lskf_hash = get_lskf_hash(pin, pin_salt)
    recovery_key = decrypt_recovery_key(lskf_hash, encrypted_recovery_key)
    application_key = decrypt_application_key(recovery_key, encrypted_application_key)
    security_domain_key = decrypt_security_domain_key(
        application_key, encrypted_security_domain_key
    )
    shared_key = decrypt_shared_key(security_domain_key, encrypted_shared_key)
    owner_key = decrypt_owner_key(shared_key, encrypted_owner_key)
    eik = decrypt_eik(owner_key, encrypted_eik)
    account_key = decrypt_account_key(owner_key, encrypted_account_key)

    # Print results (developer-only)
    print("Recovery Key:")
    print(recovery_key.hex())
    print("Application Key:")
    print(application_key.hex())
    print("Security Domain Key:")
    print(security_domain_key.hex())
    print("Shared Key:")
    print(shared_key.hex())
    print("Owner Key:")
    print(owner_key.hex())
    print("EIK:")
    print(eik.hex())
    print("Account Key:")
    print(account_key.hex())

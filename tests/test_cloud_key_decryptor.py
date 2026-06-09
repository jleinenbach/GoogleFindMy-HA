# tests/test_cloud_key_decryptor.py
"""Coverage + regression lock for ``KeyBackup/cloud_key_decryptor.py``.

This module exercises the E2EE cryptographic primitives of the key-backup chain
using the **real** ``cryptography`` library (no mocks). Fixtures are constructed
with the module's own encrypt/derive helpers so every decrypt is verified as a
true roundtrip oracle.

Highlights:
- ``RL-1`` is a *discriminating* regression lock for the historically fixed
  operator-precedence / bit-vs-byte bug in ``decrypt_aes_cbc_no_padding``
  (oracle commit ``0c2798ab``): a 16-byte block-aligned ciphertext must NOT be
  rejected as "not block-size aligned". Under the buggy variant
  ``(len % algorithms.AES.block_size) // 8`` this would raise; under the fixed
  ``len % (algorithms.AES.block_size // 8)`` it passes.
- ``CT-1 … CT-29`` freeze the contract of every public (and one private)
  primitive as a safety net for future refactors.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import (
    ACCOUNT_KEY_CBC_TOTAL_LEN,
    ACCOUNT_KEY_GCM_TOTAL_LEN,
    EIK_CBC_TOTAL_LEN,
    EIK_GCM_TOTAL_LEN,
    P256_HKDF_AES_GCM,
    SECUREBOX,
    SHARED_HKDF_AES_GCM,
    VERSION,
    _split_iv_and_ciphertext,
    decrypt_account_key,
    decrypt_aes_cbc_no_padding,
    decrypt_aes_gcm,
    decrypt_aes_gcm_with_derived_key,
    decrypt_application_key,
    decrypt_eik,
    decrypt_owner_key,
    decrypt_recovery_key,
    decrypt_security_domain_key,
    decrypt_shared_key,
    derive_key_using_hkdf_sha256,
    derive_shared_secret,
    encrypt_aes_gcm,
)
from custom_components.googlefindmy.KeyBackup.lskf_hasher import ascii_to_bytes

# ---------------------------------------------------------------------------
# Fixtures / helpers (no mocks — real cryptography lib)
# ---------------------------------------------------------------------------
KEY16 = bytes(range(16))
KEY24 = bytes(range(24))
KEY32 = bytes(range(32))
KEY_BAD = bytes(64)  # 64 bytes -> invalid AES-GCM key length


def _cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt with raw AES-CBC (no padding); plaintext must be block-aligned."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _raw_p256_scalar() -> bytes:
    """Return a valid 32-byte raw P-256 private scalar."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_numbers().private_value.to_bytes(32, "big")


def _uncompressed_pub() -> bytes:
    """Return a fresh 65-byte uncompressed SEC1 P-256 public key."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )


def _build_shared_blob(ikm: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Build a VERSION || IV||CT||TAG blob for shared-secret (non-ECDH) mode."""
    derived = derive_key_using_hkdf_sha256(
        ikm, SECUREBOX + VERSION, SHARED_HKDF_AES_GCM
    )
    return VERSION + encrypt_aes_gcm(derived, plaintext, additional_data=aad)


def _build_ecdh_blob(priv_raw: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Build a VERSION || PUBKEY(65) || IV||CT||TAG blob for ECDH mode.

    The embedded public key comes from an ephemeral keypair; the shared secret
    is computed via the SUT's own ``derive_shared_secret`` so the decrypt path
    reconstructs the identical key.
    """
    embedded_pub = _uncompressed_pub()
    shared_secret = derive_shared_secret(priv_raw, embedded_pub)
    derived = derive_key_using_hkdf_sha256(
        shared_secret, SECUREBOX + VERSION, P256_HKDF_AES_GCM
    )
    ct_and_iv = encrypt_aes_gcm(derived, plaintext, additional_data=aad)
    return VERSION + embedded_pub + ct_and_iv


# ===========================================================================
# RL-1 — discriminating regression lock for BF-1 (oracle 0c2798ab)
# ===========================================================================
def test_rl1a_block_aligned_ciphertext_is_not_rejected() -> None:
    """RL-1a: a single 16-byte block must decrypt, not raise an alignment error.

    Discriminating: the buggy ``(len % 128) // 8`` would compute ``2`` for a
    16-byte ciphertext and wrongly raise; the fixed ``len % (128 // 8)`` is 0.
    """
    plaintext = bytes(range(16))  # exactly one AES block
    iv = bytes(16)
    ciphertext = _cbc_encrypt(KEY16, iv, plaintext)
    assert len(ciphertext) == 16

    result = decrypt_aes_cbc_no_padding(KEY16, iv + ciphertext, iv_length=16)

    assert result == plaintext


def test_rl1b_non_aligned_ciphertext_raises() -> None:
    """RL-1b: a 20-byte (non block-aligned) ciphertext must raise."""
    iv = bytes(16)
    ciphertext = bytes(20)  # 20 % 16 != 0
    with pytest.raises(ValueError, match="AES-CBC ciphertext is not block-size aligned"):
        decrypt_aes_cbc_no_padding(KEY16, iv + ciphertext, iv_length=16)


# ===========================================================================
# CT-1 … CT-29 — contract / coverage tests (refactor safety net)
# ===========================================================================
def test_ct1_hkdf_length_determinism_and_domain_separation() -> None:
    """CT-1: HKDF yields 16 deterministic bytes; different info => different key."""
    ikm = bytes(32)
    salt = SECUREBOX + VERSION
    key_a = derive_key_using_hkdf_sha256(ikm, salt, SHARED_HKDF_AES_GCM)
    key_a2 = derive_key_using_hkdf_sha256(ikm, salt, SHARED_HKDF_AES_GCM)
    key_b = derive_key_using_hkdf_sha256(ikm, salt, P256_HKDF_AES_GCM)

    assert len(key_a) == 16
    assert key_a == key_a2  # deterministic
    assert key_a != key_b  # domain separation via info


def test_ct2_split_non_positive_iv_length_raises() -> None:
    """CT-2: iv_length <= 0 raises."""
    with pytest.raises(ValueError, match="IV length must be positive"):
        _split_iv_and_ciphertext(bytes(16), 0)


def test_ct3_split_buffer_shorter_than_iv_raises() -> None:
    """CT-3: buffer shorter than IV raises."""
    with pytest.raises(ValueError, match="Encrypted buffer shorter than IV length"):
        _split_iv_and_ciphertext(bytes(4), 8)


def test_ct4_split_empty_payload_raises() -> None:
    """CT-4: no payload after the IV raises."""
    with pytest.raises(ValueError, match="Ciphertext is empty"):
        _split_iv_and_ciphertext(bytes(8), 8)


def test_ct5_split_happy_path() -> None:
    """CT-5: split returns (iv, ciphertext) correctly."""
    iv = bytes(range(12))
    ciphertext = bytes(range(12, 28))
    out_iv, out_ct = _split_iv_and_ciphertext(iv + ciphertext, 12)

    assert out_iv == iv
    assert out_ct == ciphertext


def test_ct6_gcm_decrypt_invalid_key_length_raises() -> None:
    """CT-6: AES-GCM decrypt rejects invalid key length."""
    with pytest.raises(ValueError, match="AESGCM key must be 16, 24, or 32 bytes"):
        decrypt_aes_gcm(KEY_BAD, bytes(32))


def test_ct7_gcm_roundtrip() -> None:
    """CT-7: encrypt_aes_gcm -> decrypt_aes_gcm roundtrip (with and without AAD)."""
    plaintext = b"the quick brown fox"
    blob = encrypt_aes_gcm(KEY32, plaintext)
    assert decrypt_aes_gcm(KEY32, blob) == plaintext

    aad = b"context"
    blob_aad = encrypt_aes_gcm(KEY16, plaintext, additional_data=aad)
    assert decrypt_aes_gcm(KEY16, blob_aad, additional_data=aad) == plaintext


def test_ct8_gcm_encrypt_invalid_key_length_raises() -> None:
    """CT-8: AES-GCM encrypt rejects invalid key length."""
    with pytest.raises(ValueError, match="AESGCM key must be 16, 24, or 32 bytes"):
        encrypt_aes_gcm(KEY_BAD, b"data")


def test_ct9_gcm_encrypt_non_positive_iv_length_raises() -> None:
    """CT-9: AES-GCM encrypt rejects non-positive IV length."""
    with pytest.raises(ValueError, match="IV length must be positive"):
        encrypt_aes_gcm(KEY16, b"data", iv_length=0)


def test_ct10_gcm_encrypt_frames_iv_then_ciphertext_tag() -> None:
    """CT-10: encrypt returns IV || CIPHERTEXT||TAG of expected length."""
    plaintext = b"1234567890"
    iv_length = 12
    blob = encrypt_aes_gcm(KEY24, plaintext, iv_length=iv_length)

    # IV (12) + ciphertext (len plaintext) + GCM tag (16)
    assert len(blob) == iv_length + len(plaintext) + 16
    assert decrypt_aes_gcm(KEY24, blob, iv_length=iv_length) == plaintext


def test_ct11_gcm_wrong_aad_raises_invalid_tag() -> None:
    """CT-11: a wrong additional_data fails authentication with InvalidTag."""
    blob = encrypt_aes_gcm(KEY16, b"secret", additional_data=b"aad-A")
    with pytest.raises(InvalidTag):
        decrypt_aes_gcm(KEY16, blob, additional_data=b"aad-B")


def test_ct12_cbc_block_aligned_roundtrip() -> None:
    """CT-12: AES-CBC (no padding) roundtrip over two aligned blocks."""
    plaintext = bytes(range(32))  # 2 blocks
    iv = bytes(16)
    ciphertext = _cbc_encrypt(KEY16, iv, plaintext)

    result = decrypt_aes_cbc_no_padding(KEY16, iv + ciphertext, iv_length=16)

    assert result == plaintext


def test_ct13_derived_key_invalid_version_raises() -> None:
    """CT-13: a wrong VERSION header raises."""
    bad = b"\x00\x00" + bytes(32)  # wrong version prefix
    with pytest.raises(ValueError, match="Invalid version or data length"):
        decrypt_aes_gcm_with_derived_key(bad, bytes(32), b"aad")


def test_ct14_derived_key_shared_mode_roundtrip() -> None:
    """CT-14: shared-secret (non-ECDH) derived-key roundtrip."""
    ikm = bytes(range(32))
    aad = b"V1 shared-mode"
    plaintext = b"shared-mode-secret"
    blob = _build_shared_blob(ikm, plaintext, aad)

    result = decrypt_aes_gcm_with_derived_key(
        blob, ikm, aad, derive_with_public_key=False
    )

    assert result == plaintext


def test_ct15_derived_key_ecdh_mode_roundtrip() -> None:
    """CT-15: ECDH derived-key roundtrip with an embedded 65-byte public key."""
    priv_raw = _raw_p256_scalar()
    aad = b"V1 ecdh-mode"
    plaintext = b"ecdh-mode-secret"
    blob = _build_ecdh_blob(priv_raw, plaintext, aad)

    result = decrypt_aes_gcm_with_derived_key(
        blob, priv_raw, aad, derive_with_public_key=True
    )

    assert result == plaintext


def test_ct16_derive_shared_secret_short_private_key_raises() -> None:
    """CT-16: a private key buffer shorter than 32 bytes raises."""
    with pytest.raises(
        ValueError, match="Private key buffer too short"
    ):
        derive_shared_secret(bytes(31), _uncompressed_pub())


def test_ct17_derive_shared_secret_bad_public_key_length_raises() -> None:
    """CT-17: a public key that is not 65 bytes raises."""
    with pytest.raises(
        ValueError, match="Public key must be 65 bytes"
    ):
        derive_shared_secret(_raw_p256_scalar(), bytes(64))


def test_ct18_derive_shared_secret_ecdh_symmetry() -> None:
    """CT-18: ECDH is symmetric — both parties derive the same secret."""
    alice = ec.generate_private_key(ec.SECP256R1())
    bob = ec.generate_private_key(ec.SECP256R1())
    alice_raw = alice.private_numbers().private_value.to_bytes(32, "big")
    bob_raw = bob.private_numbers().private_value.to_bytes(32, "big")
    alice_pub = alice.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    bob_pub = bob.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )

    secret_ab = derive_shared_secret(alice_raw, bob_pub)
    secret_ba = derive_shared_secret(bob_raw, alice_pub)

    assert secret_ab == secret_ba


def test_ct19_decrypt_recovery_key_roundtrip() -> None:
    """CT-19: decrypt_recovery_key wrapper roundtrip."""
    lskf_hash = bytes(range(32))
    plaintext = b"recovery-key-bytes"
    aad = ascii_to_bytes("V1 locally_encrypted_recovery_key")
    blob = _build_shared_blob(lskf_hash, plaintext, aad)

    assert decrypt_recovery_key(lskf_hash, blob) == plaintext


def test_ct20_decrypt_application_key_roundtrip() -> None:
    """CT-20: decrypt_application_key wrapper roundtrip."""
    recovery_key = bytes(range(1, 33))
    plaintext = b"application-key-bytes"
    aad = ascii_to_bytes("V1 encrypted_application_key")
    blob = _build_shared_blob(recovery_key, plaintext, aad)

    assert decrypt_application_key(recovery_key, blob) == plaintext


def test_ct21_decrypt_security_domain_key_roundtrip() -> None:
    """CT-21: decrypt_security_domain_key is plain AES-GCM (no derived key)."""
    application_key = KEY32
    plaintext = b"security-domain-key"
    blob = encrypt_aes_gcm(application_key, plaintext)

    assert decrypt_security_domain_key(application_key, blob) == plaintext


def test_ct22_decrypt_shared_key_ecdh_roundtrip() -> None:
    """CT-22: decrypt_shared_key wrapper (ECDH mode) roundtrip."""
    security_domain_key = _raw_p256_scalar()
    plaintext = b"shared-key-bytes"
    aad = ascii_to_bytes("V1 shared_key")
    blob = _build_ecdh_blob(security_domain_key, plaintext, aad)

    assert decrypt_shared_key(security_domain_key, blob) == plaintext


def test_ct23_decrypt_owner_key_roundtrip() -> None:
    """CT-23: decrypt_owner_key is plain AES-GCM (no derived key)."""
    shared_key = KEY16
    plaintext = b"owner-key-bytes"
    blob = encrypt_aes_gcm(shared_key, plaintext)

    assert decrypt_owner_key(shared_key, blob) == plaintext


def test_ct24_decrypt_eik_cbc_path() -> None:
    """CT-24: a 48-byte EIK blob takes the CBC path (16 IV + 32 CT)."""
    owner_key = KEY16
    plaintext = bytes(range(32))  # 2 blocks (CBC, no padding)
    iv = bytes(16)
    ciphertext = _cbc_encrypt(owner_key, iv, plaintext)
    blob = iv + ciphertext
    assert len(blob) == EIK_CBC_TOTAL_LEN

    assert decrypt_eik(owner_key, blob) == plaintext


def test_ct25_decrypt_eik_gcm_path() -> None:
    """CT-25: a 60-byte EIK blob takes the GCM path (12 IV + 32 CT + 16 TAG)."""
    owner_key = KEY16
    plaintext = bytes(range(32))
    blob = encrypt_aes_gcm(owner_key, plaintext, iv_length=12)
    assert len(blob) == EIK_GCM_TOTAL_LEN

    assert decrypt_eik(owner_key, blob) == plaintext


def test_ct26_decrypt_eik_invalid_length_raises() -> None:
    """CT-26: an EIK blob of unexpected length raises."""
    with pytest.raises(ValueError, match="The encrypted EIK has invalid length"):
        decrypt_eik(KEY16, bytes(50))


def test_ct27_decrypt_account_key_cbc_path() -> None:
    """CT-27: a 32-byte account-key blob takes the CBC path (16 IV + 16 CT)."""
    owner_key = KEY16
    plaintext = bytes(range(16))  # one block
    iv = bytes(16)
    ciphertext = _cbc_encrypt(owner_key, iv, plaintext)
    blob = iv + ciphertext
    assert len(blob) == ACCOUNT_KEY_CBC_TOTAL_LEN

    assert decrypt_account_key(owner_key, blob) == plaintext


def test_ct28_decrypt_account_key_gcm_path() -> None:
    """CT-28: a 44-byte account-key blob takes the GCM path (12 IV + 16 CT + 16 TAG)."""
    owner_key = KEY16
    plaintext = bytes(range(16))
    blob = encrypt_aes_gcm(owner_key, plaintext, iv_length=12)
    assert len(blob) == ACCOUNT_KEY_GCM_TOTAL_LEN

    assert decrypt_account_key(owner_key, blob) == plaintext


def test_ct29_decrypt_account_key_invalid_length_raises() -> None:
    """CT-29: an account-key blob of unexpected length raises."""
    with pytest.raises(
        ValueError, match="The encrypted Account Key has invalid length"
    ):
        decrypt_account_key(KEY16, bytes(40))

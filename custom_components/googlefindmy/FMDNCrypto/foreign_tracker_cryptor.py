# custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Crypto primitives used to encrypt/decrypt Find My Device payloads on the NIST
SECP160r1 curve with AES-EAX for authenticated encryption.

Design goals (feature-neutral, HA-friendly):
- Keep public function signatures unchanged.
- Add clear docstrings, type hints and defensive checks.
- Avoid undefined behavior (e.g., s == 0, invalid lengths).
- Prefer explicitness and readability over micro-optimizations.

Notes:
- SECP160r1 has a 160-bit field; x/y coordinates are 20 bytes.
- For primes p ≡ 3 (mod 4), modular square roots can be computed as
  y = a^((p+1)/4) mod p (used by rx_to_ry).  See references in docs.
"""

# custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py

from __future__ import annotations

import asyncio
import secrets
from binascii import unhexlify

from Cryptodome.Cipher import AES
from ecdsa import SECP160r1
from ecdsa.ellipticcurve import CurveFp, Point
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    generate_eid,
    calculate_r,
)
from custom_components.googlefindmy.example_data_provider import get_example_data

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# AES-EAX authenticates with a 16-byte tag by default in PyCryptodome.
_AES_TAG_LEN: int = 16
# SECP160r1 coordinate length in bytes (160 bits)
_COORD_LEN: int = 20
# Nonce is constructed as LRx(8) || LSx(8) = 16 bytes (see spec used here)
_NONCE_LEN: int = 16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def rx_to_ry(Rx: int, curve: CurveFp) -> int:
    """Recover the even Y coordinate for a given X on a short Weierstrass curve.

    This reconstructs a point from a compressed representation by solving
    y^2 = x^3 + a·x + b (mod p) and returning the *even* root (as customary
    for "even-y" decompression).

    Args:
        Rx: X coordinate as integer.
        curve: The underlying finite field curve (ecdsa.ellipticcurve.CurveFp).

    Returns:
        The even Y coordinate as integer.

    Raises:
        ValueError: If the provided X does not yield a valid point on the curve.
    """
    # Compute y^2 = x^3 + a·x + b (mod p)
    Ryy = (Rx**3 + curve.a() * Rx + curve.b()) % curve.p()

    # For p ≡ 3 (mod 4): y = (y^2)^((p+1)//4) mod p is a square root
    Ry = pow(Ryy, (curve.p() + 1) // 4, curve.p())

    # Verify root
    if (Ry * Ry) % curve.p() != Ryy:
        raise ValueError("The provided X coordinate is not on the curve.")

    # Ensure even y (standardized choice)
    if Ry % 2 != 0:
        Ry = curve.p() - Ry

    return Ry


def _require_len(name: str, b: bytes, expected: int) -> None:
    """Validate a fixed length for bytes-like inputs."""
    if len(b) != expected:
        raise ValueError(f"{name} must be exactly {expected} bytes (got {len(b)})")


# ---------------------------------------------------------------------------
# AES-EAX wrappers (authenticated encryption)
# ---------------------------------------------------------------------------


def encrypt_aes_eax(data: bytes, nonce: bytes, key: bytes) -> tuple[bytes, bytes]:
    """Encrypt and authenticate with AES-EAX-256.

    Args:
        data: Plaintext bytes.
        nonce: 16-byte nonce used for EAX.
        key: 32-byte AES key (AES-256).

    Returns:
        (ciphertext, tag) — tag is 16 bytes.

    Raises:
        ValueError: If key/nonce lengths are invalid.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes (AES-256).")
    if len(nonce) != _NONCE_LEN:
        raise ValueError("Nonce must be 16 bytes for this construction.")

    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return ciphertext, tag


def decrypt_aes_eax(data: bytes, tag: bytes, nonce: bytes, key: bytes) -> bytes:
    """Decrypt and verify AES-EAX-256.

    Args:
        data: Ciphertext bytes.
        tag: 16-byte authentication tag from encryption.
        nonce: 16-byte nonce.
        key: 32-byte AES key.

    Returns:
        Decrypted plaintext.

    Raises:
        ValueError: If key/nonce/tag lengths are invalid or tag verification fails.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes (AES-256).")
    if len(nonce) != _NONCE_LEN:
        raise ValueError("Nonce must be 16 bytes for this construction.")
    if len(tag) != _AES_TAG_LEN:
        raise ValueError("Tag must be 16 bytes for AES-EAX.")

    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    return cipher.decrypt_and_verify(data, tag)


# ---------------------------------------------------------------------------
# ECIES-like envelope (domain-specific construction)
# ---------------------------------------------------------------------------


def encrypt(message: bytes, random: bytes, eid: bytes) -> tuple[bytes, bytes]:
    """Encrypt a payload to an ephemeral EID on SECP160r1 using AES-EAX-256.

    Construction (as implemented in the original code and preserved):
    1) Reduce a random scalar s mod n (guard s != 0); S = s·G.
    2) Rebuild R from eid (x-only); choose even y via rx_to_ry.
    3) Derive k = HKDF-SHA256( (s·R).x ) → 32 bytes.
    4) nonce = LRx(8) || LSx(8).
    5) m' || tag = AES-EAX-256_ENC(k, nonce, message).
    6) Output: (m' || tag, Sx).

    Args:
        message: Plaintext payload.
        random: Caller-provided random bytes (entropy source for s).
        eid: 20-byte X coordinate (compressed point) for the receiver.

    Returns:
        (encrypted_with_tag, Sx) where encrypted_with_tag = m' || tag (tag=16B),
        and Sx is 20-byte X coordinate of S.

    Raises:
        ValueError: On invalid inputs (lengths) or curve mismatch.
    """
    # Curve parameters
    curve = SECP160r1
    order = curve.order

    # Validate EID length (x coordinate on SECP160r1)
    _require_len("eid", eid, _COORD_LEN)

    # Derive scalar s from caller-provided randomness; guard s != 0
    s = int.from_bytes(random, byteorder="big", signed=False) % order
    if s == 0:
        # Extremely unlikely; avoid the point at infinity by bumping to 1
        s = 1

    # S = s·G
    S = s * curve.generator

    # Rebuild R from EID (x only) and choose even Y
    Rx = int.from_bytes(eid, byteorder="big")
    Ry = rx_to_ry(Rx, curve.curve)
    R = Point(curve.curve, Rx, Ry)

    # Derive AES-256 key via HKDF-SHA256 over (s·R).x (20 bytes)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"")
    k: bytes = hkdf.derive((s * R).x().to_bytes(_COORD_LEN, "big"))

    # Nonce = LRx(8) || LSx(8)
    LRx = Rx.to_bytes(_COORD_LEN, "big")[-8:]
    LSx = S.x().to_bytes(_COORD_LEN, "big")[-8:]
    nonce: bytes = LRx + LSx  # 16 bytes

    # Encrypt (AES-EAX-256) → m' || tag
    m_dash, tag = encrypt_aes_eax(message, nonce, k)
    encrypted_with_tag: bytes = m_dash + tag
    return encrypted_with_tag, S.x().to_bytes(_COORD_LEN, "big")


def decrypt(
    identity_key: bytes, encryptedAndTag: bytes, Sx: bytes, beacon_time_counter: int
) -> bytes:
    """Decrypt a payload sent to a tracker identity on SECP160r1 with AES-EAX-256.

    Construction (mirrors `encrypt` above):
    1) Compute r from (identity_key, beacon_time_counter); R = r·G.
    2) Rebuild S from Sx (x-only); choose even y via rx_to_ry.
    3) Derive k = HKDF-SHA256( (r·S).x ) → 32 bytes.
    4) nonce = LRx(8) || LSx(8).
    5) Split m' || tag and AES-EAX-256_DEC(k, nonce, m', tag).

    Args:
        identity_key: 20-byte tracker identity/private key material (domain-specific).
        encryptedAndTag: Ciphertext concatenated with 16-byte tag.
        Sx: 20-byte X coordinate of ephemeral S.
        beacon_time_counter: Time counter used to derive r.

    Returns:
        Decrypted plaintext.

    Raises:
        ValueError: On invalid input lengths or verification failure.
    """
    # Basic validations
    _require_len("identity_key", identity_key, _COORD_LEN)
    _require_len("Sx", Sx, _COORD_LEN)
    if len(encryptedAndTag) < _AES_TAG_LEN:
        raise ValueError("encryptedAndTag must be at least 16 bytes (contains tag).")

    # Split ciphertext and tag
    m_dash: bytes = encryptedAndTag[:-_AES_TAG_LEN]
    tag: bytes = encryptedAndTag[-_AES_TAG_LEN:]

    # Curve and scalar r
    curve = SECP160r1
    r = calculate_r(identity_key, beacon_time_counter)

    # R = r·G
    R = r * curve.generator

    # Rebuild S from Sx (x-only) and choose even Y
    Sx_int = int.from_bytes(Sx, byteorder="big")
    Sy = rx_to_ry(Sx_int, curve.curve)
    S = Point(curve.curve, Sx_int, Sy)

    # Derive AES-256 key via HKDF-SHA256 over (r·S).x
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"")
    k: bytes = hkdf.derive((r * S).x().to_bytes(_COORD_LEN, "big"))

    # Nonce = LRx(8) || LSx(8)
    LRx = R.x().to_bytes(_COORD_LEN, "big")[-8:]
    LSx = S.x().to_bytes(_COORD_LEN, "big")[-8:]
    nonce: bytes = LRx + LSx  # 16 bytes

    # AES-EAX-256 decrypt & verify
    return decrypt_aes_eax(m_dash, tag, nonce, k)


# ---------------------------------------------------------------------------
# Optional async wrapper (non-breaking): offload CPU work to a worker thread
# ---------------------------------------------------------------------------


async def async_decrypt(
    identity_key: bytes, encryptedAndTag: bytes, Sx: bytes, beacon_time_counter: int
) -> bytes:
    """Async convenience wrapper for `decrypt(...)` using `asyncio.to_thread`.

    This preserves the original sync API while allowing async call sites
    (e.g., inside Home Assistant event loop) to avoid blocking.
    """
    return await asyncio.to_thread(
        decrypt, identity_key, encryptedAndTag, Sx, beacon_time_counter
    )


# ---------------------------------------------------------------------------
# Self-test with example vectors
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 4-byte timestamp
    timestamp = 0x0084D000

    sample_identity_key = unhexlify(get_example_data("sample_identity_key"))
    sample_location_data = unhexlify(get_example_data("sample_location_data"))

    # Generate EID (x-only) and 32-byte randomness for s
    eid = generate_eid(sample_identity_key, timestamp)
    random = secrets.token_bytes(32)

    encryptedAndTag, Sx = encrypt(sample_location_data, random, eid)

    print("Encrypted Message and Tag: " + encryptedAndTag.hex())
    print("Random Sx: " + Sx.hex())

    decrypted = decrypt(sample_identity_key, encryptedAndTag, Sx, timestamp)
    print("Decrypted Message: " + decrypted.hex())

    assert decrypted == sample_location_data

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

from Cryptodome.Cipher import AES
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from custom_components.googlefindmy.example_data_provider import get_example_data
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveFpProtocol,
    CurveParametersProtocol,
    load_curve,
    load_curve_fp_class,
    load_point_class,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    calculate_r,
    generate_eid,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# AES-EAX authenticates with a 16-byte tag by default in PyCryptodome.
_AES_KEY_LEN: int = 32
_AES_TAG_LEN: int = 16
# SECP160r1 coordinate length in bytes (160 bits)
_COORD_LEN: int = 20
# Nonce is constructed as LRx(8) || LSx(8) = 16 bytes (see spec used here)
_NONCE_LEN: int = 16

_CURVE: CurveParametersProtocol = load_curve()
CurveFp = load_curve_fp_class()
Point = load_point_class()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def rx_to_ry(Rx: int, curve: CurveFpProtocol) -> int:
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
    p: int = int(curve.p())
    a: int = int(curve.a())
    b: int = int(curve.b())

    Rx_mod: int = Rx % p

    # Compute y^2 = x^3 + a·x + b (mod p)
    Ryy: int = (pow(Rx_mod, 3, p) + (a * Rx_mod) + b) % p

    # For p ≡ 3 (mod 4): y = (y^2)^((p+1)//4) mod p is a square root
    sqrt_candidate: int = pow(Ryy, (p + 1) // 4, p)

    # Verify root
    if (sqrt_candidate * sqrt_candidate) % p != Ryy:
        raise ValueError("The provided X coordinate is not on the curve.")

    # Ensure even y (standardized choice)
    if sqrt_candidate % 2 != 0:
        sqrt_candidate = p - sqrt_candidate

    Ry: int = int(sqrt_candidate)
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
        (ciphertext, tag) where tag is 16 bytes.

    Raises:
        ValueError: On invalid nonce/key lengths.
    """
    _require_len("nonce", nonce, _NONCE_LEN)
    _require_len("key", key, _AES_KEY_LEN)

    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    m_dash, tag = cipher.encrypt_and_digest(data)
    return m_dash, tag


def decrypt_aes_eax(m_dash: bytes, tag: bytes, nonce: bytes, key: bytes) -> bytes:
    """Decrypt and verify AES-EAX-256 payloads."""
    _require_len("nonce", nonce, _NONCE_LEN)
    _require_len("key", key, _AES_KEY_LEN)
    _require_len("tag", tag, _AES_TAG_LEN)

    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    plaintext: bytes = cipher.decrypt(m_dash)
    cipher.verify(tag)
    return plaintext


# ---------------------------------------------------------------------------
# SECP160r1 EID helpers
# ---------------------------------------------------------------------------


def encrypt(message: bytes, random: bytes, eid: bytes) -> tuple[bytes, bytes]:
    """Encrypt a message for a tracker identity using ECDH + AES-EAX-256.

    Args:
        message: Plaintext to encrypt.
        random: Caller-provided random bytes (entropy source for s).
        eid: 20-byte X coordinate (compressed point) for the receiver.

    Returns:
        (encrypted_with_tag, Sx) where encrypted_with_tag = m' || tag (tag=16B),
        and Sx is 20-byte X coordinate of S.

    Raises:
        ValueError: On invalid inputs (lengths) or curve mismatch.
    """
    # Curve parameters
    curve = _CURVE
    order: int = int(curve.order)

    # Validate EID length (x coordinate on SECP160r1)
    _require_len("eid", eid, _COORD_LEN)

    # Derive scalar s from caller-provided randomness; guard s != 0
    s = int.from_bytes(random, byteorder="big", signed=False) % order
    if s == 0:
        # Extremely unlikely; avoid the point at infinity by bumping to 1
        s = 1

    # S = s·G
    generator = curve.generator
    S = s * generator

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
    curve = _CURVE
    order: int = int(curve.order)
    r = calculate_r(identity_key, beacon_time_counter) % order

    # R and S points
    Rx = generate_eid(identity_key, beacon_time_counter)
    R = int.from_bytes(Rx, byteorder="big")
    _ = rx_to_ry(R, curve.curve)
    Sx_int = int.from_bytes(Sx, byteorder="big")
    Sy = rx_to_ry(Sx_int, curve.curve)

    curve_fp: CurveFpProtocol = curve.curve
    S = Point(curve_fp, Sx_int, Sy)

    # Derive AES-256 key via HKDF-SHA256 over (r·S).x (20 bytes)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"")
    k: bytes = hkdf.derive((r * S).x().to_bytes(_COORD_LEN, "big"))

    # Nonce = LRx(8) || LSx(8)
    LRx = R.to_bytes(_COORD_LEN, "big")[-8:]
    LSx = Sx[-8:]
    nonce: bytes = LRx + LSx  # 16 bytes

    # Decrypt and verify (raises ValueError on failure)
    plaintext: bytes = decrypt_aes_eax(m_dash, tag, nonce, k)
    return plaintext


# ---------------------------------------------------------------------------
# CLI helpers (manual testing)
# ---------------------------------------------------------------------------


def _get_keys() -> tuple[bytes, bytes]:
    # Returns a test identity key and public key pair
    identity_key = bytes.fromhex(get_example_data("sample_identity_key"))
    public_key = bytes.fromhex(get_example_data("sample_public_key"))
    return identity_key, public_key


def _get_random_bytes(length: int) -> bytes:
    # Returns random bytes of a specified length
    return secrets.token_bytes(length)


def _create_random_eid(identity_key: bytes) -> bytes:
    # Uses generate_eid to create a random EID
    beacon_time_counter: int = int.from_bytes(_get_random_bytes(4), byteorder="big")
    return generate_eid(identity_key, beacon_time_counter)


async def _async_cli() -> None:  # pragma: no cover - manual testing only
    # Example usage
    identity_key, public_key = _get_keys()
    beacon_time_counter = 0

    # Generate random data to encrypt
    random_data = _get_random_bytes(_COORD_LEN)
    eid = _create_random_eid(identity_key)

    # Encrypt
    encryptedAndTag, Sx = encrypt(random_data, random_data, eid)
    print(f"Encrypted: {encryptedAndTag.hex()}")

    # Decrypt
    decrypted = decrypt(identity_key, encryptedAndTag, Sx, beacon_time_counter)
    print(f"Decrypted: {decrypted.hex()}")


def _cli() -> None:  # pragma: no cover - manual testing only
    asyncio.run(_async_cli())


if __name__ == "__main__":  # pragma: no cover - manual testing only
    _cli()

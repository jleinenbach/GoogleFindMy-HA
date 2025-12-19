# custom_components/googlefindmy/FMDNCrypto/eid_generator.py
"""Eddystone-EID derivation helpers (AES-128, no elliptic curves).

This module implements the public, documented Eddystone-EID computation:
- Temporary Key: AES-128(identity_key, temp_key_data)
- EID: AES-128(temp_key, eid_data) truncated to 8 bytes (MSB)

Find My Device Network (FMDN) is widely understood to build on the same
cryptographic primitive; the public Eddystone document specifies the exact
16-byte block layouts and truncation rules.

References:
- Google (n.d.): "EID Computation" (Eddystone-EID). See:
  https://raw.githubusercontent.com/google/eddystone/master/eddystone-eid/eid-computation.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# Public Eddystone-EID parameters
K_DEFAULT: Final[int] = 10
K: Final[int] = K_DEFAULT  # Backwards-compatible alias used by some callers.

ROTATION_PERIOD: Final[int] = 1 << K_DEFAULT  # 2^K seconds

# Identity key size per Eddystone spec (AES-128 key).
IDENTITY_KEY_LENGTH: Final[int] = 16

# Some FMDN implementations surface a 32-byte "identity key" after decryption.
# Eddystone-EID itself needs 16 bytes; callers may split 32 bytes into two
# candidates and test both.
EIK_LENGTH: Final[int] = 32

# Eddystone-EID advertised length: 8 bytes (64 bits).
EID_LENGTH: Final[int] = 8

# Legacy constant kept for compatibility with older resolver code paths.
LEGACY_EID_LENGTH: Final[int] = EID_LENGTH


@dataclass(frozen=True, slots=True)
class EidCandidate:
    """Candidate EID produced for a given time window and key interpretation."""

    name: str
    eid: bytes


def _aes128_ecb_encrypt(key_16: bytes, block_16: bytes) -> bytes:
    """Encrypt a single 16-byte block with AES-128-ECB."""
    if len(key_16) != IDENTITY_KEY_LENGTH:
        raise ValueError(f"identity_key must be {IDENTITY_KEY_LENGTH} bytes; got {len(key_16)}")
    if len(block_16) != 16:
        raise ValueError(f"block must be 16 bytes; got {len(block_16)}")

    cipher = Cipher(algorithms.AES(key_16), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(block_16) + encryptor.finalize()


def _mask_time_counter(time_counter_u32: int, k: int) -> int:
    """Mask the lowest k bits of the 32-bit time counter (rotation alignment)."""
    if not (0 <= k <= 15):
        raise ValueError(f"k must be in 0..15; got {k}")
    mask = (~((1 << k) - 1)) & 0xFFFFFFFF
    return time_counter_u32 & mask


def _build_temporary_key_block(time_counter_u32: int) -> bytes:
    """Build the 16-byte 'temporary key' input block per Eddystone-EID.

    Layout (byte offsets):
    - 0..10: 0x00 padding
    - 11: 0xFF (salt)
    - 12..13: 0x00 padding
    - 14..15: top 16 bits of time counter (big-endian)
    """
    ts_top16 = (time_counter_u32 >> 16) & 0xFFFF
    return b"\x00" * 11 + b"\xFF" + b"\x00" * 2 + ts_top16.to_bytes(2, "big")


def _build_eid_block(time_counter_u32: int, k: int) -> bytes:
    """Build the 16-byte EID input block per Eddystone-EID.

    Layout (byte offsets):
    - 0..10: 0x00 padding
    - 11: K (rotation period exponent)
    - 12..15: time counter (big-endian), with the lowest K bits cleared
    """
    masked = _mask_time_counter(time_counter_u32, k)
    return b"\x00" * 11 + bytes([k & 0xFF]) + masked.to_bytes(4, "big")


def generate_eid_eddystone(identity_key_16: bytes, time_counter: int, *, k: int = K_DEFAULT) -> bytes:
    """Compute the 8-byte Eddystone-EID for a given 32-bit seconds counter.

    Args:
        identity_key_16: 16-byte AES-128 identity key.
        time_counter: Beacon time counter in seconds. Treated as 32-bit unsigned.
        k: Rotation period exponent (0..15). New EID every 2^k seconds.

    Returns:
        8-byte EID value (leading 8 bytes of AES output).
    """
    time_counter_u32 = int(time_counter) & 0xFFFFFFFF

    temp_key_block = _build_temporary_key_block(time_counter_u32)
    temp_key = _aes128_ecb_encrypt(identity_key_16, temp_key_block)

    eid_block = _build_eid_block(time_counter_u32, k)
    eid_full = _aes128_ecb_encrypt(temp_key, eid_block)

    return eid_full[:EID_LENGTH]


def _identity_key_candidates(identity_key: bytes) -> tuple[tuple[str, bytes], ...]:
    """Return 16-byte AES identity key candidates from the provided bytes."""
    if not isinstance(identity_key, (bytes, bytearray)):
        raise TypeError(f"identity_key must be bytes-like; got {type(identity_key)!r}")

    key_bytes = bytes(identity_key)
    if len(key_bytes) == IDENTITY_KEY_LENGTH:
        return (("aes128_identity_key_16", key_bytes),)

    if len(key_bytes) == EIK_LENGTH:
        first = key_bytes[:16]
        second = key_bytes[16:]
        if first == second:
            return (("aes128_identity_key_32_dup_first16", first),)
        return (
            ("aes128_identity_key_32_first16", first),
            ("aes128_identity_key_32_second16", second),
        )

    raise ValueError(
        f"Unsupported identity_key length {len(key_bytes)}; expected 16 (Eddystone) or 32 (FMDN decrypted key)"
    )


def generate_eid_candidates(identity_key: bytes, unix_time_seconds: int, *, k: int = K_DEFAULT) -> tuple[EidCandidate, ...]:
    """Generate Eddystone-EID candidates for the given second counter.

    For 32-byte keys, this yields two candidates (first/second half) to allow
    callers to validate which half matches the beacon's AES-128 identity key.
    """
    candidates: list[EidCandidate] = []
    for name, key16 in _identity_key_candidates(identity_key):
        eid = generate_eid_eddystone(key16, unix_time_seconds, k=k)
        candidates.append(EidCandidate(name=f"eddystone_eid/{name}/k{k}", eid=eid))
    return tuple(candidates)


def generate_eid(identity_key: bytes, timestamp: int) -> bytes:
    """Compatibility wrapper: returns the first Eddystone-EID candidate."""
    return generate_eid_candidates(identity_key, timestamp)[0].eid

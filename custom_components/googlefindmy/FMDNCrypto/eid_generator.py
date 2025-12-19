# custom_components/googlefindmy/FMDNCrypto/eid_generator.py
"""Ephemeral identifier derivation helpers for the FHN Accessory spec.

This module provides the spec-driven primitives for deriving Find My Device
Network (FHNA/FMDN) ephemeral identifiers without layering resolver heuristics.
It exposes a minimal API:

* `build_table10_prf_input` constructs the 32-byte Table 10 buffer.
* `prf_aes_256_ecb` runs the AES-256-ECB PRF over that buffer.
* `generate_eid` returns the legacy secp160r1 x-coordinate (20 bytes).
* `generate_eid_p256` returns the modern secp256r1 x-coordinate (32 bytes).
* `generate_eid_p256_le` mirrors the modern path with little-endian scalar
  interpretation for devices that require it.

All inputs are strictly validated. No truncation or speculative variants are
produced here; any resolver-side policy should build on top of these
deterministic outputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final, Literal

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveParametersProtocol,
    load_curve,
)

FHNA_K: Final[int] = 10
K: Final[int] = FHNA_K
ROTATION_PERIOD: Final[int] = 1 << FHNA_K
EIK_LENGTH: Final[int] = 32
LEGACY_EID_LENGTH: Final[int] = 20
MODERN_EID_LENGTH: Final[int] = 32
FHNA_PRF_INPUT_LENGTH: Final[int] = 32
FHNA_ROTATION_MASK: Final[int] = (1 << FHNA_K) - 1
FHNA_TIMESTAMP_MASK: Final[int] = 0xFFFFFFFF
P256_ORDER: Final[int] = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)
_CURVE: CurveParametersProtocol = load_curve()

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EidCandidate:
    """Ephemeral identifier variant produced for a time window."""

    name: str
    eid: bytes


class EidVariant(Enum):
    """Supported FHNA EID variants."""

    LEGACY_SECP160R1_X20_BE = auto()
    MODERN_P256_X32_BE = auto()
    MODERN_P256_X32_LE_SCALAR = auto()


def _normalize_ts_u32(timestamp: int, *, strict: bool = True) -> int:
    """Normalize a timestamp to an unsigned 32-bit integer.

    Args:
        timestamp: Raw timestamp value to normalize.
        strict: Raise instead of coercing when the value is out of range.

    Raises:
        TypeError: If ``timestamp`` is not an ``int`` or is a ``bool``.
        ValueError: If ``timestamp`` is outside ``[0, FHNA_TIMESTAMP_MASK]`` in
            strict mode.
    """

    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise TypeError(f"timestamp must be int (not bool); got {type(timestamp)!r}")

    if 0 <= timestamp <= FHNA_TIMESTAMP_MASK:
        return timestamp

    if strict:
        raise ValueError(f"timestamp out of u32 range: {timestamp}")

    _LOGGER.warning("timestamp out of u32 range; coercing via & 0xFFFFFFFF: %s", timestamp)
    return timestamp & FHNA_TIMESTAMP_MASK


def _align_ts_to_rotation(
    ts_u32: int, *, rotation_mask: int = FHNA_ROTATION_MASK
) -> tuple[int, bool]:
    """Align a normalized timestamp to the rotation boundary."""

    aligned: int = ts_u32 & ~rotation_mask
    return aligned, aligned != ts_u32


def build_table10_prf_input(timestamp: int, *, k: int = K, strict: bool = True) -> bytes:
    """Return the FHNA Table 10 PRF input buffer for a raw timestamp.

    FHN Accessory Specification v1.3 Table 10: Construction of a pseudorandom
    number mandates a 32-byte buffer composed of fixed padding, the rotation
    exponent (``K = 10``), and the rotation-aligned timestamp. Inputs are
    normalized to an unsigned 32-bit integer and aligned to the current
    rotation boundary before encoding the AES-256-ECB pseudorandom function
    buffer.
    """

    if k != FHNA_K:
        raise ValueError(f"Unsupported rotation exponent {k}; FHNA requires FHNA_K={FHNA_K}")

    ts_u32: int = _normalize_ts_u32(timestamp, strict=strict)
    masked_ts, was_aligned = _align_ts_to_rotation(ts_u32)
    if was_aligned:
        _LOGGER.debug("Timestamp %s masked to rotation-aligned %s", ts_u32, masked_ts)

    ts_bytes = masked_ts.to_bytes(4, byteorder="big", signed=False)

    block = bytearray(FHNA_PRF_INPUT_LENGTH)
    block[0:11] = b"\xff" * 11
    block[11] = FHNA_K
    block[12:16] = ts_bytes
    block[16:27] = b"\x00" * 11
    block[27] = FHNA_K
    block[28:32] = ts_bytes

    return bytes(block)


def prf_aes_256_ecb(eik: bytes, prf_input: bytes) -> bytes:
    """Encrypt the FHNA PRF input with AES-256-ECB.

    Args:
        eik: 32-byte Ephemeral Identity Key.
        prf_input: 32-byte Table 10 buffer.

    Raises:
        ValueError: If either input is not exactly 32 bytes.
    """

    if len(eik) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")
    if len(prf_input) != FHNA_PRF_INPUT_LENGTH:
        raise ValueError(f"PRF input must be {FHNA_PRF_INPUT_LENGTH} bytes")

    cipher = Cipher(algorithms.AES(eik), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(prf_input) + encryptor.finalize()


def _prf_table10(identity_key: bytes, timestamp: int, k: int = K, *, strict: bool = True) -> bytes:
    """Derive the 32-byte Table 10 pseudorandom output from the masked timestamp."""

    prf_input = build_table10_prf_input(timestamp, k=k, strict=strict)
    return prf_aes_256_ecb(identity_key, prf_input)


def calculate_r(identity_key: bytes, timestamp: int) -> int:
    """Compute the random scalar ``r`` per FHN Accessory Specification v1.3."""

    r_dash = _prf_table10(identity_key, timestamp, K)
    r_dash_int = int.from_bytes(r_dash, byteorder="big", signed=False)

    curve_order: int = int(_CURVE.order)
    r_mod: int = (r_dash_int % (curve_order - 1)) + 1
    return r_mod


def generate_eid(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a legacy FHNA EID using secp160r1 semantics."""

    if len(identity_key) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")

    r_bytes = _prf_table10(identity_key, timestamp, K)
    r_prime: int = int.from_bytes(r_bytes, byteorder="big", signed=False)

    curve = _CURVE
    curve_order: int = int(curve.order)
    generator = curve.generator

    r: int = (r_prime % (curve_order - 1)) + 1
    R = r * generator

    x_int: int = int(R.x())
    return x_int.to_bytes(LEGACY_EID_LENGTH, "big")


def _derive_scalar_p256(
    identity_key: bytes,
    timestamp: int,
    K: int,
    *,
    byteorder: Literal["big", "little"] = "big",
    strict: bool = True,
) -> int:
    """Derive the secp256r1 scalar ``r`` from the Table 10 PRF output."""

    r_dash: bytes = _prf_table10(identity_key, timestamp, K, strict=strict)
    r_dash_int: int = int.from_bytes(r_dash, byteorder=byteorder, signed=False)

    curve_order: int = P256_ORDER
    return (r_dash_int % (curve_order - 1)) + 1


def _serialize_p256_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp256r1."""

    curve = ec.SECP256R1()
    public_numbers = ec.derive_private_key(scalar_r, curve).public_key().public_numbers()

    x_int: int = int(public_numbers.x)
    return x_int.to_bytes(MODERN_EID_LENGTH, byteorder="big")


def generate_eid_p256(
    identity_key: bytes, timestamp: int, *, strict: bool = True
) -> bytes:
    """Generate a modern FHNA EID using secp256r1 semantics (big endian)."""

    if len(identity_key) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")

    scalar_r = _derive_scalar_p256(identity_key, timestamp, K, strict=strict)
    return _serialize_p256_x(scalar_r)


def generate_eid_p256_le(
    identity_key: bytes, timestamp: int, *, strict: bool = True
) -> bytes:
    """Generate a modern FHNA EID using P-256 with little-endian scalar derivation."""

    if len(identity_key) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")

    scalar_r = _derive_scalar_p256(
        identity_key, timestamp, K, byteorder="little", strict=strict
    )
    return _serialize_p256_x(scalar_r)


def get_masked_timestamp(timestamp: int, k: int, *, strict: bool = True) -> bytes:
    """Return the rotation-aligned timestamp bytes for a raw timestamp."""

    ts_u32: int = _normalize_ts_u32(timestamp, strict=strict)
    rotation_mask: int = ((1 << k) - 1) & FHNA_TIMESTAMP_MASK
    masked, _ = _align_ts_to_rotation(ts_u32, rotation_mask=rotation_mask)

    return masked.to_bytes(4, byteorder="big", signed=False)


def generate_eid_candidates(
    identity_key: bytes, timestamp: int, *, include_little_endian: bool = False
) -> tuple[EidCandidate, ...]:
    """Return canonical EID candidates for the rotation window."""

    legacy = EidCandidate(name="fhna_secp160r1_rx20", eid=generate_eid(identity_key, timestamp))
    modern = EidCandidate(name="fhna_secp256r1_rx32", eid=generate_eid_p256(identity_key, timestamp))

    candidates: list[EidCandidate] = [legacy, modern]

    if include_little_endian:
        le = EidCandidate(
            name="fhna_secp256r1_le_rx32",
            eid=generate_eid_p256_le(identity_key, timestamp),
        )
        candidates.append(le)

    return tuple(candidates)

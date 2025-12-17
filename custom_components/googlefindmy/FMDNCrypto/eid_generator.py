"""Ephemeral identifier derivation helpers for the FHN Accessory spec."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy.example_data_provider import get_example_data
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveParametersProtocol,
    load_curve,
)

# Constants
FHNA_K: Final[int] = 10
K: Final[int] = FHNA_K
ROTATION_PERIOD: Final[int] = 1 << FHNA_K
EIK_LENGTH: Final[int] = 32
LEGACY_EID_LENGTH: Final[int] = 20
FHNA_PRF_INPUT_LENGTH: Final[int] = 32
FHNA_ROTATION_MASK: Final[int] = (1 << FHNA_K) - 1
FHNA_TIMESTAMP_MASK: Final[int] = 0xFFFFFFFF
TRUNCATED_P256_EID_LENGTH: Final[int] = 20
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
        raise TypeError(
            f"timestamp must be int (not bool); got {type(timestamp)!r}"
        )

    if 0 <= timestamp <= FHNA_TIMESTAMP_MASK:
        return timestamp

    if strict:
        raise ValueError(f"timestamp out of u32 range: {timestamp}")

    _LOGGER.warning(
        "timestamp out of u32 range; coercing via & 0xFFFFFFFF: %s", timestamp
    )
    return timestamp & FHNA_TIMESTAMP_MASK


def _align_ts_to_rotation(
    ts_u32: int, *, rotation_mask: int = FHNA_ROTATION_MASK
) -> tuple[int, bool]:
    """Align a normalized timestamp to the rotation boundary."""

    aligned: int = ts_u32 & ~rotation_mask
    return aligned, aligned != ts_u32


def fhna_build_prf_input(timestamp: int, *, strict: bool = True) -> bytes:
    """Return the FHNA Table 10 PRF input buffer for a raw timestamp.

    FHN Accessory Specification v1.3 Table 10: Construction of a pseudorandom
    number mandates a 32-byte buffer composed of fixed padding, the rotation
    exponent (``K = 10``), and the rotation-aligned timestamp. Inputs are
    normalized to an unsigned 32-bit integer and aligned to the current
    rotation boundary before encoding the AES-256-ECB pseudorandom function
    buffer.
    """

    ts_u32: int = _normalize_ts_u32(timestamp, strict=strict)
    masked_ts, was_aligned = _align_ts_to_rotation(ts_u32)
    if was_aligned:
        _LOGGER.debug(
            "Timestamp %s masked to rotation-aligned %s", ts_u32, masked_ts
        )

    ts_bytes = masked_ts.to_bytes(4, byteorder="big", signed=False)

    block = bytearray(FHNA_PRF_INPUT_LENGTH)
    block[0:11] = b"\xff" * 11
    block[11] = FHNA_K
    block[12:16] = ts_bytes
    block[16:27] = b"\x00" * 11
    block[27] = FHNA_K
    block[28:32] = ts_bytes

    return bytes(block)


def build_table10_block(timestamp: int, k: int = K, *, strict: bool = True) -> bytes:
    """Legacy wrapper for FHNA Table 10 PRF input construction.

    The ``k`` argument is retained for backward compatibility with older test
    helpers but defaults to the FHNA rotation exponent. Inputs that request a
    different ``k`` are rejected to avoid silently producing malformed PRF
    buffers.
    """

    if k != FHNA_K:
        raise ValueError(
            f"Unsupported rotation exponent {k}; FHNA requires FHNA_K={FHNA_K}"
        )

    return fhna_build_prf_input(timestamp, strict=strict)


def fhna_prf_aes_ecb_256(eik: bytes, prf_input: bytes) -> bytes:
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


def _prf_table10(
    identity_key: bytes, timestamp: int, k: int = K, *, strict: bool = True
) -> bytes:
    """Derive the 32-byte Table 10 pseudorandom output from the masked timestamp."""

    return fhna_prf_aes_ecb_256(
        identity_key, build_table10_block(timestamp, k=k, strict=strict)
    )


def generate_eid(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a legacy FHNA EID using secp160r1 semantics."""

    return generate_eid_fhna_secp160r1(identity_key, timestamp)


def generate_eid_p256(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a modern FHNA EID using secp256r1 semantics (big endian)."""

    scalar_r = _derive_scalar_p256(identity_key, timestamp, K, byteorder="big")
    return _serialize_p256_x(scalar_r)



def generate_eid_fhna_secp160r1(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a 20-byte FHNA EID on secp160r1 (Table 10 PRF)."""

    prf_input = fhna_build_prf_input(timestamp)
    r_bytes = fhna_prf_aes_ecb_256(identity_key, prf_input)
    r_prime: int = int.from_bytes(r_bytes, byteorder="big", signed=False)

    curve = _CURVE
    curve_order: int = int(curve.order)
    generator = curve.generator

    # Project into 1..n-1 to avoid the point at infinity when r_prime % n == 0
    r: int = (r_prime % (curve_order - 1)) + 1
    R = r * generator

    x_int: int = int(R.x())
    x_bytes: bytes = x_int.to_bytes(LEGACY_EID_LENGTH, "big")
    return x_bytes


def generate_eid_fhna_secp256r1(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a 32-byte FHNA EID on secp256r1 (Table 10 PRF)."""

    return generate_eid_p256(identity_key, timestamp)


def generate_eid_candidates(identity_key: bytes, timestamp: int) -> tuple[EidCandidate, ...]:
    """Return legacy and modern EID candidates for the masked rotation window."""

    if len(identity_key) != EIK_LENGTH:
        raise ValueError(
            f"Ephemeral Identity Key must be {EIK_LENGTH} bytes; got {len(identity_key)}"
        )

    legacy_eid = generate_eid_fhna_secp160r1(identity_key, timestamp)
    modern_eid = generate_eid_fhna_secp256r1(identity_key, timestamp)
    truncated_modern = modern_eid[:TRUNCATED_P256_EID_LENGTH]

    return (
        EidCandidate(name="fhna_secp160r1_rx20", eid=legacy_eid),
        EidCandidate(name="fhna_secp256r1_rx32", eid=modern_eid),
        EidCandidate(name="fhna_p256_truncated_rx20", eid=truncated_modern),
    )


def calculate_r(identity_key: bytes, timestamp: int) -> int:
    """Compute the random scalar ``r`` per FHN Accessory Specification v1.3.

    FHN Accessory Specification v1.3 - Table 10: Construction of a
    pseudorandom number mandates the AES-ECB-256 input layout (``0xFF`` padding,
    repeated timestamp blocks, and fixed rotation period exponent ``K = 10``)
    used below. The byte construction ensures the encrypted block satisfies the
    spec's requirements for producing the pseudorandom value that is later
    reduced modulo the curve order to obtain ``r``.
    """
    r_dash = _prf_table10(identity_key, timestamp, K)

    # Convert r' to an integer
    r_dash_int = int.from_bytes(r_dash, byteorder="big", signed=False)

    # SECP160R1 parameters
    curve = _CURVE
    curve_order: int = int(curve.order)

    # r' is now projected to the finite field Fp by calculating r = (r' mod (n-1)) + 1
    # Keep the scalar in the valid range [1, n-1]
    r_mod: int = (r_dash_int % (curve_order - 1)) + 1
    return r_mod


def _derive_scalar_p256(
    identity_key: bytes,
    timestamp: int,
    K: int,
    *,
    byteorder: Literal["big", "little"] = "big",
) -> int:
    """Derive the secp256r1 scalar ``r`` from the Table 10 PRF output.

    FHN Accessory Specification v1.3 — Table 10 defines the AES-ECB-256 PRF.
    The resulting 32-byte value is interpreted as an integer using the provided
    ``byteorder`` before being projected into the curve order range via
    ``(r' mod (n - 1)) + 1`` to avoid the point at infinity.
    """

    r_dash: bytes = _prf_table10(identity_key, timestamp, K)

    # Convert r' to an integer using the requested endianness
    r_dash_int: int = int.from_bytes(r_dash, byteorder=byteorder, signed=False)

    curve_order: int = P256_ORDER

    # r' is now projected to the finite field Fp by calculating r = (r' mod (n-1)) + 1
    # Keep the scalar in the valid range [1, n-1]
    r_mod: int = (r_dash_int % (curve_order - 1)) + 1
    return r_mod


def _serialize_p256_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp256r1."""

    curve = ec.SECP256R1()
    public_numbers = ec.derive_private_key(scalar_r, curve).public_key().public_numbers()

    x_int: int = int(public_numbers.x)
    x_bytes: bytes = x_int.to_bytes(32, byteorder="big")
    return x_bytes


def get_masked_timestamp(timestamp: int, K: int, *, strict: bool = True) -> bytes:
    """Return the rotation-aligned timestamp bytes for a raw timestamp.

    Table 10 requires clearing the lowest ``K`` bits of the beacon time counter
    before encrypting the AES input block. Inputs are normalized to an unsigned
    32-bit integer, aligned to the requested rotation period, and returned in
    big-endian form.

    Args:
        timestamp: The original timestamp as an ``int``.
    """

    ts_u32: int = _normalize_ts_u32(timestamp, strict=strict)
    rotation_mask: int = ((1 << K) - 1) & FHNA_TIMESTAMP_MASK
    masked, _was_aligned = _align_ts_to_rotation(ts_u32, rotation_mask=rotation_mask)

    return masked.to_bytes(4, byteorder="big", signed=False)


def generate_eid_p256_le(
    identity_key: bytes, timestamp: int, K: int = FHNA_K
) -> bytes:
    """Generate an EID using P-256 with Little Endian scalar derivation."""

    r = _derive_scalar_p256(identity_key, timestamp, K, byteorder="little")
    return _serialize_p256_x(r)


if __name__ == "__main__":
    sample_identity_key_hex = get_example_data("sample_identity_key")
    sample_identity_key = bytes.fromhex(sample_identity_key_hex)

    # Generate EIDs
    for i in range(1000):
        timestamp = i * ROTATION_PERIOD
        eid = generate_eid(sample_identity_key, timestamp)
        print(f"{timestamp}: {eid.hex()}")

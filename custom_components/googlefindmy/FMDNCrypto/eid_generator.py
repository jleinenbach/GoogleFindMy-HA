# custom_components/googlefindmy/FMDNCrypto/eid_generator.py
"""Spec-driven FHNA ephemeral identifier derivation primitives.

This module exposes deterministic, side-effect free helpers for Find My Device
Network (FHNA/FMDN) ephemeral identifiers. Responsibilities are intentionally
split so resolver heuristics remain out-of-tree:

* ``build_table10_prf_input`` constructs the 32-byte Table 10 buffer
  (FHN Accessory Specification v1.3 — Table 10).
* ``prf_aes_256_ecb`` applies the AES-256-ECB PRF to that buffer.
* ``generate_eid_variant`` derives explicit EID variants given an Ephemeral
  Identity Key (EIK), a 32-bit time counter, and a declared ``EidVariant``.
* ``generate_eid`` is a thin, deprecated wrapper that forces callers to pass an
  explicit ``EidVariant`` to avoid silent semantic changes.
"""

from __future__ import annotations

import logging
import warnings
from enum import Enum
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
FHNA_COUNTER_MASK: Final[int] = 0xFFFFFFFF
P256_ORDER: Final[int] = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)
_CURVE: CurveParametersProtocol = load_curve()

_LOGGER = logging.getLogger(__name__)


class EidVariant(str, Enum):
    """Supported FHNA EID variants (explicit, no silent format changes)."""

    LEGACY_SECP160R1_X20_BE = "legacy_secp160r1_x20_be"
    MODERN_P256_X32_BE = "modern_p256_x32_be"
    MODERN_P256_X20_TRUNC_BE = "modern_p256_x20_trunc_be"
    MODERN_P256_X32_LE_SCALAR = "modern_p256_x32_le_scalar"
    MODERN_P256_X20_TRUNC_LE = "modern_p256_x20_trunc_le"


def _normalize_time_counter(time_counter_u32: int, *, strict: bool) -> int:
    """Normalize a raw time counter to u32.

    A strict call enforces ``0 <= counter <= 0xFFFFFFFF``. Lenient callers mask
    wrap-around counters with ``& FHNA_COUNTER_MASK`` to preserve drift signals.
    """

    if isinstance(time_counter_u32, bool) or not isinstance(time_counter_u32, int):
        raise TypeError(
            f"time_counter_u32 must be int (not bool); got {type(time_counter_u32)!r}"
        )

    if 0 <= time_counter_u32 <= FHNA_COUNTER_MASK:
        return time_counter_u32

    if strict:
        raise ValueError(f"time_counter_u32 out of u32 range: {time_counter_u32}")

    masked = time_counter_u32 & FHNA_COUNTER_MASK
    _LOGGER.debug(
        "time_counter_u32 out of range (%s); masking to %s", time_counter_u32, masked
    )
    return masked


def _align_to_rotation(
    counter_u32: int, *, rotation_mask: int = FHNA_ROTATION_MASK
) -> tuple[int, bool]:
    """Return the rotation-aligned counter per Table 10."""

    aligned: int = counter_u32 & ~rotation_mask
    return aligned, aligned != counter_u32


def build_table10_prf_input(
    time_counter_u32: int, *, k: int = K, strict: bool = True
) -> bytes:
    """Return the 32-byte Table 10 PRF input buffer (FHN spec v1.3 — Table 10)."""

    if k != FHNA_K:
        raise ValueError(f"Unsupported rotation exponent {k}; FHNA requires FHNA_K={FHNA_K}")

    counter_u32: int = _normalize_time_counter(time_counter_u32, strict=strict)
    masked_counter, was_aligned = _align_to_rotation(counter_u32)
    if was_aligned:
        _LOGGER.debug("Counter %s masked to rotation-aligned %s", counter_u32, masked_counter)

    counter_bytes = masked_counter.to_bytes(4, byteorder="big", signed=False)

    block = bytearray(FHNA_PRF_INPUT_LENGTH)
    block[0:11] = b"\xff" * 11
    block[11] = FHNA_K
    block[12:16] = counter_bytes
    block[16:27] = b"\x00" * 11
    block[27] = FHNA_K
    block[28:32] = counter_bytes

    return bytes(block)


def prf_aes_256_ecb(eik: bytes, prf_input: bytes) -> bytes:
    """Encrypt the FHNA PRF input with AES-256-ECB (deterministic, no padding)."""

    if len(eik) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")
    if len(prf_input) != FHNA_PRF_INPUT_LENGTH:
        raise ValueError(f"PRF input must be {FHNA_PRF_INPUT_LENGTH} bytes")

    cipher = Cipher(algorithms.AES(eik), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(prf_input) + encryptor.finalize()


def _prf_table10(
    identity_key: bytes,
    time_counter_u32: int,
    k: int = K,
    *,
    strict: bool = True,
) -> bytes:
    """Derive the Table 10 pseudorandom output for the provided counter."""

    prf_input = build_table10_prf_input(time_counter_u32, k=k, strict=strict)
    return prf_aes_256_ecb(identity_key, prf_input)


def _derive_scalar(  # noqa: PLR0913
    identity_key: bytes,
    time_counter_u32: int,
    *,
    k: int,
    byteorder: Literal["big", "little"],
    curve_order: int,
    strict: bool,
    include_zero_endpoint: bool = False,
) -> int:
    """Derive a scalar from the Table 10 PRF output.

    Modern P-256 trackers require an open interval ``[1, curve_order - 1]`` to
    avoid the point at infinity, while legacy FHNA accessories project directly
    into the closed interval ``[0, curve_order - 1]``. The `include_zero_endpoint`
    toggle preserves the legacy modulo-n behavior (Table 10) instead of the
    P-256-adjusted projection used by modern trackers.
    """

    r_dash: bytes = _prf_table10(identity_key, time_counter_u32, k, strict=strict)
    r_dash_int: int = int.from_bytes(r_dash, byteorder=byteorder, signed=False)
    order: int = int(curve_order)

    if include_zero_endpoint:
        mod_n_scalar: int = r_dash_int % order
        return mod_n_scalar

    projected_scalar: int = (r_dash_int % (order - 1)) + 1
    return projected_scalar


def _serialize_legacy_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp160r1."""

    curve = _CURVE
    generator = curve.generator
    R = scalar_r * generator

    x_int: int = int(R.x())
    return x_int.to_bytes(LEGACY_EID_LENGTH, byteorder="big")


def _serialize_p256_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp256r1."""

    curve = ec.SECP256R1()
    public_numbers = ec.derive_private_key(scalar_r, curve).public_key().public_numbers()

    x_int: int = int(public_numbers.x)
    return x_int.to_bytes(MODERN_EID_LENGTH, byteorder="big")


def generate_eid_variant(
    eik: bytes,
    time_counter_u32: int,
    variant: EidVariant,
    *,
    k: int = K,
    strict: bool = True,
) -> bytes:
    """Return the explicit EID variant for the given counter and EIK."""

    if len(eik) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")
    counter_u32 = _normalize_time_counter(time_counter_u32, strict=strict)

    if variant is EidVariant.LEGACY_SECP160R1_X20_BE:
        curve_order: int = int(_CURVE.order)
        scalar = _derive_scalar(
            eik,
            counter_u32,
            k=k,
            byteorder="big",
            curve_order=curve_order,
            strict=strict,
            include_zero_endpoint=True,
        )
        return _serialize_legacy_x(scalar)

    if variant is EidVariant.MODERN_P256_X32_BE:
        scalar = _derive_scalar(
            eik,
            counter_u32,
            k=k,
            byteorder="big",
            curve_order=P256_ORDER,
            strict=strict,
        )
        return _serialize_p256_x(scalar)

    if variant is EidVariant.MODERN_P256_X20_TRUNC_BE:
        full = generate_eid_variant(
            eik,
            counter_u32,
            EidVariant.MODERN_P256_X32_BE,
            k=k,
            strict=strict,
        )
        return full[:LEGACY_EID_LENGTH]

    if variant is EidVariant.MODERN_P256_X32_LE_SCALAR:
        scalar = _derive_scalar(
            eik,
            counter_u32,
            k=k,
            byteorder="little",
            curve_order=P256_ORDER,
            strict=strict,
        )
        return _serialize_p256_x(scalar)

    if variant is EidVariant.MODERN_P256_X20_TRUNC_LE:
        full = generate_eid_variant(
            eik,
            counter_u32,
            EidVariant.MODERN_P256_X32_LE_SCALAR,
            k=k,
            strict=strict,
        )
        return full[:LEGACY_EID_LENGTH]

    raise ValueError(f"Unsupported EID variant: {variant}")


def get_masked_counter(time_counter_u32: int, k: int, *, strict: bool = True) -> bytes:
    """Return the rotation-aligned counter bytes for diagnostics."""

    counter_u32 = _normalize_time_counter(time_counter_u32, strict=strict)
    rotation_mask: int = ((1 << k) - 1) & FHNA_COUNTER_MASK
    masked, _ = _align_to_rotation(counter_u32, rotation_mask=rotation_mask)

    return masked.to_bytes(4, byteorder="big", signed=False)


def generate_eid(
    eik: bytes,
    time_counter_u32: int,
    *,
    variant: EidVariant,
    k: int = K,
    strict: bool = True,
) -> bytes:
    """Deprecated shim that forwards to ``generate_eid_variant``.

    Callers must pass ``variant`` explicitly to avoid silent format changes.
    """

    warnings.warn(
        "generate_eid is deprecated; call generate_eid_variant(..., variant=...) directly",
        DeprecationWarning,
        stacklevel=2,
    )
    return generate_eid_variant(eik, time_counter_u32, variant, k=k, strict=strict)

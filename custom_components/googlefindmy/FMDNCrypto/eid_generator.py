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
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

__all__ = [
    "EidVariant",
    "EIK_LENGTH",
    "FHNA_K",
    "HeuristicBasis",
    "HeuristicEidResult",
    "K",
    "LEGACY_EID_LENGTH",
    "MODERN_EID_LENGTH",
    "P256_ORDER",
    "ROTATION_PERIOD",
    "ROTATION_PERIOD_900",
    "ROTATION_PERIOD_3600",
    "build_heuristic_prf_input",
    "build_table10_prf_input",
    "generate_eid",
    "generate_eid_variant",
    "generate_heuristic_eid",
    "get_masked_counter",
    "prf_aes_256_ecb",
]

from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveParametersProtocol,
    load_curve,
)
from custom_components.googlefindmy.FMDNCrypto._lazy_crypto import (
    get_algorithms_module,
    get_cipher_class,
    get_ec_module,
    get_modes_module,
    get_p256_curve,
)

FHNA_K: Final[int] = 10
K: Final[int] = FHNA_K
ROTATION_PERIOD: Final[int] = 1 << FHNA_K  # 1024 seconds (standard FMDN trackers)
ROTATION_PERIOD_900: Final[int] = 900  # 15 minutes (Android phone MAC sync)
ROTATION_PERIOD_3600: Final[int] = 3600  # 1 hour (alternative phone period)
EIK_LENGTH: Final[int] = 32
LEGACY_EID_LENGTH: Final[int] = 20
MODERN_EID_LENGTH: Final[int] = 32
FHNA_PRF_INPUT_LENGTH: Final[int] = 32
FHNA_ROTATION_MASK: Final[int] = (1 << FHNA_K) - 1
FHNA_COUNTER_MASK: Final[int] = 0xFFFFFFFF

# Heuristic rotation periods for phone discovery (ordered by likelihood)
HEURISTIC_ROTATION_PERIODS: Final[tuple[int, ...]] = (900, 3600, 1024)
P256_ORDER: Final[int] = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)

# Lazy-loaded curve instances (deferred to first use for faster startup)
_CURVE: CurveParametersProtocol | None = None
_P256_CURVE: object | None = None

_LOGGER = logging.getLogger(__name__)


def _get_curve() -> CurveParametersProtocol:
    """Get the SECP160r1 curve, loading lazily on first access."""
    global _CURVE  # noqa: PLW0603
    if _CURVE is None:
        _CURVE = load_curve()
    return _CURVE


def _get_p256_curve() -> object:
    """Get the P-256 curve, loading lazily on first access."""
    global _P256_CURVE  # noqa: PLW0603
    if _P256_CURVE is None:
        _P256_CURVE = get_p256_curve()
    return _P256_CURVE


class EidVariant(str, Enum):
    """Supported FHNA EID variants (explicit, no silent format changes)."""

    LEGACY_SECP160R1_X20_BE = "legacy_secp160r1_x20_be"
    MODERN_P256_X32_BE = "modern_p256_x32_be"
    MODERN_P256_X20_TRUNC_BE = "modern_p256_x20_trunc_be"
    MODERN_P256_X32_LE_SCALAR = "modern_p256_x32_le_scalar"
    MODERN_P256_X20_TRUNC_LE = "modern_p256_x20_trunc_le"


class HeuristicBasis(str, Enum):
    """Time basis modes for heuristic EID generation.

    Android phones with "Offline Finding" may use different time bases than
    the standard FMDN tracker protocol:

    - RELATIVE: counter = (now - anchor) // period  (standard FMDN)
    - ABSOLUTE: counter = now // period  (Android phone style)
    """

    RELATIVE = "relative"
    ABSOLUTE = "absolute"


@dataclass(frozen=True, slots=True)
class HeuristicEidResult:
    """Result of a successful heuristic EID match.

    Contains the discovered parameters that successfully matched the payload,
    allowing the resolver to cache and reuse them for future lookups.
    """

    eid_bytes: bytes
    rotation_period: int
    basis: HeuristicBasis
    variant: EidVariant
    counter: int
    drift_offset: int  # -1, 0, or +1 from the expected counter
    is_reversed: bool


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
    was_modified: bool = aligned != counter_u32
    return aligned, was_modified


def build_table10_prf_input(
    time_counter_u32: int,
    *,
    k: int = K,
    strict: bool = True,
    _normalized: bool = False,
) -> bytes:
    """Return the 32-byte Table 10 PRF input buffer (FHN spec v1.3 — Table 10).

    Args:
        time_counter_u32: Raw or pre-normalized 32-bit time counter.
        k: Rotation exponent (must equal FHNA_K).
        strict: Enforce u32 range when True.
        _normalized: Internal flag to skip redundant normalization when the
            caller has already validated the counter.
    """
    if k != FHNA_K:
        raise ValueError(
            f"Unsupported rotation exponent {k}; FHNA requires FHNA_K={FHNA_K}"
        )

    counter_u32: int = (
        time_counter_u32
        if _normalized
        else _normalize_time_counter(time_counter_u32, strict=strict)
    )
    masked_counter, was_modified = _align_to_rotation(counter_u32)
    if was_modified:
        _LOGGER.debug(
            "Counter %s masked to rotation-aligned %s", counter_u32, masked_counter
        )

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

    # Lazy-load cryptography modules for faster startup
    Cipher = get_cipher_class()
    algorithms = get_algorithms_module()
    modes = get_modes_module()
    cipher = Cipher(algorithms.AES(eik), modes.ECB())
    encryptor = cipher.encryptor()
    ciphertext: bytes = encryptor.update(prf_input) + encryptor.finalize()
    return ciphertext


def _prf_table10(
    identity_key: bytes,
    time_counter_u32: int,
    k: int = K,
    *,
    strict: bool = True,
    _normalized: bool = False,
) -> bytes:
    """Derive the Table 10 pseudorandom output for the provided counter."""
    prf_input = build_table10_prf_input(
        time_counter_u32, k=k, strict=strict, _normalized=_normalized
    )
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
    _normalized: bool = False,
) -> int:
    """Derive a scalar from the Table 10 PRF output.

    Modern P-256 trackers require an open interval ``[1, curve_order - 1]`` to
    avoid the point at infinity, while legacy FHNA accessories project directly
    into the closed interval ``[0, curve_order - 1]``. The `include_zero_endpoint`
    toggle preserves the legacy modulo-n behavior (Table 10) instead of the
    P-256-adjusted projection used by modern trackers.
    """
    r_dash: bytes = _prf_table10(
        identity_key, time_counter_u32, k, strict=strict, _normalized=_normalized
    )
    r_dash_int: int = int.from_bytes(r_dash, byteorder=byteorder, signed=False)

    if include_zero_endpoint:
        mod_n_scalar: int = r_dash_int % curve_order
        return mod_n_scalar

    projected_scalar: int = (r_dash_int % (curve_order - 1)) + 1
    return projected_scalar


def _serialize_legacy_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp160r1."""

    curve = _get_curve()
    generator = curve.generator
    R = scalar_r * generator

    x_int: int = int(R.x())
    return x_int.to_bytes(LEGACY_EID_LENGTH, byteorder="big")


def _serialize_p256_x(scalar_r: int) -> bytes:
    """Return the big-endian x-coordinate for ``R = r * G`` on secp256r1."""
    ec = get_ec_module()
    public_numbers = (
        ec.derive_private_key(scalar_r, _get_p256_curve()).public_key().public_numbers()
    )

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

    match variant:
        case EidVariant.LEGACY_SECP160R1_X20_BE:
            curve_order: int = int(_get_curve().order)
            scalar = _derive_scalar(
                eik,
                counter_u32,
                k=k,
                byteorder="big",
                curve_order=curve_order,
                strict=strict,
                include_zero_endpoint=True,
                _normalized=True,
            )
            return _serialize_legacy_x(scalar)

        case EidVariant.MODERN_P256_X32_BE:
            scalar = _derive_scalar(
                eik,
                counter_u32,
                k=k,
                byteorder="big",
                curve_order=P256_ORDER,
                strict=strict,
                _normalized=True,
            )
            return _serialize_p256_x(scalar)

        case EidVariant.MODERN_P256_X20_TRUNC_BE:
            full = generate_eid_variant(
                eik,
                counter_u32,
                EidVariant.MODERN_P256_X32_BE,
                k=k,
                strict=strict,
            )
            return full[:LEGACY_EID_LENGTH]

        case EidVariant.MODERN_P256_X32_LE_SCALAR:
            scalar = _derive_scalar(
                eik,
                counter_u32,
                k=k,
                byteorder="little",
                curve_order=P256_ORDER,
                strict=strict,
                _normalized=True,
            )
            return _serialize_p256_x(scalar)

        case EidVariant.MODERN_P256_X20_TRUNC_LE:
            full = generate_eid_variant(
                eik,
                counter_u32,
                EidVariant.MODERN_P256_X32_LE_SCALAR,
                k=k,
                strict=strict,
            )
            return full[:LEGACY_EID_LENGTH]

        case _:
            raise ValueError(f"Unsupported EID variant: {variant}")


def get_masked_counter(time_counter_u32: int, k: int, *, strict: bool = True) -> bytes:
    """Return the rotation-aligned counter bytes for diagnostics."""
    if k != FHNA_K:
        raise ValueError(
            f"Unsupported rotation exponent {k}; FHNA requires FHNA_K={FHNA_K}"
        )

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


# =============================================================================
# Heuristic EID Generation for Android Phone Discovery
# =============================================================================
#
# Android phones with "Offline Finding" may use different parameters than
# standard FMDN trackers:
# - Rotation period: 900s (15 min) or 3600s (1 hour) instead of 1024s
# - Time basis: Absolute Unix time instead of relative (now - anchor)
#
# These functions support flexible rotation periods using integer division
# instead of the power-of-2 bitwise operations required by FHNA spec.
# =============================================================================


def _align_to_rotation_flexible(timestamp: int, *, rotation_period: int) -> int:
    """Align a timestamp to the start of its rotation window using integer division.

    Unlike ``_align_to_rotation`` which uses bitwise masking (power-of-2 only),
    this function supports arbitrary rotation periods like 900s or 3600s.

    Args:
        timestamp: Unix timestamp in seconds.
        rotation_period: Rotation period in seconds (e.g., 900, 1024, 3600).

    Returns:
        The timestamp aligned to the start of its rotation window.
    """
    if rotation_period <= 0:
        raise ValueError(f"rotation_period must be positive; got {rotation_period}")
    return (timestamp // rotation_period) * rotation_period


def _compute_heuristic_counter(
    now_unix: int,
    *,
    rotation_period: int,
    basis: HeuristicBasis,
    anchor: int | None = None,
) -> int:
    """Compute the EID counter based on the specified time basis.

    Args:
        now_unix: Current Unix timestamp in seconds.
        rotation_period: Rotation period in seconds.
        basis: Time basis mode (ABSOLUTE or RELATIVE).
        anchor: Anchor timestamp for relative basis (pair_date or secrets_creation_date).

    Returns:
        The counter value aligned to the rotation period.

    Raises:
        ValueError: If RELATIVE basis is used without an anchor.
    """
    if basis == HeuristicBasis.ABSOLUTE:
        # Android phone style: counter based on absolute Unix time
        return _align_to_rotation_flexible(now_unix, rotation_period=rotation_period)

    # RELATIVE basis: counter based on elapsed time since anchor
    if anchor is None or anchor <= 0:
        raise ValueError(
            f"RELATIVE basis requires a valid anchor timestamp; got {anchor}"
        )
    elapsed = now_unix - anchor
    if elapsed < 0:
        _LOGGER.debug(
            "Negative elapsed time (%s - %s = %s); using 0",
            now_unix,
            anchor,
            elapsed,
        )
        elapsed = 0
    return _align_to_rotation_flexible(elapsed, rotation_period=rotation_period)


def build_heuristic_prf_input(
    counter: int,
    *,
    rotation_period: int,
) -> bytes:
    """Build PRF input for heuristic EID generation with flexible rotation period.

    This function constructs a Table 10-like PRF input buffer but uses the
    rotation period directly in the exponent field, allowing non-power-of-2
    periods to be encoded.

    For compatibility with the standard FHNA crypto, we still use the Table 10
    structure but encode the rotation-aligned counter directly.

    Args:
        counter: The rotation-aligned counter value.
        rotation_period: Rotation period in seconds (for logging/diagnostics).

    Returns:
        32-byte PRF input buffer.
    """
    # Normalize counter to u32 range
    counter_u32 = counter & FHNA_COUNTER_MASK
    counter_bytes = counter_u32.to_bytes(4, byteorder="big", signed=False)

    # Compute an effective K value for the PRF input structure
    # For standard periods, use log2; for others, use FHNA_K as default
    effective_k = FHNA_K
    if rotation_period == ROTATION_PERIOD:
        effective_k = FHNA_K  # 1024 = 2^10
    elif rotation_period == ROTATION_PERIOD_900:
        # 900 is not a power of 2; use a marker value
        # The crypto still works because the counter is already aligned
        effective_k = 9  # Approximation: 2^9 = 512 < 900 < 1024 = 2^10
    elif rotation_period == ROTATION_PERIOD_3600:
        effective_k = 12  # Approximation: 2^12 = 4096 > 3600

    block = bytearray(FHNA_PRF_INPUT_LENGTH)
    block[0:11] = b"\xff" * 11
    block[11] = effective_k
    block[12:16] = counter_bytes
    block[16:27] = b"\x00" * 11
    block[27] = effective_k
    block[28:32] = counter_bytes

    return bytes(block)


def _generate_heuristic_eid_single(
    eik: bytes,
    counter: int,
    variant: EidVariant,
) -> bytes:
    """Generate a single EID using the heuristic counter.

    This is a lightweight wrapper around the scalar derivation that uses
    an already-computed counter value.
    """
    if len(eik) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")

    # Build PRF input with the pre-computed counter
    # For heuristic mode, we use the counter directly as the time value
    prf_input = build_heuristic_prf_input(counter, rotation_period=ROTATION_PERIOD)
    r_dash = prf_aes_256_ecb(eik, prf_input)

    match variant:
        case EidVariant.LEGACY_SECP160R1_X20_BE:
            curve_order = int(_get_curve().order)
            r_dash_int = int.from_bytes(r_dash, byteorder="big", signed=False)
            scalar = r_dash_int % curve_order
            return _serialize_legacy_x(scalar)

        case EidVariant.MODERN_P256_X32_BE:
            r_dash_int = int.from_bytes(r_dash, byteorder="big", signed=False)
            scalar = (r_dash_int % (P256_ORDER - 1)) + 1
            return _serialize_p256_x(scalar)

        case EidVariant.MODERN_P256_X20_TRUNC_BE:
            full = _generate_heuristic_eid_single(
                eik, counter, EidVariant.MODERN_P256_X32_BE
            )
            return full[:LEGACY_EID_LENGTH]

        case EidVariant.MODERN_P256_X32_LE_SCALAR:
            r_dash_int = int.from_bytes(r_dash, byteorder="little", signed=False)
            scalar = (r_dash_int % (P256_ORDER - 1)) + 1
            return _serialize_p256_x(scalar)

        case EidVariant.MODERN_P256_X20_TRUNC_LE:
            full = _generate_heuristic_eid_single(
                eik, counter, EidVariant.MODERN_P256_X32_LE_SCALAR
            )
            return full[:LEGACY_EID_LENGTH]

        case _:
            raise ValueError(f"Unsupported EID variant: {variant}")


def generate_heuristic_eid(  # noqa: PLR0913
    eik: bytes,
    now_unix: int,
    *,
    rotation_period: int,
    basis: HeuristicBasis,
    variant: EidVariant,
    anchor: int | None = None,
    drift_offsets: tuple[int, ...] = (-1, 0, 1),
) -> list[HeuristicEidResult]:
    """Generate EID candidates using heuristic parameters for phone discovery.

    This function supports flexible rotation periods (900s, 1024s, 3600s) and
    both absolute and relative time bases, allowing discovery of Android phones
    that don't follow the standard FMDN tracker protocol.

    Args:
        eik: 32-byte Ephemeral Identity Key.
        now_unix: Current Unix timestamp in seconds.
        rotation_period: Rotation period in seconds (e.g., 900, 1024, 3600).
        basis: Time basis mode (ABSOLUTE or RELATIVE).
        variant: EID variant to generate.
        anchor: Anchor timestamp for RELATIVE basis (required if basis=RELATIVE).
        drift_offsets: Counter offsets to check for clock skew (default: -1, 0, +1).

    Returns:
        List of HeuristicEidResult objects for each drift offset, containing
        both the normal and reversed EID bytes.
    """
    if len(eik) != EIK_LENGTH:
        raise ValueError(f"Ephemeral Identity Key must be {EIK_LENGTH} bytes")

    base_counter = _compute_heuristic_counter(
        now_unix,
        rotation_period=rotation_period,
        basis=basis,
        anchor=anchor,
    )

    results: list[HeuristicEidResult] = []

    for drift in drift_offsets:
        counter = base_counter + (drift * rotation_period)
        if counter < 0:
            continue

        try:
            eid_bytes = _generate_heuristic_eid_single(eik, counter, variant)
        except Exception as exc:
            _LOGGER.debug(
                "Heuristic EID generation failed: period=%s basis=%s drift=%s: %s",
                rotation_period,
                basis.value,
                drift,
                exc,
            )
            continue

        # Add normal orientation
        results.append(
            HeuristicEidResult(
                eid_bytes=eid_bytes,
                rotation_period=rotation_period,
                basis=basis,
                variant=variant,
                counter=counter,
                drift_offset=drift,
                is_reversed=False,
            )
        )

        # Add reversed orientation (some devices advertise bytes in reverse)
        results.append(
            HeuristicEidResult(
                eid_bytes=eid_bytes[::-1],
                rotation_period=rotation_period,
                basis=basis,
                variant=variant,
                counter=counter,
                drift_offset=drift,
                is_reversed=True,
            )
        )

    return results


if __name__ == "__main__":
    # Developer test harness for EID generation.
    # Usage: python -m custom_components.googlefindmy.FMDNCrypto.eid_generator
    from custom_components.googlefindmy.example_data_provider import get_example_data

    sample_eik = bytes.fromhex(get_example_data("sample_identity_key"))

    print("EID Generation Demo (FHN Accessory Specification v1.3 — Table 10)")
    print("=" * 70)
    print(f"EIK (hex): {sample_eik.hex()}")
    print(f"Rotation period: {ROTATION_PERIOD} seconds")
    print()

    for variant in EidVariant:
        print(f"Variant: {variant.value}")
        for i in range(3):
            timestamp = i * ROTATION_PERIOD
            eid = generate_eid_variant(sample_eik, timestamp, variant)
            print(f"  t={timestamp:>6}: {eid.hex()}")
        print()

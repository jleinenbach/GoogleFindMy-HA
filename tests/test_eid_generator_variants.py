# tests/test_eid_generator_variants.py
"""Invariant and golden-vector coverage for FHNA EID derivation."""

from __future__ import annotations

import logging

import pytest

from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import load_curve
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    FHNA_K,
    FHNA_PRF_INPUT_LENGTH,
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    P256_ORDER,
    ROTATION_PERIOD,
    EidVariant,
    build_table10_prf_input,
    generate_eid_variant,
    get_masked_counter,
    prf_aes_256_ecb,
)

SAMPLE_EIK = bytes.fromhex(
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
SAMPLE_COUNTER = 0x12345678

PRF_INPUT_HEX = "ffffffffffffffffffffff0a1234540000000000000000000000000a12345400"
PRF_OUTPUT_HEX = "52c746bf4ab7c7c35f0ddb3b2c8632d129f0a0453f76767a29f033d00dee96ba"

GOLDEN_VECTORS: dict[EidVariant, str] = {
    EidVariant.LEGACY_SECP160R1_X20_BE: "86add72d6e7dea8bbdecb5dc8a5c3fa90e3eb6cc",
    EidVariant.MODERN_P256_X32_BE: "72d4e4be2c6f3c1c5328f10d884ab58e0a474b06584d9c893b1e099853289cd9",
    EidVariant.MODERN_P256_X20_TRUNC_BE: "72d4e4be2c6f3c1c5328f10d884ab58e0a474b06",
    EidVariant.MODERN_P256_X32_LE_SCALAR: "acc9009d7630b76c89cfb17f126fe640508ba7e184528341cbdb217053dd4050",
}


def test_table10_block_layout_and_masking() -> None:
    """Table 10 block should embed masked counters and sentinels."""

    block = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    masked_bytes = get_masked_counter(SAMPLE_COUNTER, FHNA_K)

    assert len(block) == FHNA_PRF_INPUT_LENGTH
    assert block[0:11] == b"\xff" * 11
    assert block[11] == FHNA_K
    assert block[12:16] == masked_bytes
    assert block[16:27] == b"\x00" * 11
    assert block[27] == FHNA_K
    assert block[28:32] == masked_bytes


def test_rotation_mask_equivalence_within_period() -> None:
    """Counters in the same rotation window should produce identical PRF input."""

    base = 2 * ROTATION_PERIOD + 5
    aligned = build_table10_prf_input(base, k=FHNA_K)
    aligned_neighbor = build_table10_prf_input(base + 1, k=FHNA_K)
    next_window = build_table10_prf_input(base + ROTATION_PERIOD, k=FHNA_K)

    assert aligned == aligned_neighbor
    assert aligned != next_window


def test_prf_is_deterministic_and_matches_golden_vector() -> None:
    """AES-256-ECB PRF should be deterministic for the same inputs."""

    prf_input = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    assert prf_input.hex() == PRF_INPUT_HEX

    first = prf_aes_256_ecb(SAMPLE_EIK, prf_input)
    second = prf_aes_256_ecb(SAMPLE_EIK, prf_input)

    assert first == second
    assert first.hex() == PRF_OUTPUT_HEX


def test_scalar_derivation_respects_curve_ranges() -> None:
    """Derived scalars must remain within the open interval (0, order)."""

    prf_input = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    prf_output = prf_aes_256_ecb(SAMPLE_EIK, prf_input)
    prf_int = int.from_bytes(prf_output, "big", signed=False)

    legacy_curve = load_curve()
    legacy_scalar = (prf_int % (int(legacy_curve.order) - 1)) + 1
    assert 1 <= legacy_scalar < int(legacy_curve.order)

    modern_scalar = (prf_int % (P256_ORDER - 1)) + 1
    assert 1 <= modern_scalar < P256_ORDER


def test_generate_eid_variants_match_golden_vectors() -> None:
    """Each variant should emit deterministic outputs with explicit lengths."""

    for variant, expected_hex in GOLDEN_VECTORS.items():
        eid = generate_eid_variant(SAMPLE_EIK, SAMPLE_COUNTER, variant)
        assert eid.hex() == expected_hex

        if variant is EidVariant.LEGACY_SECP160R1_X20_BE or variant is EidVariant.MODERN_P256_X20_TRUNC_BE:
            assert len(eid) == LEGACY_EID_LENGTH
        else:
            assert len(eid) == MODERN_EID_LENGTH


def test_lenient_normalization_masks_out_of_range(caplog: pytest.LogCaptureFixture) -> None:
    """Lenient normalization should mask oversized counters to u32 without failure."""

    oversize = (1 << 40) + SAMPLE_COUNTER
    masked = oversize & FHNA_COUNTER_MASK

    with caplog.at_level(logging.DEBUG):
        eid = generate_eid_variant(
            SAMPLE_EIK,
            oversize,
            EidVariant.MODERN_P256_X32_BE,
            strict=False,
        )

    expected = generate_eid_variant(
        SAMPLE_EIK,
        masked,
        EidVariant.MODERN_P256_X32_BE,
    )
    assert eid == expected
    assert "masking" in caplog.text


def test_strict_normalization_rejects_negative() -> None:
    """Strict normalization should reject invalid counters."""

    with pytest.raises(ValueError):
        generate_eid_variant(
            SAMPLE_EIK,
            -5,
            EidVariant.LEGACY_SECP160R1_X20_BE,
            strict=True,
        )

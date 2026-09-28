# tests/test_eid_generator_variants.py
"""Invariant and golden-vector coverage for FHNA EID derivation."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import pytest

from custom_components.googlefindmy.FMDNCrypto import curve_profile
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import load_curve
from custom_components.googlefindmy.FMDNCrypto.curve_profile import ScalarRule
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    FHNA_K,
    FHNA_PRF_INPUT_LENGTH,
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    P256_ORDER,
    ROTATION_PERIOD,
    ROTATION_PERIOD_900,
    ROTATION_PERIOD_3600,
    VARIANT_DERIVATIONS,
    EidVariant,
    HeuristicBasis,
    HeuristicEidResult,
    _align_to_rotation_flexible,
    _compute_heuristic_counter,
    _generate_heuristic_eid_single,
    build_heuristic_prf_input,
    build_table10_prf_input,
    compute_flags_xor_mask,
    generate_eid,
    generate_eid_variant,
    generate_heuristic_eid,
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
    EidVariant.LEGACY_SECP160R1_X20_BE: "7bf149821dafae98259bfe53a87283c41d7b1b1c",
    EidVariant.MODERN_P256_X32_BE: "72d4e4be2c6f3c1c5328f10d884ab58e0a474b06584d9c893b1e099853289cd9",
    EidVariant.MODERN_P256_X20_TRUNC_BE: "72d4e4be2c6f3c1c5328f10d884ab58e0a474b06",
    EidVariant.MODERN_P256_X32_LE_SCALAR: "acc9009d7630b76c89cfb17f126fe640508ba7e184528341cbdb217053dd4050",
    EidVariant.MODERN_P256_X20_TRUNC_LE: "acc9009d7630b76c89cfb17f126fe640508ba7e1",
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
    """Derived scalars must stay within each variant's expected interval."""

    prf_input = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    prf_output = prf_aes_256_ecb(SAMPLE_EIK, prf_input)
    prf_int = int.from_bytes(prf_output, "big", signed=False)

    legacy_curve = load_curve()
    legacy_order: int = int(legacy_curve.order)
    legacy_scalar = prf_int % legacy_order
    assert 0 <= legacy_scalar < legacy_order

    modern_scalar = (prf_int % (P256_ORDER - 1)) + 1
    assert 1 <= modern_scalar < P256_ORDER
    assert legacy_scalar != modern_scalar


def test_legacy_scalar_reduction_rejects_p256_projection() -> None:
    """Legacy EIDs must use modulo-n reduction, not the modern (n-1)+1 projection.

    This regression test is intentionally verbose so a future AI-assisted edit
    can diagnose the failure: if the assertion below flips, it means the legacy
    branch started using the P-256 scalar projection, which shifts every
    derived key by one and produces invalid EIDs for SECP160R1 accessories.
    """

    prf_input = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    prf_output = prf_aes_256_ecb(SAMPLE_EIK, prf_input)
    prf_int = int.from_bytes(prf_output, "big", signed=False)

    legacy_curve = load_curve()
    legacy_order: int = int(legacy_curve.order)
    mod_n_scalar = prf_int % legacy_order
    p256_projection_scalar = (prf_int % (legacy_order - 1)) + 1

    mod_n_x_int = int((mod_n_scalar * legacy_curve.generator).x())
    projected_x_int = int((p256_projection_scalar * legacy_curve.generator).x())

    legacy_eid = generate_eid_variant(
        SAMPLE_EIK,
        SAMPLE_COUNTER,
        EidVariant.LEGACY_SECP160R1_X20_BE,
    )

    assert legacy_eid == mod_n_x_int.to_bytes(LEGACY_EID_LENGTH, "big"), (
        "Legacy EID derivation must keep the modulo-n scalar; using the P-256 "
        "projection ((r % (n-1)) + 1) would shift the scalar and break the "
        "derived SECP160R1 EIDs."
    )
    assert mod_n_x_int != projected_x_int


def test_generate_eid_variants_match_golden_vectors() -> None:
    """Each variant should emit deterministic outputs with explicit lengths."""

    for variant, expected_hex in GOLDEN_VECTORS.items():
        eid = generate_eid_variant(SAMPLE_EIK, SAMPLE_COUNTER, variant)
        assert eid.hex() == expected_hex

        if variant in (
            EidVariant.LEGACY_SECP160R1_X20_BE,
            EidVariant.MODERN_P256_X20_TRUNC_BE,
            EidVariant.MODERN_P256_X20_TRUNC_LE,
        ):
            assert len(eid) == LEGACY_EID_LENGTH
        else:
            assert len(eid) == MODERN_EID_LENGTH


def test_lenient_normalization_masks_out_of_range(
    caplog: pytest.LogCaptureFixture,
) -> None:
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


# =============================================================================
# Audit coverage (AP10 Teil C): guards, flags mask, heuristic derivation.
# Each test asserts behavior (independent recomputation for crypto paths), not
# mere line execution, so a regression flips the assertion rather than silently
# keeping coverage green.
# =============================================================================


def test_normalize_rejects_bool_and_non_int() -> None:
    """The u32 counter must reject bool and non-int types (bool is not a counter)."""

    with pytest.raises(TypeError, match="must be int"):
        build_table10_prf_input(True, k=FHNA_K)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be int"):
        build_table10_prf_input("0x10", k=FHNA_K)  # type: ignore[arg-type]


def test_build_table10_rejects_wrong_rotation_exponent() -> None:
    """Only FHNA_K is a valid rotation exponent; any other k must raise."""

    with pytest.raises(ValueError, match="rotation exponent"):
        build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K + 1)


def test_build_table10_aligned_counter_skips_mask_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An already rotation-aligned counter must not trigger the masking debug log."""

    aligned = 2 * ROTATION_PERIOD  # low K bits already zero
    with caplog.at_level(logging.DEBUG):
        block = build_table10_prf_input(aligned, k=FHNA_K)

    assert len(block) == FHNA_PRF_INPUT_LENGTH
    assert "masked to rotation-aligned" not in caplog.text


def test_prf_rejects_wrong_key_length() -> None:
    """The AES-256 PRF requires a 32-byte EIK."""

    prf_input = build_table10_prf_input(SAMPLE_COUNTER, k=FHNA_K)
    with pytest.raises(ValueError, match="Identity Key"):
        prf_aes_256_ecb(SAMPLE_EIK[:16], prf_input)


def test_prf_rejects_wrong_input_length() -> None:
    """The PRF input buffer must be exactly the Table 10 length."""

    with pytest.raises(ValueError, match="PRF input"):
        prf_aes_256_ecb(SAMPLE_EIK, b"\x00" * (FHNA_PRF_INPUT_LENGTH - 1))


def test_generate_variant_rejects_wrong_key_length() -> None:
    """generate_eid_variant must reject a short EIK before any derivation."""

    with pytest.raises(ValueError, match="Identity Key"):
        generate_eid_variant(
            SAMPLE_EIK[:8], SAMPLE_COUNTER, EidVariant.MODERN_P256_X32_BE
        )


def test_generate_variant_reports_unknown_variant_before_the_prf() -> None:
    """An unknown variant is named even when ``k`` would also be rejected."""

    with pytest.raises(ValueError, match="Unsupported EID variant"):
        generate_eid_variant(SAMPLE_EIK, 0, "bogus_variant", k=9)  # type: ignore[arg-type]


def test_generate_variant_rejects_unknown_variant() -> None:
    """An unsupported variant token must hit the explicit guard, not derive silently."""

    with pytest.raises(ValueError, match="Unsupported EID variant"):
        generate_eid_variant(SAMPLE_EIK, SAMPLE_COUNTER, "bogus_variant")  # type: ignore[arg-type]


def test_get_masked_counter_rejects_wrong_rotation_exponent() -> None:
    """get_masked_counter shares the FHNA_K precondition with the PRF builder."""

    with pytest.raises(ValueError, match="rotation exponent"):
        get_masked_counter(SAMPLE_COUNTER, FHNA_K + 2)


def _independent_flags_mask(eik: bytes, counter: int, byte_len: int, order: int) -> int:
    """Recompute the flags XOR mask from primitives, independently of the SUT."""

    import hashlib

    prf_input = build_table10_prf_input(counter, k=FHNA_K, strict=False)
    r_dash = prf_aes_256_ecb(eik, prf_input)
    r_int = int.from_bytes(r_dash, "big", signed=False)
    r_scalar = r_int % order
    r_bytes = r_scalar.to_bytes(byte_len, "big")
    return hashlib.sha256(r_bytes).digest()[-1]


def test_compute_flags_xor_mask_legacy_matches_recomputation() -> None:
    """Legacy (secp160r1) flags mask must equal an independent primitive recomputation."""

    legacy_order = int(load_curve().order)
    expected = _independent_flags_mask(
        SAMPLE_EIK, SAMPLE_COUNTER, LEGACY_EID_LENGTH, legacy_order
    )
    actual = compute_flags_xor_mask(SAMPLE_EIK, SAMPLE_COUNTER)

    assert actual == expected
    assert 0 <= actual <= 0xFF


def test_compute_flags_xor_mask_p256_branch_uses_supplied_order() -> None:
    """The P-256 branch must reduce mod P256_ORDER and pad to 32 bytes.

    The independent recomputation (``expected``) is the real branch proof: if
    the function ignored ``curve_byte_len``/``curve_order``, ``actual`` would
    diverge from the P-256 oracle. The legacy comparison is an additional
    collapse guard (a different order/padding must yield a different mask).
    """

    expected = _independent_flags_mask(
        SAMPLE_EIK, SAMPLE_COUNTER, MODERN_EID_LENGTH, P256_ORDER
    )
    actual = compute_flags_xor_mask(
        SAMPLE_EIK,
        SAMPLE_COUNTER,
        curve_byte_len=MODERN_EID_LENGTH,
        curve_order=P256_ORDER,
    )
    legacy = compute_flags_xor_mask(SAMPLE_EIK, SAMPLE_COUNTER)

    assert actual == expected
    # Distinct curve order/padding must produce a distinct mask for these fixed
    # inputs (legacy=213, p256=16): guards against a branch collapse that would
    # ignore the supplied P-256 parameters.
    assert actual != legacy


def test_generate_eid_shim_warns_and_matches_variant() -> None:
    """The deprecated generate_eid shim must warn and equal generate_eid_variant."""

    with pytest.warns(DeprecationWarning, match="deprecated"):
        shimmed = generate_eid(
            SAMPLE_EIK, SAMPLE_COUNTER, variant=EidVariant.MODERN_P256_X32_BE
        )

    direct = generate_eid_variant(
        SAMPLE_EIK, SAMPLE_COUNTER, EidVariant.MODERN_P256_X32_BE
    )
    assert shimmed == direct


def test_align_flexible_rejects_nonpositive_period() -> None:
    """Flexible alignment must reject non-positive rotation periods."""

    with pytest.raises(ValueError, match="rotation_period must be positive"):
        _align_to_rotation_flexible(1000, rotation_period=0)


def test_align_flexible_floors_to_window_start() -> None:
    """Flexible alignment floors a timestamp to the start of its rotation window."""

    assert _align_to_rotation_flexible(1000, rotation_period=900) == 900
    assert _align_to_rotation_flexible(1800, rotation_period=900) == 1800
    assert _align_to_rotation_flexible(3601, rotation_period=3600) == 3600


def test_heuristic_counter_absolute_uses_now() -> None:
    """ABSOLUTE basis floors absolute now_unix and ignores any anchor."""

    counter = _compute_heuristic_counter(
        10_000, rotation_period=900, basis=HeuristicBasis.ABSOLUTE
    )
    assert counter == (10_000 // 900) * 900


def test_heuristic_counter_relative_requires_anchor() -> None:
    """RELATIVE basis without a valid anchor must raise."""

    with pytest.raises(ValueError, match="RELATIVE basis requires"):
        _compute_heuristic_counter(
            10_000, rotation_period=900, basis=HeuristicBasis.RELATIVE, anchor=None
        )
    with pytest.raises(ValueError, match="RELATIVE basis requires"):
        _compute_heuristic_counter(
            10_000, rotation_period=900, basis=HeuristicBasis.RELATIVE, anchor=0
        )


def test_heuristic_counter_relative_clamps_negative_elapsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A now_unix before the anchor clamps elapsed to 0 (counter 0), with a debug log."""

    with caplog.at_level(logging.DEBUG):
        counter = _compute_heuristic_counter(
            500,
            rotation_period=900,
            basis=HeuristicBasis.RELATIVE,
            anchor=1000,
        )
    assert counter == 0
    assert "Negative elapsed time" in caplog.text


def test_heuristic_counter_relative_valid_elapsed() -> None:
    """RELATIVE basis floors (now - anchor) to the rotation window."""

    counter = _compute_heuristic_counter(
        5000,
        rotation_period=900,
        basis=HeuristicBasis.RELATIVE,
        anchor=1000,
    )
    assert counter == ((5000 - 1000) // 900) * 900


def test_build_heuristic_prf_input_effective_k_per_period() -> None:
    """The effective-k marker byte must reflect the rotation period branch."""

    counter = 2 * ROTATION_PERIOD
    counter_bytes = (counter & FHNA_COUNTER_MASK).to_bytes(4, "big")

    cases = {
        ROTATION_PERIOD_900: 9,
        ROTATION_PERIOD_3600: 12,
        ROTATION_PERIOD: FHNA_K,
        500: FHNA_K,  # non-standard period falls through to the FHNA_K default
    }
    for period, expected_k in cases.items():
        block = build_heuristic_prf_input(counter, rotation_period=period)
        assert len(block) == FHNA_PRF_INPUT_LENGTH
        assert block[0:11] == b"\xff" * 11
        assert block[11] == expected_k
        assert block[12:16] == counter_bytes
        assert block[16:27] == b"\x00" * 11
        assert block[27] == expected_k
        assert block[28:32] == counter_bytes


def test_heuristic_single_rejects_wrong_key_length() -> None:
    """The single-EID heuristic helper must reject a short EIK."""

    with pytest.raises(ValueError, match="Identity Key"):
        _generate_heuristic_eid_single(SAMPLE_EIK[:4], 0, EidVariant.MODERN_P256_X32_BE)


def test_heuristic_single_rejects_unknown_variant() -> None:
    """An unsupported variant reaches the explicit guard in the heuristic helper."""

    with pytest.raises(ValueError, match="Unsupported EID variant"):
        _generate_heuristic_eid_single(SAMPLE_EIK, 0, "bogus_variant")  # type: ignore[arg-type]


def test_heuristic_single_all_variants_have_expected_lengths() -> None:
    """Each variant emits a deterministic EID with the documented byte length."""

    counter = 3 * ROTATION_PERIOD
    for variant in EidVariant:
        first = _generate_heuristic_eid_single(SAMPLE_EIK, counter, variant)
        second = _generate_heuristic_eid_single(SAMPLE_EIK, counter, variant)
        assert first == second  # deterministic
        if variant in (
            EidVariant.LEGACY_SECP160R1_X20_BE,
            EidVariant.MODERN_P256_X20_TRUNC_BE,
            EidVariant.MODERN_P256_X20_TRUNC_LE,
        ):
            assert len(first) == LEGACY_EID_LENGTH
        else:
            assert len(first) == MODERN_EID_LENGTH


def test_heuristic_single_legacy_matches_primitive_recomputation() -> None:
    """The legacy heuristic EID equals R=r*G x-coordinate from an independent scalar."""

    counter = 3 * ROTATION_PERIOD
    prf_input = build_heuristic_prf_input(counter, rotation_period=ROTATION_PERIOD)
    r_dash = prf_aes_256_ecb(SAMPLE_EIK, prf_input)
    legacy_curve = load_curve()
    order = int(legacy_curve.order)
    scalar = int.from_bytes(r_dash, "big", signed=False) % order
    expected_x = int((scalar * legacy_curve.generator).x())
    expected = expected_x.to_bytes(LEGACY_EID_LENGTH, "big")

    actual = _generate_heuristic_eid_single(
        SAMPLE_EIK, counter, EidVariant.LEGACY_SECP160R1_X20_BE
    )
    assert actual == expected


def test_generate_heuristic_eid_rejects_wrong_key_length() -> None:
    """generate_heuristic_eid must reject a short EIK before any derivation."""

    with pytest.raises(ValueError, match="Identity Key"):
        generate_heuristic_eid(
            SAMPLE_EIK[:2],
            10_000,
            rotation_period=900,
            basis=HeuristicBasis.ABSOLUTE,
            variant=EidVariant.MODERN_P256_X32_BE,
        )


def test_generate_heuristic_eid_emits_normal_and_reversed_per_drift() -> None:
    """Each drift offset yields a normal result and its byte-reversed twin."""

    results = generate_heuristic_eid(
        SAMPLE_EIK,
        100_000,
        rotation_period=900,
        basis=HeuristicBasis.ABSOLUTE,
        variant=EidVariant.MODERN_P256_X32_BE,
        drift_offsets=(-1, 0, 1),
    )
    assert len(results) == 6  # 3 drifts x {normal, reversed}
    assert all(isinstance(r, HeuristicEidResult) for r in results)

    normals = [r for r in results if not r.is_reversed]
    reverseds = [r for r in results if r.is_reversed]
    assert len(normals) == len(reverseds) == 3
    for normal, reversed_ in zip(normals, reverseds, strict=True):
        assert reversed_.eid_bytes == normal.eid_bytes[::-1]
        assert normal.basis is HeuristicBasis.ABSOLUTE
        assert normal.rotation_period == 900


def test_generate_heuristic_eid_skips_negative_counter() -> None:
    """Drift offsets that push the counter below zero are skipped (no result)."""

    results = generate_heuristic_eid(
        SAMPLE_EIK,
        0,
        rotation_period=900,
        basis=HeuristicBasis.ABSOLUTE,
        variant=EidVariant.MODERN_P256_X32_BE,
        drift_offsets=(-1,),
    )
    assert results == []


def test_generate_heuristic_eid_swallows_derivation_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A per-drift derivation failure is logged and skipped, not propagated."""

    with caplog.at_level(logging.DEBUG):
        results = generate_heuristic_eid(
            SAMPLE_EIK,
            100_000,
            rotation_period=900,
            basis=HeuristicBasis.ABSOLUTE,
            variant="bogus_variant",  # type: ignore[arg-type]
            drift_offsets=(0,),
        )
    assert results == []
    assert "Heuristic EID generation failed" in caplog.text


# Heuristic EIDs pinned per variant (characterization taken on commit
# 6f6243bc, before the derivation table replaced the per-variant formulas).
# The heuristic PRF input differs from Table 10, so these differ from
# GOLDEN_VECTORS. One entry per variant; new variants add their own entry.
HEURISTIC_NOW_UNIX = 1_700_000_000
HEURISTIC_GOLDEN_VECTORS: dict[EidVariant, str] = {
    EidVariant.LEGACY_SECP160R1_X20_BE: "97754e8f9b5cfa37bd2a120a252a754db1c21650",
    EidVariant.MODERN_P256_X32_BE: "a7d711fc0f8760abf4ae3444d5c56122fc74a4f76e0bd033af186102d627a839",
    EidVariant.MODERN_P256_X20_TRUNC_BE: "a7d711fc0f8760abf4ae3444d5c56122fc74a4f7",
    EidVariant.MODERN_P256_X32_LE_SCALAR: "383cec434412038ca6c92ed44d377c8481c10136d5c09edf27c546d1ebb65d5e",
    EidVariant.MODERN_P256_X20_TRUNC_LE: "383cec434412038ca6c92ed44d377c8481c10136",
}


@pytest.mark.parametrize(
    ("variant", "expected_hex"), list(HEURISTIC_GOLDEN_VECTORS.items())
)
def test_heuristic_golden_vectors(variant: EidVariant, expected_hex: str) -> None:
    """The heuristic path emits the pinned EID bytes for every variant."""

    results = [
        result
        for result in generate_heuristic_eid(
            SAMPLE_EIK,
            HEURISTIC_NOW_UNIX,
            rotation_period=ROTATION_PERIOD_900,
            basis=HeuristicBasis.ABSOLUTE,
            variant=variant,
            drift_offsets=(0,),
        )
        if not result.is_reversed
    ]
    assert len(results) == 1
    assert results[0].eid_bytes.hex() == expected_hex


def test_variant_derivations_cover_every_variant() -> None:
    """Every variant, including ones a resolver lock may name, has one entry."""

    assert set(VARIANT_DERIVATIONS) == set(EidVariant)


def test_variant_derivations_are_read_only() -> None:
    """The table cannot be changed at runtime."""

    with pytest.raises(TypeError):
        VARIANT_DERIVATIONS[EidVariant.MODERN_P256_X32_BE] = (  # type: ignore[index]
            VARIANT_DERIVATIONS[EidVariant.LEGACY_SECP160R1_X20_BE]
        )


_P256_VARIANTS: tuple[EidVariant, ...] = (
    EidVariant.MODERN_P256_X32_BE,
    EidVariant.MODERN_P256_X20_TRUNC_BE,
    EidVariant.MODERN_P256_X32_LE_SCALAR,
    EidVariant.MODERN_P256_X20_TRUNC_LE,
)
_EXPECTED_RULE: dict[EidVariant, ScalarRule] = {
    EidVariant.LEGACY_SECP160R1_X20_BE: ScalarRule.MOD_N,
    **dict.fromkeys(_P256_VARIANTS, ScalarRule.PLUS_ONE),
}


def _scalar_paths() -> list[tuple[str, Callable[[], object], ScalarRule]]:
    """Return every public scalar-deriving path with the rule it must apply."""

    paths: list[tuple[str, Callable[[], object], ScalarRule]] = []
    for variant, rule in _EXPECTED_RULE.items():
        paths.append(
            (
                f"generate_eid_variant[{variant.value}]",
                lambda v=variant: generate_eid_variant(SAMPLE_EIK, SAMPLE_COUNTER, v),
                rule,
            )
        )
        paths.append(
            (
                f"generate_heuristic_eid[{variant.value}]",
                lambda v=variant: generate_heuristic_eid(
                    SAMPLE_EIK,
                    100_000,
                    rotation_period=900,
                    basis=HeuristicBasis.ABSOLUTE,
                    variant=v,
                    drift_offsets=(0,),
                ),
                rule,
            )
        )
    paths.append(
        (
            "compute_flags_xor_mask[secp160r1]",
            lambda: compute_flags_xor_mask(SAMPLE_EIK, SAMPLE_COUNTER),
            ScalarRule.MOD_N,
        )
    )
    paths.append(
        (
            "compute_flags_xor_mask[p256]",
            lambda: compute_flags_xor_mask(
                SAMPLE_EIK,
                SAMPLE_COUNTER,
                curve_byte_len=MODERN_EID_LENGTH,
                curve_order=P256_ORDER,
            ),
            ScalarRule.MOD_N,
        )
    )
    return paths


def test_scalar_sites_route_through_reduce_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every public scalar path reduces ``r'`` through ``reduce_scalar``.

    The spy replaces ``reduce_scalar`` in the namespace of each loaded module
    that imported it, so a caller that spells out its own formula again is
    caught by behaviour, not by a text search. The recorded rule pins the
    unchanged semantics: ``MOD_N`` for legacy EIDs and the flags mask,
    ``PLUS_ONE`` for the persisted ``MODERN_P256_*`` variants.

    Known limits: a caller that calls ``reduce_scalar`` but discards the result
    still passes, and a caller that reaches the function through the module
    attribute ``curve_profile.reduce_scalar`` is reported as a bypass.
    """

    original = curve_profile.reduce_scalar
    calls: list[ScalarRule] = []

    def spy(r_dash_int: int, order: int, rule: ScalarRule) -> int:
        calls.append(ScalarRule(rule))
        return original(r_dash_int, order, rule)

    importers = [
        name
        for name, module in list(sys.modules.items())
        if name.startswith("custom_components.googlefindmy")
        and module is not curve_profile
        and getattr(module, "reduce_scalar", None) is original
    ]
    assert "custom_components.googlefindmy.FMDNCrypto.eid_generator" in importers
    for name in importers:
        monkeypatch.setattr(sys.modules[name], "reduce_scalar", spy)

    for label, call, expected_rule in _scalar_paths():
        calls.clear()
        call()
        assert len(calls) >= 1, f"{label} bypassed reduce_scalar"
        assert set(calls) == {expected_rule}, f"{label} used {calls}"

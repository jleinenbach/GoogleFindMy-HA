import logging

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.FMDNCrypto import eid_generator
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import load_curve
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_K,
    FHNA_PRF_INPUT_LENGTH,
    FHNA_ROTATION_MASK,
    FHNA_TIMESTAMP_MASK,
    ROTATION_PERIOD,
    _normalize_ts_u32,
    _prf_table10,
    fhna_build_prf_input,
    generate_eid_p256,
    generate_eid_p256_le,
)


def test_prf_table10_returns_32_bytes_and_is_deterministic() -> None:
    identity_key = bytes(range(32))
    timestamp = 1234

    first = _prf_table10(identity_key, timestamp)
    second = _prf_table10(identity_key, timestamp)

    assert len(first) == 32
    assert first == second


def test_normalize_timestamp_rejects_bool_and_out_of_range() -> None:
    """Raw timestamps must be real integers within the uint32 window."""

    with pytest.raises(TypeError):
        _normalize_ts_u32(True)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        _normalize_ts_u32(-1)

    with pytest.raises(ValueError):
        _normalize_ts_u32(1 << 40)


def test_normalize_timestamp_warns_when_coercing(caplog: pytest.LogCaptureFixture) -> None:
    """Non-strict mode should warn before masking oversized timestamps."""

    oversize = 1 << 40
    with caplog.at_level(logging.WARNING):
        coerced = _normalize_ts_u32(oversize, strict=False)

    assert coerced == oversize & FHNA_TIMESTAMP_MASK
    assert "coercing via & 0xFFFFFFFF" in caplog.text


def test_fhna_build_prf_input_masks_timestamp_and_layout() -> None:
    timestamp = 0x12345678
    block = fhna_build_prf_input(timestamp)

    masked = (timestamp & FHNA_TIMESTAMP_MASK) & ~FHNA_ROTATION_MASK
    masked_bytes = masked.to_bytes(4, "big")

    assert len(block) == FHNA_PRF_INPUT_LENGTH
    assert block[0:11] == b"\xff" * 11
    assert block[11] == FHNA_K
    assert block[12:16] == masked_bytes
    assert block[16:27] == b"\x00" * 11
    assert block[27] == FHNA_K
    assert block[28:32] == masked_bytes


def test_generate_eid_p256_matches_known_x_coordinate() -> None:
    identity_key = bytes(range(32))
    timestamp = ROTATION_PERIOD

    eid = generate_eid_p256(identity_key, timestamp)

    # Expected value derived from the Table 10 PRF with masked timestamp for ROTATION_PERIOD.
    assert eid.hex() == (
        "20d4d2b42f4a485498f357ad3f0afaa8562264be5ef0f4a99aae07e4389f5490"
    )
    assert len(eid) == 32


def test_generate_eid_p256_rejects_invalid_key_lengths() -> None:
    with pytest.raises(ValueError):
        generate_eid_p256(b"short", 0)


def test_generate_eid_p256_le_applies_little_endian_scalar() -> None:
    identity_key = bytes(range(32))
    timestamp = ROTATION_PERIOD

    eid_be = generate_eid_p256(identity_key, timestamp)
    eid_le = generate_eid_p256_le(identity_key, timestamp)

    assert eid_le != eid_be
    assert eid_le.hex() == (
        "a5afbd3af2705ce30236c4098dcd2b9a78d90c4725bdff120fc42da1db8e69d9"
    )


def test_generate_eid_p256_le_rejects_invalid_key_lengths() -> None:
    with pytest.raises(ValueError):
        generate_eid_p256_le(b"short", 0)


def test_generate_eid_p256_avoids_zero_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero PRF output should still yield the generator point (r=1)."""

    identity_key = b"\xaa" * 32

    monkeypatch.setattr(
        eid_generator, "fhna_prf_aes_ecb_256", lambda *_args, **_kwargs: b"\x00" * 32
    )

    eid = generate_eid_p256(identity_key, 0)

    generator_point = (
        ec.derive_private_key(1, ec.SECP256R1()).public_key().public_numbers()
    )
    expected_x = int(generator_point.x).to_bytes(32, "big")

    assert eid == expected_x


def test_generate_eid_legacy_avoids_zero_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy path maps zero PRF output to the curve generator (r=1)."""

    identity_key = b"\xbb" * 32

    monkeypatch.setattr(
        eid_generator, "fhna_prf_aes_ecb_256", lambda *_args, **_kwargs: b"\x00" * 32
    )

    eid = eid_generator.generate_eid(identity_key, 0)

    curve = load_curve()
    expected_x = int(curve.generator.x()).to_bytes(20, "big")

    assert eid == expected_x

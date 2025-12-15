import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.FMDNCrypto import eid_generator
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import load_curve
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    _prf_table10,
    generate_eid_p256,
)


def test_prf_table10_returns_32_bytes_and_is_deterministic() -> None:
    identity_key = bytes(range(32))
    timestamp = 1234

    first = _prf_table10(identity_key, timestamp)
    second = _prf_table10(identity_key, timestamp)

    assert len(first) == 32
    assert first == second


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


def test_generate_eid_p256_avoids_zero_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero PRF output should still yield the generator point (r=1)."""

    identity_key = b"\xAA" * 32

    monkeypatch.setattr(
        eid_generator, "fhna_prf_aes_ecb_256", lambda *_args, **_kwargs: b"\x00" * 32
    )

    eid = generate_eid_p256(identity_key, 0)

    generator_point = ec.derive_private_key(1, ec.SECP256R1()).public_key().public_numbers()
    expected_x = int(generator_point.x).to_bytes(32, "big")

    assert eid == expected_x


def test_generate_eid_legacy_avoids_zero_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy path maps zero PRF output to the curve generator (r=1)."""

    identity_key = b"\xBB" * 32

    monkeypatch.setattr(
        eid_generator, "fhna_prf_aes_ecb_256", lambda *_args, **_kwargs: b"\x00" * 32
    )

    eid = eid_generator.generate_eid(identity_key, 0)

    curve = load_curve()
    expected_x = int(curve.generator.x()).to_bytes(20, "big")

    assert eid == expected_x

import pytest

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

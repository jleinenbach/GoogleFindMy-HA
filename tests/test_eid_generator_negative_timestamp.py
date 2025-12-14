"""EID generator resilience and determinism for negative timestamps."""

from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EIK_LENGTH,
    LEGACY_EID_LENGTH,
    generate_eid,
    generate_eid_p256,
)


def test_generate_eid_accepts_negative_timestamp() -> None:
    """Legacy EID generation should be deterministic for negative timestamps."""

    key = b"\x10" * EIK_LENGTH

    first = generate_eid(key, -1)
    second = generate_eid(key, -1)

    assert first == second
    assert len(first) == LEGACY_EID_LENGTH


def test_generate_eid_p256_accepts_negative_timestamp() -> None:
    """Modern EID generation must remain deterministic with negative inputs."""

    key = b"\x20" * EIK_LENGTH

    first = generate_eid_p256(key, -123)
    second = generate_eid_p256(key, -123)

    assert first == second
    assert len(first) == 32

"""EID generator validation for invalid timestamps."""

import pytest

from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EIK_LENGTH,
    generate_eid,
    generate_eid_p256,
)


def test_generate_eid_rejects_negative_timestamp() -> None:
    """Legacy EID generation should fail fast for negative timestamps."""

    key = b"\x10" * EIK_LENGTH

    with pytest.raises(ValueError):
        generate_eid(key, -1)


def test_generate_eid_p256_rejects_negative_timestamp() -> None:
    """Modern EID generation should reject negative timestamps by default."""

    key = b"\x20" * EIK_LENGTH

    with pytest.raises(ValueError):
        generate_eid_p256(key, -123)

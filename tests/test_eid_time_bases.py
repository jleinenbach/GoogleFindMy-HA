"""Unit tests for timestamp basis helpers used by the EID resolver."""

import pytest

from custom_components.googlefindmy.coordinator import _normalize_epoch_seconds
from custom_components.googlefindmy.eid_resolver import _clamp_and_mask_u32


def test_clamp_and_mask_u32_negative_does_not_wrap() -> None:
    """Negative deltas must clamp to zero before masking."""

    assert _clamp_and_mask_u32(-1) == 0
    assert _clamp_and_mask_u32(-999_999) == 0


def test_clamp_and_mask_u32_positive_masks() -> None:
    """Positive values should mask to uint32."""

    assert _clamp_and_mask_u32(1) == 1
    assert _clamp_and_mask_u32(0) == 0
    assert _clamp_and_mask_u32(0x1_0000_0001) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1749216525", 1_749_216_525),
        (1_749_216_525, 1_749_216_525),
        ("1749216525000", 1_749_216_525),
        (1_749_216_525_000, 1_749_216_525),
        (None, None),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_normalize_epoch_seconds(raw: object, expected: int | None) -> None:
    """Epoch normalization should handle seconds and millisecond inputs."""

    assert _normalize_epoch_seconds(raw) == expected

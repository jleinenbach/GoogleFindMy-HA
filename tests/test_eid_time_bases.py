"""Unit tests for timestamp basis helpers used by the EID resolver."""

import pytest

from custom_components.googlefindmy.coordinator import (
    DeviceIdentity,
    _normalize_epoch_seconds,
    normalize_epoch_seconds,
)
from custom_components.googlefindmy.eid_resolver import (
    TimebaseLabel,
    _build_timebase_candidates,
    _clamp_and_mask_u32,
)


def test_clamp_and_mask_u32_masks_negative_values() -> None:
    """Negative deltas wrap into the uint32 counter space."""

    assert _clamp_and_mask_u32(-1) == 0xFFFFFFFF
    assert _clamp_and_mask_u32(-999_999) == (-999_999 & 0xFFFFFFFF)


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


def test_normalize_epoch_seconds_handles_millisecond_inputs() -> None:
    """Millisecond epoch inputs normalize to seconds for all accepted types."""

    assert normalize_epoch_seconds(1_749_216_525_000) == 1_749_216_525
    assert normalize_epoch_seconds("1749216525000") == 1_749_216_525


def test_future_anchor_candidates_are_skipped() -> None:
    """Extremely future anchors fall back to ABSOLUTE without REL candidates."""

    now = 100
    identity = DeviceIdentity(
        registry_id="registry-future",
        canonical_id="can-future",
        identity_key=b"\x00" * 2,
        config_entry_id="entry-future",
        pair_date=now + 200_000,
    )

    candidates = _build_timebase_candidates(
        identity,
        now=now,
        provisioning_counter=now,
        primary_anchor_epoch=None,
    )

    labels = {candidate.label for candidate in candidates}
    assert TimebaseLabel.ABSOLUTE in labels
    assert TimebaseLabel.REL_PAIR not in labels

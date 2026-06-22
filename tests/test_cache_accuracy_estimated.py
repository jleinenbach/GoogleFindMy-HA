# tests/test_cache_accuracy_estimated.py
"""Producer-side tests for the ``accuracy_estimated`` flag in the cache.

The cache is the only component that can tell a conservative 200m fallback from
a real 200m measurement, because the sanitizer overwrites the raw value in
place. These tests pin that the flag is set to True at every fallback point and
False whenever a real measurement survives, across both the significance gate
(_is_significant_update) and the weighted fusion path
(_apply_weighted_location_fusion).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.googlefindmy.coordinator.cache import CacheOperations
from custom_components.googlefindmy.coordinator.helpers.cache import merge_cache_row
from custom_components.googlefindmy.coordinator.helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK_M,
)


def _mock_cache() -> MagicMock:
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {}
    coord.increment_stat = MagicMock()
    return coord


def test_significance_missing_accuracy_marks_estimated() -> None:
    """A missing accuracy key is filled with the fallback and flagged estimated."""

    coord = _mock_cache()
    update = {"latitude": 48.1, "longitude": 11.4, "last_seen": 1_700_000_100}

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert update["accuracy_estimated"] is True


def test_significance_error_code_accuracy_marks_estimated() -> None:
    """The 0.0 error code is sanitized to the fallback and flagged estimated."""

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 0.0,
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert update["accuracy_estimated"] is True


def test_significance_non_numeric_accuracy_marks_estimated() -> None:
    """A non-numeric accuracy is sanitized to the fallback and flagged estimated."""

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": "not-a-number",
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert update["accuracy_estimated"] is True


def test_significance_valid_accuracy_marks_not_estimated() -> None:
    """A real measurement survives sanitization and is flagged not estimated."""

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 12.5,
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == 12.5
    assert update["accuracy_estimated"] is False


def test_significance_sub_meter_accuracy_marks_not_estimated() -> None:
    """Valid sub-meter accuracy (modern GNSS) is not estimated."""

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 0.5,
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == 0.5
    assert update["accuracy_estimated"] is False


def test_fusion_both_valid_marks_not_estimated() -> None:
    """Fusing two real measurements yields a non-estimated result."""

    coord = _mock_cache()
    coord._device_location_data = {
        "dev": {
            "latitude": 48.10000,
            "longitude": 11.40000,
            "accuracy": 10.0,
            "location_type": "sensor",
        }
    }
    new_data = {
        "latitude": 48.10001,
        "longitude": 11.40001,
        "accuracy": 12.0,
    }

    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)

    assert result is True
    assert new_data["_fused_applied"] is True
    assert new_data["accuracy_estimated"] is False


def test_fusion_neither_valid_marks_estimated() -> None:
    """Fusing two unusable accuracies falls back and flags estimated."""

    coord = _mock_cache()
    coord._device_location_data = {
        "dev": {
            "latitude": 48.10000,
            "longitude": 11.40000,
            "accuracy": 0.0,  # error code -> not valid
            "location_type": "sensor",
        }
    }
    new_data = {
        "latitude": 48.10001,
        "longitude": 11.40001,
        "accuracy": 0.0,  # error code -> not valid
    }

    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)

    assert result is True
    # Neither input valid -> both safe-accuracy values equal the fallback, so the
    # "both fallback" short-circuit accepts as-is without fusing. The accuracy is
    # then sanitized downstream by _is_significant_update, which sets the flag.
    # Drive the full producer path to confirm the estimated flag lands.
    sig = CacheOperations._is_significant_update(coord, "dev", new_data)
    assert sig is True
    assert new_data["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert new_data["accuracy_estimated"] is True


def test_merge_keeps_accuracy_estimated_atomic_with_accuracy_when_rejected() -> None:
    """A rejected location update must not desync accuracy and its estimated flag.

    ``merge_cache_row`` keeps the existing location fields when an incoming update
    is not significant (older/lower-rank/jitter). ``accuracy_estimated`` must
    travel together with ``accuracy`` (both kept), otherwise a real fix would be
    paired with a fresh row's estimated flag, reintroducing exactly the
    indistinguishability the flag was added to remove. Regression for the
    [REVIEW:DIFF] finding on PR #1124.
    """

    # Existing: a real 10m fix (not estimated).
    existing = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 10.0,
        "accuracy_estimated": False,
        "last_seen": 2000.0,
        "source_rank": 10,
    }
    # Incoming: an older, estimated 200m fallback -> rejected (not significant).
    incoming = {
        "latitude": 48.2,
        "longitude": 11.5,
        "accuracy": 200.0,
        "accuracy_estimated": True,
        "last_seen": 1000.0,
        "source_rank": 10,
    }

    merged = merge_cache_row(existing, incoming)

    # Both location fields are kept together: the real fix stays not-estimated.
    assert merged["accuracy"] == 10.0
    assert merged["accuracy_estimated"] is False


def test_merge_takes_accuracy_estimated_with_accuracy_when_accepted() -> None:
    """An accepted update carries accuracy and its estimated flag together."""

    existing = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 10.0,
        "accuracy_estimated": False,
        "last_seen": 1000.0,
        "source_rank": 10,
    }
    # Newer, estimated fallback -> accepted; both fields update together.
    incoming = {
        "latitude": 48.2,
        "longitude": 11.5,
        "accuracy": 200.0,
        "accuracy_estimated": True,
        "last_seen": 5000.0,
        "source_rank": 10,
    }

    merged = merge_cache_row(existing, incoming)

    assert merged["accuracy"] == 200.0
    assert merged["accuracy_estimated"] is True

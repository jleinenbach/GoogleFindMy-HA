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
from custom_components.googlefindmy.coordinator.helpers.cache import (
    carry_reused_accuracy,
    merge_cache_row,
)
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


# ---------------------------------------------------------------------------
# carry_reused_accuracy helper (anti-repetition surface)
# ---------------------------------------------------------------------------
#
# The four upstream preserve-sites (polling.py Google-Home filter + semantic-only
# preserve; locate.py manual Google-Home filter + semantic-only setdefault) reuse
# a previously cached ``accuracy``. The cached value can be the conservative 200m
# fallback that was flagged ``accuracy_estimated=True``. Copying only the numeric
# value would let a downstream chokepoint reclassify it as a real measurement.
# ``carry_reused_accuracy`` keeps the flag travelling with the value.


def test_carry_assignment_copies_value_and_flag() -> None:
    """Assignment mode overwrites both accuracy and its estimated flag."""

    target: dict[str, object] = {"accuracy": 999.0, "accuracy_estimated": False}
    source = {"accuracy": 200.0, "accuracy_estimated": True}

    carry_reused_accuracy(target, source)

    assert target["accuracy"] == 200.0
    assert target["accuracy_estimated"] is True


def test_carry_assignment_flag_absent_in_source_is_cleared() -> None:
    """Assignment mode mirrors an absent source flag (no stale True remains)."""

    target: dict[str, object] = {"accuracy": 999.0, "accuracy_estimated": True}
    source = {"accuracy": 50.0}  # no flag -> source describes an unflagged fix

    carry_reused_accuracy(target, source)

    assert target["accuracy"] == 50.0
    assert "accuracy_estimated" not in target


def test_carry_setdefault_does_not_clobber_present_value() -> None:
    """setdefault mode leaves an already-present accuracy/flag untouched."""

    target: dict[str, object] = {"accuracy": 12.0, "accuracy_estimated": False}
    source = {"accuracy": 200.0, "accuracy_estimated": True}

    carry_reused_accuracy(target, source, setdefault=True)

    assert target["accuracy"] == 12.0
    assert target["accuracy_estimated"] is False


def test_carry_setdefault_fills_value_and_flag_when_missing() -> None:
    """setdefault mode fills both fields only when the target lacks accuracy."""

    target: dict[str, object] = {}
    source = {"accuracy": 200.0, "accuracy_estimated": True}

    carry_reused_accuracy(target, source, setdefault=True)

    assert target["accuracy"] == 200.0
    assert target["accuracy_estimated"] is True


def test_carry_setdefault_never_injects_none_flag() -> None:
    """setdefault mode must not inject a None flag when the source lacks one."""

    target: dict[str, object] = {}
    source = {"accuracy": 200.0}  # no flag in source

    carry_reused_accuracy(target, source, setdefault=True)

    assert target["accuracy"] == 200.0
    assert "accuracy_estimated" not in target


# ---------------------------------------------------------------------------
# Fusion chokepoint: an estimated input must not count as a real measurement
# ---------------------------------------------------------------------------


def test_fusion_existing_estimated_fallback_stays_estimated() -> None:
    """A cached estimated 200m fallback fused with a new estimated fallback stays estimated.

    Both inputs are numerically valid (200m >= MIN_VALID_ACCURACY) but both are
    flagged estimated, so neither is a real measurement and the fused result must
    remain estimated. The clear-jump branch is exercised (distinct coordinates,
    no overlap) so fusion reaches the flag derivation rather than the FIX #155
    short-circuit.
    """

    coord = _mock_cache()
    coord._device_location_data = {
        "dev": {
            "latitude": 48.10000,
            "longitude": 11.40000,
            "accuracy": DEFAULT_ACCURACY_FALLBACK_M,
            "accuracy_estimated": True,
            "location_type": "sensor",
        }
    }
    # Near-identical coordinates so the accuracy circles overlap and the
    # inverse-variance fusion math (line ~931) executes rather than the
    # clear-jump pass-through. The new accuracy differs from 200m so the FIX #155
    # both-fallback guard (which requires BOTH safe accuracies to equal the
    # fallback) does not short-circuit before the flag derivation.
    new_data = {
        "latitude": 48.10001,
        "longitude": 11.40001,
        "accuracy": 150.0,
        "accuracy_estimated": True,
    }

    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)

    assert result is True
    assert new_data["_fused_applied"] is True
    assert new_data["accuracy_estimated"] is True


def test_fusion_one_real_measurement_marks_not_estimated() -> None:
    """When one input is a real measurement the fused result is not estimated."""

    coord = _mock_cache()
    coord._device_location_data = {
        "dev": {
            "latitude": 48.10000,
            "longitude": 11.40000,
            "accuracy": DEFAULT_ACCURACY_FALLBACK_M,
            "accuracy_estimated": True,  # estimated fallback
            "location_type": "sensor",
        }
    }
    new_data = {
        "latitude": 48.10001,
        "longitude": 11.40001,
        "accuracy": 12.0,  # real measurement, not flagged
    }

    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)

    assert result is True
    assert new_data["accuracy_estimated"] is False


def test_fusion_real_existing_with_estimated_new_marks_not_estimated() -> None:
    """The symmetric case: a real cached fix plus an estimated new input stays real.

    Mirrors ``test_fusion_one_real_measurement_marks_not_estimated`` with the roles
    swapped (real on the existing side, estimated fallback on the new side) to pin
    that the ``real_existing or real_new`` derivation is symmetric.
    """

    coord = _mock_cache()
    coord._device_location_data = {
        "dev": {
            "latitude": 48.10000,
            "longitude": 11.40000,
            "accuracy": 12.0,  # real measurement, not flagged
            "location_type": "sensor",
        }
    }
    new_data = {
        "latitude": 48.10001,
        "longitude": 11.40001,
        "accuracy": DEFAULT_ACCURACY_FALLBACK_M,
        "accuracy_estimated": True,  # estimated fallback carried on the new side
    }

    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)

    assert result is True
    assert new_data["accuracy_estimated"] is False


# ---------------------------------------------------------------------------
# Significance chokepoint: a carried estimated True must not be clobbered
# ---------------------------------------------------------------------------


def test_significance_preserves_carried_estimated_true() -> None:
    """A preserved estimated 200m fallback keeps its True flag through significance.

    This reproduces the Codex finding end-to-end at the chokepoint: a semantic-only
    update carried the cached 200m fallback and its ``accuracy_estimated=True``.
    The value is numerically valid, so the old unconditional ``False`` assignment
    reclassified it as a real measurement. The flag must survive.
    """

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": DEFAULT_ACCURACY_FALLBACK_M,
        "accuracy_estimated": True,  # carried from the cached fallback
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert update["accuracy_estimated"] is True


def test_significance_fresh_real_measurement_marks_not_estimated() -> None:
    """A fresh real measurement without a prior flag is still marked not estimated."""

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 25.0,
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy_estimated"] is False


def test_significance_preserves_explicit_estimated_false() -> None:
    """An explicit ``accuracy_estimated=False`` on a real value stays False.

    The ``is not True`` guard must be idempotent for a value already flagged as a
    real measurement: it neither flips it to True nor drops the key.
    """

    coord = _mock_cache()
    update = {
        "latitude": 48.1,
        "longitude": 11.4,
        "accuracy": 25.0,
        "accuracy_estimated": False,  # already declared a real measurement
        "last_seen": 1_700_000_100,
    }

    result = CacheOperations._is_significant_update(coord, "dev", update)

    assert result is True
    assert update["accuracy_estimated"] is False


# ---------------------------------------------------------------------------
# End-to-end producer pipeline: preserve-site -> fusion -> significance
# ---------------------------------------------------------------------------
#
# Mirrors the runtime order: an upstream preserve-site (polling/locate) reuses a
# cached estimated 200m fallback via ``carry_reused_accuracy`` (so the value AND
# the flag travel together), then the payload runs through fusion and the
# significance gate. The final flag must remain True so the recorder/map draw an
# estimated (dashed) circle rather than a solid "real measurement" radius.


def test_pipeline_preserved_estimated_fallback_stays_estimated() -> None:
    """A preserved estimated fallback survives fusion + significance as estimated.

    Reproduces the Codex finding on PR #1124 end-to-end through the chokepoints
    that follow the preserve-site, using the same helper the preserve-sites use.
    """

    # Cached row: an estimated 200m fallback.
    cached = {
        "latitude": 48.10000,
        "longitude": 11.40000,
        "accuracy": DEFAULT_ACCURACY_FALLBACK_M,
        "accuracy_estimated": True,
        "location_type": "sensor",
    }
    coord = _mock_cache()
    coord._device_location_data = {"dev": cached}

    # Semantic-only update: coordinates/accuracy are reused from the cached row
    # exactly as the preserve-sites do, via the shared carry helper.
    update: dict[str, object] = {
        "semantic_name": None,
        "last_seen": 1_700_000_100,
    }
    update["latitude"] = cached["latitude"]
    update["longitude"] = cached["longitude"]
    carry_reused_accuracy(update, cached)

    # Fusion runs first (preapplied flag set by the caller afterwards).
    assert CacheOperations._apply_weighted_location_fusion(coord, "dev", update) is True
    # Then the significance gate (authoritative writer) runs.
    assert CacheOperations._is_significant_update(coord, "dev", update) is True

    assert update["accuracy"] == DEFAULT_ACCURACY_FALLBACK_M
    assert update["accuracy_estimated"] is True

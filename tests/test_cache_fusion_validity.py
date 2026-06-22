# tests/test_cache_fusion_validity.py
"""Characterization tests for the fusion accuracy-validity predicate.

These tests pin the observable behavior of the weighted fusion path
(_apply_weighted_location_fusion) across a value matrix that lights up the
difference between the local ``_is_valid_accuracy`` (acc > 0, isinstance) and the
canonical ``geo.is_valid_accuracy`` (acc >= 0.001m, float-cast). They establish a
behavioral baseline before any DRY consolidation so a change can only land if it
is verified behavior-neutral.

Note: inputs reach the predicate already passed through ``coerce_float``, so by
construction they are always a finite float or None (numeric strings are
converted, NaN/Inf/non-numeric become None). The only remaining behavioral
difference between the two predicates is therefore the open interval
(0, 0.001m).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator.cache import CacheOperations
from custom_components.googlefindmy.coordinator.helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK_M,
)


def _mock_cache(existing: dict[str, Any]) -> MagicMock:
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {"dev": existing}
    coord.increment_stat = MagicMock()
    return coord


def _fuse(existing_acc: Any, new_acc: Any) -> dict[str, Any]:
    """Run fusion for two overlapping points and return the mutated new_data.

    The points share near-identical coordinates so their accuracy circles
    overlap and the fusion math (not a clear-jump pass-through) executes.
    """

    existing = {
        "latitude": 48.100000,
        "longitude": 11.400000,
        "accuracy": existing_acc,
        "location_type": "sensor",
    }
    coord = _mock_cache(existing)
    new_data: dict[str, Any] = {
        "latitude": 48.100001,
        "longitude": 11.400001,
        "accuracy": new_acc,
    }
    result = CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)
    new_data["_fusion_result"] = result
    return new_data


# Value matrix: (existing_acc, new_acc) -> expected observable outcome.
#
# Outcomes are characterized against the CURRENT local predicate. ``estimated``
# is the value of new_data["accuracy_estimated"] after fusion, or None when the
# fusion path returned before setting it (early pass-through / both-fallback
# short-circuit, where the significance gate sets the flag later instead).
@pytest.mark.parametrize(
    ("existing_acc", "new_acc", "expect_accuracy_kind", "expect_estimated"),
    [
        # Both valid real measurements -> fused, not estimated.
        (10.0, 12.0, "fused", False),
        # One valid, one error code (0.0): safe() lifts 0.0 to 200, so circles
        # overlap; only the valid side counts -> best_accuracy is the valid raw.
        (10.0, 0.0, "valid_existing", False),
        (0.0, 12.0, "valid_new", False),
        # Both error codes -> both-fallback short-circuit accepts as-is and does
        # not set the flag in fusion (significance gate handles it downstream).
        (0.0, 0.0, "passthrough", None),
        # Negative values behave like error codes through safe()/predicate.
        (-5.0, 12.0, "valid_new", False),
        # Sub-millimeter values in (0, 0.001): the local predicate treats them as
        # VALID (> 0), so BOTH sides are valid and the inverse-variance fusion
        # runs. geo would reject the sub-mm side (< 0.001), collapsing to the
        # valid-new branch instead. This is the single behavioral divergence
        # point and is pinned here as "fused".
        (0.0005, 12.0, "fused_submm", False),
    ],
)
def test_fusion_validity_value_matrix(
    existing_acc: Any,
    new_acc: Any,
    expect_accuracy_kind: str,
    expect_estimated: Any,
) -> None:
    """Pin fusion accuracy + estimated flag across the validity value matrix."""

    out = _fuse(existing_acc, new_acc)

    assert out["_fusion_result"] is True

    if expect_estimated is None:
        assert "accuracy_estimated" not in out
    else:
        assert out["accuracy_estimated"] is expect_estimated

    acc = out["accuracy"]
    if expect_accuracy_kind == "fused":
        # Inverse-variance fusion bounded below by MIN_FUSED_ACCURACY_M (5.0).
        assert acc >= 5.0
    elif expect_accuracy_kind == "fused_submm":
        # Both treated valid by the local predicate -> inverse-variance fusion.
        # The result is close to the better (sub-mm) input but floored by the
        # "never claim better than the best input" / MIN_FUSED bounds.
        assert acc == pytest.approx(11.9784581, rel=1e-6)
    elif expect_accuracy_kind == "valid_existing":
        assert acc == existing_acc
    elif expect_accuracy_kind == "valid_new":
        assert acc == new_acc
    elif expect_accuracy_kind == "passthrough":
        # Both-fallback short-circuit leaves the incoming accuracy untouched.
        assert acc == new_acc


def test_submillimeter_is_the_only_divergence_point() -> None:
    """Document the local-vs-geo divergence explicitly for the DRY decision.

    With existing accuracy 0.0005m (in the open interval (0, 0.001)) and a valid
    new measurement, the local predicate (> 0) treats the sub-millimeter value as
    a valid measurement, so BOTH sides count and the inverse-variance fusion runs
    (result ~11.978m). geo.is_valid_accuracy (>= 0.001) would reject the sub-mm
    side, collapsing to the valid-new branch (result exactly 12.0m). Swapping the
    predicate would therefore change this observable output, which is why the
    consolidation is NOT behavior-neutral. This test pins the current (local)
    behavior so the divergence cannot be erased silently.
    """

    out = _fuse(0.0005, 12.0)

    assert out["_fusion_result"] is True
    # Current (local-predicate) behavior: fused, distinctly different from the
    # 12.0 that a geo-predicate consolidation would produce.
    assert out["accuracy"] == pytest.approx(11.9784581, rel=1e-6)
    assert out["accuracy"] != pytest.approx(12.0)
    assert out["accuracy"] != DEFAULT_ACCURACY_FALLBACK_M

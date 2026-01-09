"""Geographic utility functions for GoogleFindMy coordinator.

This module provides:
- Value clamping utilities
- Safe float coercion
- GPS accuracy normalization
- Haversine distance calculation between coordinates

Extracted from coordinator.py for improved testability and reduced complexity.
All functions are pure and side-effect free.

Phase 1 of coordinator.py refactoring.
"""

from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Earth radius in meters (WGS84 mean radius)
EARTH_RADIUS_M = 6371000.0

# Default fallback for invalid/missing GPS accuracy
DEFAULT_ACCURACY_FALLBACK = 10000.0


# ---------------------------------------------------------------------------
# Value Clamping
# ---------------------------------------------------------------------------


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi (inclusive).

    Args:
        value: The value to clamp.
        lo: Lower bound.
        hi: Upper bound.

    Returns:
        The clamped value. Returns lo if value cannot be converted.

    Example:
        >>> clamp(5.0, 0.0, 10.0)
        5.0
        >>> clamp(15.0, 0.0, 10.0)
        10.0
        >>> clamp(-5.0, 0.0, 10.0)
        0.0
    """
    try:
        v = float(value)
        return max(float(lo), min(float(hi), v))
    except (TypeError, ValueError):
        return float(lo)


# ---------------------------------------------------------------------------
# Float Coercion
# ---------------------------------------------------------------------------


def coerce_float(value: Any) -> float | None:
    """Return a float representation or None when conversion fails.

    Rejects NaN and Infinity values as they are not valid coordinates
    or measurements.

    Args:
        value: Any value to convert to float.

    Returns:
        A finite float, or None if conversion fails or value is not finite.

    Example:
        >>> coerce_float("3.14")
        3.14
        >>> coerce_float("invalid")
        None
        >>> coerce_float(float("nan"))
        None
    """
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(coerced):
        return None
    return coerced


# ---------------------------------------------------------------------------
# GPS Accuracy Normalization
# ---------------------------------------------------------------------------


def safe_accuracy(value: float | None) -> float:
    """Normalize GPS accuracy to a safe, finite value.

    Invalid, missing, or negative accuracy values are replaced with
    a large fallback (10km) to indicate low confidence.

    Args:
        value: GPS accuracy in meters, or None.

    Returns:
        A non-negative finite float representing accuracy in meters.

    Example:
        >>> safe_accuracy(50.0)
        50.0
        >>> safe_accuracy(None)
        10000.0
        >>> safe_accuracy(-5.0)
        10000.0
    """
    if value is None or not math.isfinite(value):
        return DEFAULT_ACCURACY_FALLBACK
    if value < 0:
        return DEFAULT_ACCURACY_FALLBACK
    return value


# ---------------------------------------------------------------------------
# Distance Calculation
# ---------------------------------------------------------------------------


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return distance in meters between two WGS84 coordinates.

    Uses the Haversine formula which gives great-circle distances
    between two points on a sphere.

    Implementation note:
        Kept lightweight and allocation-free; called per candidate update only.

    Args:
        lat1: Latitude of first point in degrees.
        lon1: Longitude of first point in degrees.
        lat2: Latitude of second point in degrees.
        lon2: Longitude of second point in degrees.

    Returns:
        Distance in meters.

    Example:
        >>> int(haversine_distance(52.52, 13.405, 48.1351, 11.582))
        504227  # Berlin to Munich, approximately
    """
    from math import atan2, cos, radians, sin, sqrt

    lat1_r, lon1_r = radians(float(lat1)), radians(float(lon1))
    lat2_r, lon2_r = radians(float(lat2)), radians(float(lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = sin(dlat / 2.0) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2.0) ** 2
    c = 2.0 * atan2(sqrt(a), sqrt(1.0 - a))
    return EARTH_RADIUS_M * c

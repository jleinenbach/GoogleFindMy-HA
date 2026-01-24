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

# Minimum valid accuracy threshold (meters).
# The Android Location API uses 0.0 as an error code meaning "no accuracy available".
# Modern dual-frequency GNSS chips (L1+L5) can achieve sub-meter accuracy under
# ideal conditions (Open Sky), so we use 1mm as the floor to catch only the
# error code (0.0) and negative values, NOT valid high-precision measurements.
MIN_VALID_ACCURACY = 0.001  # 1 millimeter

# Default fallback for invalid/missing GPS accuracy.
# Based on Bluetooth tracker physics: max Bluetooth range (~100m) + GPS error margin.
# 200m is large enough to lose against any real GPS measurement (typically 20-50m)
# in weighted fusion (200²/20² = 100x lower weight), yet small enough to be
# useful for actually finding a tracker on a map (unlike 2km which is useless).
PRIVACY_ACCURACY_FALLBACK = 200.0  # 200 meters

# Legacy alias for backward compatibility
DEFAULT_ACCURACY_FALLBACK = PRIVACY_ACCURACY_FALLBACK

# Legacy aliases for backward compatibility
MIN_PHYSICAL_ACCURACY_M = MIN_VALID_ACCURACY
DEFAULT_ACCURACY_FALLBACK_M = DEFAULT_ACCURACY_FALLBACK


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


def safe_accuracy(value: Any, *, fallback: float | None = None) -> float:
    """Normalize GPS accuracy to a safe, finite value.

    This function is EXTREMELY DEFENSIVE - it will never raise an exception
    and always returns a valid numeric value suitable for Home Assistant's
    gps_accuracy attribute.

    The Android Location API uses 0.0 as an error code meaning "no accuracy".
    We treat values < MIN_VALID_ACCURACY (0.001m) as this error code.

    Modern dual-frequency GNSS can achieve sub-meter accuracy, so values like
    0.01m (1cm) or 0.5m are valid and preserved unchanged.

    Args:
        value: Any value that might represent GPS accuracy. Handles None,
               strings, floats, ints, and any other type gracefully.
        fallback: Custom fallback value. Defaults to PRIVACY_ACCURACY_FALLBACK (200m).

    Returns:
        A finite float >= MIN_VALID_ACCURACY representing accuracy in meters,
        or the fallback value if input is invalid. NEVER returns None.

    Example:
        >>> safe_accuracy(50.0)
        50.0
        >>> safe_accuracy(0.5)  # Valid sub-meter accuracy
        0.5
        >>> safe_accuracy(0.01)  # Valid centimeter accuracy
        0.01
        >>> safe_accuracy(None)
        200.0
        >>> safe_accuracy(0.0)  # Error code
        200.0
        >>> safe_accuracy(-5.0)
        200.0
        >>> safe_accuracy("invalid")  # Non-numeric
        200.0
    """
    if fallback is None:
        fallback = DEFAULT_ACCURACY_FALLBACK

    # Handle None explicitly
    if value is None:
        return fallback

    # Try to convert to float - handle any type gracefully
    try:
        float_value = float(value)
    except (TypeError, ValueError):
        return fallback

    # Check for NaN/Inf
    if not math.isfinite(float_value):
        return fallback

    # Values below MIN_VALID_ACCURACY are the error code (0.0) or negative
    # Modern GNSS can achieve sub-meter accuracy, so we only reject < 0.001m
    if float_value < MIN_VALID_ACCURACY:
        return fallback

    return float_value


def is_valid_accuracy(value: float | None) -> bool:
    """Check if an accuracy value is valid (not an error code).

    Valid accuracy must be:
    - Not None
    - Finite (not NaN or Inf)
    - >= MIN_VALID_ACCURACY (0.001m) - below this is the error code 0.0

    Modern dual-frequency GNSS can achieve sub-meter accuracy, so values like
    0.01m (1cm) or 0.5m are valid.

    Args:
        value: GPS accuracy in meters, or None.

    Returns:
        True if the value represents a valid GPS measurement.

    Example:
        >>> is_valid_accuracy(20.0)
        True
        >>> is_valid_accuracy(0.5)  # Valid sub-meter
        True
        >>> is_valid_accuracy(0.01)  # Valid centimeter
        True
        >>> is_valid_accuracy(0.0)  # Error code
        False
        >>> is_valid_accuracy(None)
        False
    """
    if value is None:
        return False
    try:
        v = float(value)
        return math.isfinite(v) and v >= MIN_VALID_ACCURACY
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Distance Calculation
# ---------------------------------------------------------------------------


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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

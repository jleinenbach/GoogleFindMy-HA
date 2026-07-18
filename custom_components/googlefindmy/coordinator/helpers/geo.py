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
from collections.abc import Mapping
from typing import Any, TypeVar

_RowT = TypeVar("_RowT", bound=Mapping[str, Any])

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

    # Single source of truth for the validity policy: is_valid_accuracy()
    # decides None / non-numeric / NaN / Inf / below-MIN_VALID_ACCURACY in one
    # place, so safe_accuracy and is_valid_accuracy can never drift apart.
    if not is_valid_accuracy(value):
        return fallback

    # Validity guarantees float() succeeds and the value is finite and
    # >= MIN_VALID_ACCURACY, so this conversion cannot fail.
    return float(value)


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


def recorded_accuracy_pair(
    attributes: Mapping[str, Any],
) -> tuple[Any, bool | None]:
    """Read the ``(raw_accuracy, estimated_flag)`` pair from a recorded state.

    The accuracy value and its "estimated" provenance MUST be read from the
    same authoritative source. Reading the value from one attribute and the
    flag from another lets a stale or fallback radius be paired with the wrong
    provenance, so a 200m fallback ends up masquerading as a real measurement.
    That decoupling is the recurring defect class behind several PR #1124
    Codex findings; this single reader is the structural fix and every consumer
    that reconstructs accuracy from a recorded/published state must use it.

    Value precedence:
        ``accuracy_m`` -- the stable producer attribute emitted by
        ``_as_ha_attributes``. It survives Home Assistant clearing the volatile
        core ``gps_accuracy`` value on stale states, so it always reflects the
        producer's measurement (real or fallback).
        ``gps_accuracy`` -- the Home Assistant core integer; the only accuracy
        key present on legacy recorder rows predating ``accuracy_m``.

    Args:
        attributes: A recorded state's attribute mapping.

    Returns:
        A ``(raw_accuracy, estimated_flag)`` tuple. ``raw_accuracy`` is the
        producer value when available, else the core/legacy value, else
        ``None``. ``estimated_flag`` is the producer ``accuracy_estimated``
        bool when present, else ``None`` so callers can apply a legacy validity
        fallback on ``raw_accuracy`` themselves.
    """
    raw = attributes.get("accuracy_m")
    if raw is None:
        raw = attributes.get("gps_accuracy")
    flag = attributes.get("accuracy_estimated")
    return raw, (bool(flag) if flag is not None else None)


# ---------------------------------------------------------------------------
# Display-row selection & staleness
# ---------------------------------------------------------------------------
# Single source of truth for "which fix actually gets published" and "is that
# fix too old to publish". The device_tracker owns the stateful last-good cache
# and its restore wiring; these pure helpers let every location consumer
# (tracker, Plus Code sensor) apply the identical accuracy/staleness gate so no
# consumer publishes a coordinate the tracker deliberately hides (Codex #202 /
# PR #1179).


def has_usable_accuracy(row: Mapping[str, Any] | None) -> bool:
    """Return ``True`` when ``row`` carries a non-``None`` accuracy.

    Mirrors the tracker's publish gate: an accuracy-less fix is deliberately
    withheld (HA's zone engine raises ``TypeError`` comparing it, and the
    integration hides accuracy-less positions on purpose).
    """
    return row is not None and row.get("accuracy") is not None


def select_display_row(
    current: _RowT | None,
    last_good: _RowT | None,
) -> _RowT | None:
    """Return the single row whose data is actually published.

    The current row when it carries a usable accuracy, otherwise the last
    accuracy-bearing fix, otherwise ``None``. Binding every consumer to this one
    row keeps the published snapshot internally consistent: a fresh update can
    arrive with valid lat/lon yet no accuracy, and publishing that coordinate
    would leak a position the tracker hides (Codex #202).

    The fallback applies the *same* :func:`has_usable_accuracy` gate to
    ``last_good`` that it applies to ``current``: the last-good cache can hold a
    genuinely accuracy-less row (bootstrapped from a legacy/partial restore that
    never went through ``_is_significant_update`` sanitization) purely so age /
    status / ``has_last_known`` stay available, but such a row must never be
    published as a coordinate. Gating both branches identically is what makes
    the standalone Plus Code sensor and the tracker ``plus_code`` attribute
    blank the coordinate exactly where the tracker already blanks its own
    ``latitude``/``longitude`` (Codex PR #1181).
    """
    if has_usable_accuracy(current):
        return current
    if has_usable_accuracy(last_good):
        return last_good
    return None


def is_reliable_fix(row: Mapping[str, Any] | None) -> bool:
    """Return ``True`` when ``row`` carries a *real* (non-estimated) accuracy.

    Stricter than :func:`has_usable_accuracy`, and the gate for the *retention*
    of a last-good fix (coordinator ``_device_last_good_location`` and the
    tracker's private ``_last_good_accuracy_data``). ``_is_significant_update``
    replaces a missing or error-code accuracy with the conservative 200 m
    fallback and marks ``accuracy_estimated=True``; that sanitized row is
    accuracy-less in substance, so :func:`has_usable_accuracy` (a plain
    ``accuracy is not None`` check) can no longer tell it apart from a real fix.
    Retaining such a row as last-good would poison the fallback the Plus Code
    display accessors read (#1179). :func:`has_usable_accuracy` stays the
    *display* gate (:func:`select_display_row`) so a fresh estimated fix is
    still shown; ``is_reliable_fix`` is the retention gate so only a real
    measurement overwrites the last reliable position.

    Also rejects a numerically *present* but non-physical accuracy even when
    ``accuracy_estimated`` is absent: Android's no-accuracy sentinel (``0.0``),
    a negative value, a sub-``MIN_PHYSICAL_ACCURACY_M`` value, or a non-finite
    one (``NaN``/``inf``). Such a row is accuracy-less in substance, so it must
    not be retained as last-good, seed a round-trip anchor, or serve as a
    speed-gate reference. This mirrors the incoming-side ``new_acc_measured``
    check in ``cache.py`` (``math.isfinite`` + ``>= MIN_PHYSICAL_ACCURACY_M``),
    closing the asymmetry where the cached/reference endpoint was gated only on
    presence. ``has_usable_accuracy`` deliberately stays lenient here (a ``0.0``
    is still "present" for display), so only ``is_reliable_fix`` tightens.
    """
    if not has_usable_accuracy(row) or row is None:
        return False
    if bool(row.get("accuracy_estimated")):
        return False
    acc = row.get("accuracy")
    return (
        isinstance(acc, (int, float))
        and not isinstance(acc, bool)
        and math.isfinite(acc)
        and acc >= MIN_PHYSICAL_ACCURACY_M
    )


def encode_plus_code_for_row(row: Mapping[str, Any] | None) -> str | None:
    """Return the 10-digit Plus Code for ``row``'s coordinates, or ``None``.

    Shared SSOT for the Plus Code sensor and the device_tracker ``plus_code``
    attribute so both encode the identical display row (single source, no
    duplicated encode logic). Rejects missing, boolean (a ``bool`` is an ``int``
    subclass) and non-finite (NaN/inf) coordinates, mirroring the sensor's
    original guard, so the result is ``None`` rather than a bogus code.
    """
    if not row:
        return None
    lat = row.get("latitude")
    lon = row.get("longitude")
    if (
        not isinstance(lat, (int, float))
        or not isinstance(lon, (int, float))
        or isinstance(lat, bool)
        or isinstance(lon, bool)
        or not math.isfinite(lat)
        or not math.isfinite(lon)
    ):
        return None
    # Lazy import mirrors resolve_stale_threshold's const import below: keeps the
    # vendored Open Location Code encoder off the module-import hot path.
    from ...vendor.openlocationcode import encode

    try:
        return encode(float(lat), float(lon), 10)
    except (ValueError, TypeError):  # pragma: no cover - defensive; inputs are
        # already validated as finite numbers above, so encode does not raise.
        return None


def location_age_seconds(row: Mapping[str, Any] | None, now: float) -> float | None:
    """Return the age of ``row`` in seconds relative to ``now``, or ``None``.

    ``now`` is injected rather than read from the clock so the computation stays
    pure and testable. Returns ``None`` when there is no row or its ``last_seen``
    is missing or non-numeric.
    """
    if not row:
        return None
    last_seen = row.get("last_seen")
    if last_seen is None:
        return None
    try:
        return now - float(last_seen)
    except (TypeError, ValueError):
        return None


def resolve_stale_threshold(coordinator: Any) -> int:
    """Return the configured stale threshold in seconds for ``coordinator``.

    Duck-typed SSOT reader shared by the device_tracker and the Plus Code
    sensor: reads ``config_entry.options[OPT_STALE_THRESHOLD]`` and falls back
    to ``DEFAULT_STALE_THRESHOLD`` on any absence or malformed value.
    """
    from ...const import DEFAULT_STALE_THRESHOLD, OPT_STALE_THRESHOLD

    entry = getattr(coordinator, "config_entry", None)
    if entry is None:
        return DEFAULT_STALE_THRESHOLD
    options = getattr(entry, "options", {})
    if not isinstance(options, Mapping):
        return DEFAULT_STALE_THRESHOLD
    threshold = options.get(OPT_STALE_THRESHOLD, DEFAULT_STALE_THRESHOLD)
    try:
        return int(threshold)
    except (TypeError, ValueError):
        return DEFAULT_STALE_THRESHOLD


def resolve_seeded_accuracy(raw: Any, flag: bool | None) -> tuple[float, bool | None]:
    """Couple accuracy sanitization with its ``estimated`` provenance.

    Write-side mirror of :func:`recorded_accuracy_pair`. A direct cache seed
    (e.g. ``prime_device_location_cache`` on restore) bypasses the canonical
    ``_is_significant_update`` writer, which is the single place that normally
    pairs sanitization with provenance: whenever it replaces an invalid value
    with the conservative fallback radius it also sets ``accuracy_estimated``
    so a fabricated radius never masquerades as a real measurement. Open-coding
    only :func:`safe_accuracy` at such a seed site reproduces that decoupling
    (the recurring PR #1124 defect class) -- the fabricated fallback radius
    enters flagless and map_view then draws a solid accuracy circle for it.

    This helper keeps the two halves together so every seed site classifies
    provenance the same way the canonical writer does:

    - whenever :func:`safe_accuracy` has to fall back (``raw`` is missing or
      invalid) the value is ``estimated``, overriding any recorded ``flag``.
      The canonical ``_is_significant_update`` writer marks every fabricated
      fallback radius estimated regardless of an incoming flag, so a stale
      ``accuracy_estimated=False`` recorded beside an error-code value (e.g.
      ``gps_accuracy=0``) never wins -- otherwise the fabricated radius would
      masquerade as a real measurement and map_view would draw a solid circle;
    - otherwise, for a numerically valid value, an explicit producer ``flag``
      wins (a restored real/estimated fix keeps its recorded provenance);
    - otherwise ``None`` for a legacy *valid* measurement with no recorded
      flag, leaving the documented map_view legacy fallback in charge instead
      of fabricating a flag.

    Args:
        raw: The recorded/raw accuracy value (producer or legacy), or ``None``.
        flag: The recorded ``accuracy_estimated`` provenance, or ``None`` when
            the source row predates the flag.

    Returns:
        A ``(sanitized_accuracy, estimated_flag)`` tuple. ``sanitized_accuracy``
        is always a finite float (see :func:`safe_accuracy`). ``estimated_flag``
        is the resolved provenance, or ``None`` when the caller should leave the
        flag unset (legacy valid value without recorded provenance).
    """
    sanitized = safe_accuracy(raw)
    if not is_valid_accuracy(raw):
        # safe_accuracy fell back to the conservative radius. The canonical
        # _is_significant_update writer ALWAYS marks such a fabricated radius
        # estimated, overriding any flag a stale recorder row carried (e.g. an
        # explicit accuracy_estimated=False next to gps_accuracy=0). Mirror that
        # unconditionally so the fallback can never masquerade as a real fix.
        return sanitized, True
    if flag is not None:
        # Valid measurement with recorded provenance: honor it (a carried
        # estimated fix keeps its flag; a real fix stays real).
        return sanitized, bool(flag)
    # Legacy valid measurement without a recorded flag: do not fabricate one.
    return sanitized, None


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

"""Locate operations for GoogleFindMyCoordinator.

This module contains location-related methods extracted from main.py.

Methods moved here:
- _normalize_coords: Validate and normalize latitude/longitude
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


class LocateOperations:
    """Locate operations mixin for GoogleFindMyCoordinator.

    This class contains methods that handle device location requests,
    including coordinate validation and location management.
    """

    def _normalize_coords(
        self: "GoogleFindMyCoordinator",
        payload: dict[str, Any],
        *,
        device_label: str | None = None,
        warn_on_invalid: bool = True,
    ) -> bool:
        """Validate and normalize latitude/longitude (and optionally accuracy).

        - Accepts numeric-like strings and converts them to floats.
        - Rejects NaN/Inf and out-of-range values.
        - Writes normalized floats back into `payload` when valid.
        - Normalizes `accuracy` to a finite float if present (best-effort).

        Returns:
            True if latitude/longitude are present and valid after normalization.
            False if coordinates are missing or invalid.

        Side effects:
            - Increments `invalid_coords` on invalid input.
            - Logs warnings for invalid data (unless warn_on_invalid=False).
        """
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if lat is None or lon is None:
            # Missing coordinates is not an error per se (semantic-only is valid).
            return False

        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring invalid (non-numeric) coordinates%s: lat=%r, lon=%r",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        if not (
            math.isfinite(lat_f)
            and math.isfinite(lon_f)
            and -90.0 <= lat_f <= 90.0
            and -180.0 <= lon_f <= 180.0
        ):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring out-of-range/invalid coordinates%s: lat=%s, lon=%s",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        # Write back normalized floats
        payload["latitude"] = lat_f
        payload["longitude"] = lon_f

        # Best-effort normalize accuracy (if present)
        acc = payload.get("accuracy")
        if acc is not None:
            try:
                acc_f = float(acc)
                if math.isfinite(acc_f):
                    payload["accuracy"] = acc_f
            except (TypeError, ValueError):
                # Accuracy can be absent or malformed; not critical enough for a warning.
                pass

        return True

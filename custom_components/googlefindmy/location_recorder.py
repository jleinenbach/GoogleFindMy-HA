# custom_components/googlefindmy/location_recorder.py
"""Location history using Home Assistant's recorder properly."""

import logging
import math
import time
from typing import Any
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.components.recorder import history, get_instance
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class LocationRecorder:
    """Manage location history using Home Assistant's recorder properly."""

    def __init__(self, hass: HomeAssistant):
        """Initialize location recorder."""
        self.hass = hass

    async def get_location_history(
        self, entity_id: str, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Get location history from recorder for the last N hours."""
        try:
            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(hours=hours)

            # Use the proper recorder database executor API
            recorder_instance = get_instance(self.hass)
            history_list = await recorder_instance.async_add_executor_job(
                history.get_significant_states,
                self.hass,
                start_time,
                end_time,
                [entity_id],
                None,  # filters
                True,  # include_start_time_state
                True,  # significant_changes_only
                False,  # minimal_response
                False,  # no_attributes
            )

            locations = []
            if entity_id in history_list:
                for state in history_list[entity_id]:
                    if state.state not in ("unknown", "unavailable", None):
                        # Extract location from attributes
                        attrs = state.attributes or {}
                        if ATTR_LATITUDE in attrs and ATTR_LONGITUDE in attrs:
                            last_seen_ts, last_seen_utc = _extract_last_seen(attrs)
                            fallback_ts = state.last_changed.timestamp()
                            timestamp = (
                                last_seen_ts
                                if last_seen_ts is not None
                                else fallback_ts
                            )
                            locations.append(
                                {
                                    "timestamp": timestamp,
                                    "latitude": attrs.get(ATTR_LATITUDE),
                                    "longitude": attrs.get(ATTR_LONGITUDE),
                                    "accuracy": attrs.get(
                                        "gps_accuracy", attrs.get("accuracy")
                                    ),
                                    "is_own_report": attrs.get("is_own_report", False),
                                    "altitude": attrs.get("altitude"),
                                    "state": state.state,
                                    "last_seen": last_seen_ts,
                                    "last_seen_utc": last_seen_utc,
                                }
                            )

            # Sort by timestamp (newest first)
            locations.sort(key=lambda x: x["timestamp"], reverse=True)

            _LOGGER.debug(
                f"Retrieved {len(locations)} historical locations from recorder"
            )
            return locations

        except Exception as e:
            _LOGGER.error(f"Failed to get location history from recorder: {e}")
            return []

    def get_best_location(self, locations: list[dict[str, Any]]) -> dict[str, Any]:
        """Select the best location from a list of locations."""
        if not locations:
            return {}

        current_time = time.time()

        def _normalized_ts(loc: dict[str, Any]) -> float | None:
            ts = _normalize_epoch(loc.get("last_seen"))
            if ts is None:
                ts = _normalize_epoch(loc.get("timestamp"))
            return ts

        def calculate_score(loc: dict[str, Any], ts: float | None) -> float:
            """Calculate location score (lower is better)."""
            try:
                accuracy = loc.get("accuracy", float("inf"))
                semantic = loc.get("semantic_name")
                if accuracy is None:
                    accuracy = float(0) if semantic else float("inf")
                else:
                    accuracy = float(accuracy)
            except (TypeError, ValueError):
                accuracy = float("inf")

            if ts is None:
                # Without a usable timestamp the record cannot be fresher.
                return float("inf")

            age_seconds = max(0.0, current_time - ts)

            # Age penalty: 1m per 3 minutes
            age_penalty = age_seconds / (3 * 60)

            # Heavy penalty for old locations (> 2 hours)
            if age_seconds > 2 * 60 * 60:
                age_penalty += 100

            # Bonus for own reports
            own_report_bonus = -2 if loc.get("is_own_report") else 0

            return accuracy + age_penalty + own_report_bonus

        try:
            ranked: list[tuple[float, float, int, dict[str, Any], float | None]] = []
            for idx, loc in enumerate(locations):
                ts = _normalized_ts(loc)
                score = calculate_score(loc, ts)
                ts_rank = ts if ts is not None else float("-inf")
                ranked.append((ts_rank, -score, -idx, loc, ts))

            if not ranked:
                return {}

            ranked.sort(reverse=True)
            _, _, _, best, best_ts = ranked[0]

            age_minutes = (
                (current_time - best_ts) / 60 if best_ts is not None else float("inf")
            )
            _LOGGER.debug(
                "Selected best location: accuracy=%sm, age=%0.1fmin from %d options",
                best.get("accuracy"),
                age_minutes,
                len(locations),
            )

            return best

        except Exception as e:
            _LOGGER.error(f"Failed to select best location: {e}")
            return locations[0] if locations else {}


def _normalize_epoch(value: Any) -> float | None:
    """Normalize epoch timestamps (seconds) allowing milliseconds and ISO strings."""

    if value is None:
        return None

    try:
        f = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.timestamp()
        return None

    if not math.isfinite(f):
        return None
    if f > 1_000_000_000_000:
        f = f / 1000.0
    return f


def _extract_last_seen(attrs: dict[str, Any]) -> tuple[float | None, str | None]:
    """Return normalized last_seen epoch seconds and the best UTC ISO string."""

    raw_last_seen = attrs.get("last_seen")
    raw_last_seen_utc = attrs.get("last_seen_utc")

    last_seen_ts = _normalize_epoch(raw_last_seen)
    if last_seen_ts is None:
        last_seen_ts = _normalize_epoch(raw_last_seen_utc)

    last_seen_utc = _coerce_iso(raw_last_seen_utc)
    if last_seen_utc is None and isinstance(raw_last_seen, str):
        last_seen_utc = raw_last_seen.strip() or None
    if last_seen_utc is None and last_seen_ts is not None:
        try:
            dt = datetime.fromtimestamp(last_seen_ts, tz=timezone.utc)
            last_seen_utc = dt.isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            last_seen_utc = None

    return last_seen_ts, last_seen_utc


def _coerce_iso(value: Any) -> str | None:
    """Return the value if it is a non-empty string."""

    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None

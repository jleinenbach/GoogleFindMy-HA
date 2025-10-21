# custom_components/googlefindmy/location_recorder.py
"""Location history using Home Assistant's recorder properly."""

import logging
import time
from typing import Any
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.components.recorder import history, get_instance

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
            end_time = datetime.now()
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
                            locations.append(
                                {
                                    "timestamp": state.last_changed.timestamp(),
                                    "latitude": attrs.get(ATTR_LATITUDE),
                                    "longitude": attrs.get(ATTR_LONGITUDE),
                                    "accuracy": attrs.get(
                                        "gps_accuracy", attrs.get("accuracy")
                                    ),
                                    "is_own_report": attrs.get("is_own_report", False),
                                    "altitude": attrs.get("altitude"),
                                    "state": state.state,
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

        def calculate_score(loc):
            """Calculate location score (lower is better)."""
            try:
                accuracy = loc.get("accuracy", float("inf"))
                semantic = loc.get("semantic_name")
                if accuracy is None:
                    if semantic:
                        accuracy = float(0)
                    else:
                        accuracy = float("inf")
                else:
                    accuracy = float(accuracy)

                age_seconds = current_time - loc.get("timestamp", 0)

                # Age penalty: 1m per 3 minutes
                age_penalty = age_seconds / (3 * 60)

                # Heavy penalty for old locations (> 2 hours)
                if age_seconds > 2 * 60 * 60:
                    age_penalty += 100

                # Bonus for own reports
                own_report_bonus = -2 if loc.get("is_own_report") else 0

                return accuracy + age_penalty + own_report_bonus

            except (TypeError, ValueError):
                return float("inf")

        try:
            # Sort by score (best first)
            sorted_locations = sorted(locations, key=calculate_score)
            best = sorted_locations[0]

            age_minutes = (current_time - best.get("timestamp", 0)) / 60
            _LOGGER.debug(
                f"Selected best location: accuracy={best.get('accuracy')}m, "
                f"age={age_minutes:.1f}min from {len(locations)} options"
            )

            return best

        except Exception as e:
            _LOGGER.error(f"Failed to select best location: {e}")
            return locations[0] if locations else {}

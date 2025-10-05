"""Google Home device filtering utilities (options-first, v2.2)."""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional, Tuple

from homeassistant.core import HomeAssistant, State
from homeassistant.components.zone import DOMAIN as ZONE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    GOOGLE_HOME_SPAM_THRESHOLD_MINUTES,
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
)

_LOGGER = logging.getLogger(__name__)


class GoogleHomeFilter:
    """Filter Google Home device detections and prevent 'home' spam events.

    Design goals:
    - Options-first: read filter settings from entry.options with a safe fallback.
    - Backward compatible: accept raw dict config as before.
    - No I/O or blocking calls inside hot paths.
    """

    def __init__(self, hass: HomeAssistant, config_like: Mapping[str, Any] | ConfigEntry) -> None:
        """Initialize the Google Home filter.

        Args:
            hass: Home Assistant instance.
            config_like: Either a plain dict with keys (legacy) or a ConfigEntry.
        """
        self.hass = hass
        self._enabled: bool = True
        self._keywords: list[str] = []
        # Track last detection timestamps per device_id to debounce spam.
        self._spam_tracking: dict[str, float] = {}
        self._spam_threshold: float = float(GOOGLE_HOME_SPAM_THRESHOLD_MINUTES) * 60.0

        # Initial configuration load
        self._apply_from_mapping_or_entry(config_like)

    # ------------------------- Configuration helpers -------------------------

    def _apply_from_mapping_or_entry(self, source: Mapping[str, Any] | ConfigEntry) -> None:
        """Load settings from ConfigEntry (options-first) or from a plain mapping."""
        enabled: Optional[bool] = None
        keywords_raw: Optional[str] = None

        if isinstance(source, ConfigEntry):
            # Options-first: prefer entry.options; fall back to entry.data.
            enabled = source.options.get(
                OPT_GOOGLE_HOME_FILTER_ENABLED,
                source.data.get(OPT_GOOGLE_HOME_FILTER_ENABLED, True),
            )
            keywords_raw = source.options.get(
                OPT_GOOGLE_HOME_FILTER_KEYWORDS,
                source.data.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS),
            )
        else:
            # Legacy dict: read directly, keep defaults aligned with const.py.
            enabled = bool(source.get(OPT_GOOGLE_HOME_FILTER_ENABLED, True))
            keywords_raw = source.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS)

        self._enabled = bool(enabled)
        self._keywords = self._parse_keywords(str(keywords_raw or ""))

        _LOGGER.debug(
            "GoogleHomeFilter loaded (enabled=%s, keywords=%s)", self._enabled, self._keywords
        )

    def apply_from_entry(self, entry: ConfigEntry) -> None:
        """Public helper to (re)load settings from a ConfigEntry."""
        self._apply_from_mapping_or_entry(entry)

    def update_config(self, config_or_entry: Mapping[str, Any] | ConfigEntry) -> None:
        """Update filter configuration (accepts dict or ConfigEntry for compatibility)."""
        self._apply_from_mapping_or_entry(config_or_entry)
        _LOGGER.info(
            "Updated Google Home filter config: enabled=%s, keywords=%s",
            self._enabled,
            self._keywords,
        )

    # ------------------------------ Core logic ------------------------------

    @staticmethod
    def _parse_keywords(keywords_string: str) -> list[str]:
        """Parse comma-separated keywords into a normalized list."""
        if not keywords_string:
            return []
        # Support commas, newlines, and extra whitespace
        parts = [p.strip().lower() for p in keywords_string.replace("\n", ",").split(",")]
        return [p for p in parts if p]

    def is_google_home_device(self, location_name: str | None) -> bool:
        """Return True if the location name matches any configured Google Home keyword."""
        if not self._enabled or not self._keywords or not location_name:
            return False
        location_lower = location_name.lower()
        return any(keyword in location_lower for keyword in self._keywords)

    def get_home_zone_name(self) -> str | None:
        """Return the 'Home' zone display name.

        Strategy:
        - Prefer the canonical 'zone.home' if present.
        - Fallback: any zone with 'home' in entity_id or friendly name.
        - Final fallback: literal "Home".
        """
        try:
            zone_states: list[State] = self.hass.states.async_all(ZONE_DOMAIN)
            for st in zone_states:
                if st.entity_id == "zone.home":
                    return st.attributes.get("friendly_name", "Home")

            for st in zone_states:
                fn = str(st.attributes.get("friendly_name", "")).lower()
                if "home" in st.entity_id.lower() or "home" in fn:
                    return st.attributes.get("friendly_name", "Home")

            return "Home"
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to resolve Home zone name: %s", err)
            return "Home"

    # --------------------------- Entity lookups -----------------------------

    def _find_tracker_entity_id(self, device_id: str) -> str | None:
        """Resolve the device_tracker entity_id for a given Find My device ID.

        We match by the unique_id shape used in the integration: f\"{DOMAIN}_{device_id}\".
        This avoids guessing multiple entity_id formats.
        """
        try:
            reg = er.async_get(self.hass)
            target_unique_id = f"{DOMAIN}_{device_id}"
            for ent in reg.entities.values():
                if (
                    ent.unique_id == target_unique_id
                    and ent.platform == DOMAIN
                    and ent.entity_id.startswith("device_tracker.")
                ):
                    return ent.entity_id
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Entity registry lookup failed for %s: %s", device_id, err)

        # Backward-compatible guesses as a last resort:
        guess = f"device_tracker.{device_id.lower().replace(' ', '_')}"
        if self.hass.states.get(guess):
            return guess
        guess2 = f"device_tracker.{DOMAIN}_{device_id}"
        if self.hass.states.get(guess2):
            return guess2
        return None

    def is_device_at_home(self, device_id: str) -> bool:
        """Return True if the device is currently in the Home zone (state == 'home')."""
        try:
            entity_id = self._find_tracker_entity_id(device_id)
            if not entity_id:
                _LOGGER.debug("No device_tracker entity found for %s", device_id)
                return False
            st = self.hass.states.get(entity_id)
            if not st:
                return False
            return str(st.state).lower() == "home"
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error checking if device %s is at home: %s", device_id, err)
            return False

    # --------------------------- Spam debounce ------------------------------

    def _should_prevent_spam(self, device_id: str) -> bool:
        """Return True if we should debounce repeated detections for this device."""
        last = self._spam_tracking.get(device_id)
        if last is None:
            return False
        return (time.time() - last) < self._spam_threshold

    def _update_spam_tracking(self, device_id: str) -> None:
        """Record the current moment as the last detection time for this device."""
        self._spam_tracking[device_id] = time.time()

    def reset_spam_tracking(self, device_id: str) -> None:
        """Clear spam tracking for a device (e.g., when it leaves home)."""
        if device_id in self._spam_tracking:
            del self._spam_tracking[device_id]
            _LOGGER.debug("Reset spam tracking for %s (left Home zone)", device_id)

    # ---------------------------- Filter decision ---------------------------

    def should_filter_detection(self, device_id: str, location_name: str | None) -> Tuple[bool, str | None]:
        """Return a tuple (should_filter, replacement_location).

        Semantics:
          - If location is already a 'Home' zone: apply spam debounce only.
          - If location matches Google Home device keywords:
              * If device is already at home: apply spam debounce only.
              * If device is NOT at home: substitute the location with the 'Home' zone name.
          - Otherwise: do not filter and do not substitute.
        """
        if not self._enabled:
            return False, None

        # 1) Home zone detection has priority.
        is_home_zone = False
        home_zone_name = self.get_home_zone_name()
        if location_name:
            loc = location_name.strip().lower()
            if home_zone_name:
                is_home_zone = loc in {"home", home_zone_name.strip().lower()}

        if is_home_zone:
            if self._should_prevent_spam(device_id):
                _LOGGER.debug("Filtering 'home' spam for %s at %s", device_id, location_name)
                return True, None
            self._update_spam_tracking(device_id)
            return False, None

        # 2) Google Home device detection?
        is_google_home = self.is_google_home_device(location_name)
        if not is_google_home:
            return False, None

        # 3) If device already at Home → only spam protection.
        if self.is_device_at_home(device_id):
            if self._should_prevent_spam(device_id):
                _LOGGER.debug("Filtering Google Home spam for %s at %s", device_id, location_name)
                return True, None
            self._update_spam_tracking(device_id)
            return False, None

        # 4) Device not at Home → substitute semantic location with the Home zone name.
        home_zone = home_zone_name or "Home"
        _LOGGER.info(
            "Substituting Google Home detection for %s at '%s' with zone '%s'",
            device_id,
            location_name,
            home_zone,
        )
        self._update_spam_tracking(device_id)
        return False, home_zone

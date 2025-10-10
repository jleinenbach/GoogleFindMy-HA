# custom_components/googlefindmy/google_home_filter.py
"""Google Home semantic-location filter (options-first, v2.2).

This module centralizes heuristics to:
- suppress noisy "home"-like detections from Google/Nest/Chromecast speakers,
- optionally substitute obvious Google-Home semantic places with the Home zone,
- debounce repeated "Home" reports within a time window.

Design goals
------------
* **Options-first**: Settings are read from `ConfigEntry.options`, with safe
  fallback to `entry.data` and constants from `const.py`.
* **No I/O in hot paths**: All operations are event-loop friendly.
* **Backward compatible**: Accepts a plain Mapping/dict input for legacy use.

Public API (stable)
-------------------
- `GoogleHomeFilter(hass, config_like)`
- `apply_from_entry(entry)`
- `update_config(config_or_entry)`
- `is_google_home_device(location_name) -> bool`
- `get_home_zone_name() -> str | None`
- `is_device_at_home(device_id) -> bool`
- `reset_spam_tracking(device_id) -> None`
- `should_filter_detection(device_id, location_name) -> tuple[bool, str | None]`
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from homeassistant.components.zone import DOMAIN as ZONE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .const import (
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    DOMAIN,
    GOOGLE_HOME_SPAM_THRESHOLD_MINUTES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
)

_LOGGER = logging.getLogger(__name__)


class GoogleHomeFilter:
    """Filter Google Home device detections and prevent "home" spam events.

    Responsibilities:
      * Keyword matching for semantic places (e.g., "Nest Hub", "Chromecast").
      * Debounce logic for frequent "Home" detections.
      * Optional substitution of such places with the actual Home zone.

    Configuration (options-first):
      * `OPT_GOOGLE_HOME_FILTER_ENABLED` (bool)
      * `OPT_GOOGLE_HOME_FILTER_KEYWORDS` (str; comma-separated) — also accepts list

    Notes:
      * Debounce timing uses `time.monotonic()` (robust against system clock changes).
      * Entity lookups use the registry shortcut `er.async_get_entity_id(...)`
        for clarity and performance.
    """

    __slots__ = (
        "hass",
        "_enabled",
        "_keywords",
        "_spam_tracking",
        "_spam_threshold",
    )

    def __init__(self, hass: HomeAssistant, config_like: Mapping[str, Any] | ConfigEntry) -> None:
        """Initialize the Google Home filter.

        Args:
            hass: Home Assistant instance.
            config_like: Either a `ConfigEntry` (preferred) or a Mapping (legacy).
        """
        self.hass = hass
        self._enabled: bool = True
        self._keywords: list[str] = []
        # Debounce tracking: device_id -> last_seen_monotonic
        self._spam_tracking: dict[str, float] = {}
        self._spam_threshold: float = float(GOOGLE_HOME_SPAM_THRESHOLD_MINUTES) * 60.0

        self._apply_from_mapping_or_entry(config_like)

    # ---------------------------------------------------------------------
    # Configuration helpers
    # ---------------------------------------------------------------------

    def _apply_from_mapping_or_entry(self, source: Mapping[str, Any] | ConfigEntry) -> None:
        """Load settings from ConfigEntry (options-first) or from a plain mapping.

        Accepted keyword formats:
          * Comma-separated string
          * Iterable[str] (list/tuple/set)
        """
        enabled: bool = True
        keywords_raw: Any = DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS

        if isinstance(source, ConfigEntry):
            # Options-first, fallback to data
            enabled = bool(
                source.options.get(
                    OPT_GOOGLE_HOME_FILTER_ENABLED,
                    source.data.get(OPT_GOOGLE_HOME_FILTER_ENABLED, True),
                )
            )
            keywords_raw = source.options.get(
                OPT_GOOGLE_HOME_FILTER_KEYWORDS,
                source.data.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS),
            )
        else:
            enabled = bool(source.get(OPT_GOOGLE_HOME_FILTER_ENABLED, True))
            keywords_raw = source.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS)

        self._enabled = bool(enabled)
        self._keywords = self._normalize_keywords(keywords_raw)

        _LOGGER.debug("GoogleHomeFilter loaded (enabled=%s, keywords=%s)", self._enabled, self._keywords)

    def apply_from_entry(self, entry: ConfigEntry) -> None:
        """(Re)load settings from a ConfigEntry."""
        self._apply_from_mapping_or_entry(entry)

    def update_config(self, config_or_entry: Mapping[str, Any] | ConfigEntry) -> None:
        """Update filter configuration (accepts dict or ConfigEntry)."""
        self._apply_from_mapping_or_entry(config_or_entry)
        _LOGGER.info("Updated Google Home filter config: enabled=%s, keywords=%s", self._enabled, self._keywords)

    # ---------------------------------------------------------------------
    # Normalization / parsing
    # ---------------------------------------------------------------------

    @staticmethod
    def _normalize_keywords(value: Any) -> list[str]:
        """Normalize keywords to a lowercased, de-duplicated list.

        Accepted formats:
          - str: comma/newline separated
          - list|tuple|set of str: each item is one keyword
        """
        items: list[str] = []

        if isinstance(value, str):
            raw = value.replace("\n", ",")
            items = [p.strip().lower() for p in raw.split(",") if p.strip()]
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                if isinstance(v, str) and v.strip():
                    items.append(v.strip().lower())
        elif value is None:
            items = []
        else:
            # Defensive fallback: stringify unknown types
            items = [str(value).strip().lower()] if str(value).strip() else []

        # De-duplicate while preserving order
        seen = set()
        normed: list[str] = []
        for k in items:
            if k not in seen:
                seen.add(k)
                normed.append(k)
        return normed

    @staticmethod
    def _norm_id(device_id: str) -> str:
        """Create a stable key for per-device bookkeeping."""
        return (device_id or "").strip()

    # ---------------------------------------------------------------------
    # Core logic: keyword match, zone name, "am I home?"
    # ---------------------------------------------------------------------

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

    def _find_tracker_entity_id(self, device_id: str) -> str | None:
        """Resolve the device_tracker entity_id for a given Find My device ID.

        Uses the unique_id shape: f"{DOMAIN}_{device_id}" via registry shortcut.
        """
        try:
            reg = er.async_get(self.hass)
            unique_id = f"{DOMAIN}_{device_id}"
            entity_id = reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)
            if entity_id:
                return entity_id
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Entity registry lookup failed for %s: %s", device_id, err)

        # Backward-compatible guesses as a last resort (best effort)
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

    # ---------------------------------------------------------------------
    # Spam debounce (monotonic)
    # ---------------------------------------------------------------------

    def _should_prevent_spam(self, device_id: str) -> bool:
        """Return True if another detection should be debounced for this device."""
        key = self._norm_id(device_id)
        last = self._spam_tracking.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < self._spam_threshold

    def _update_spam_tracking(self, device_id: str) -> None:
        """Record now (monotonic) as last detection for this device."""
        self._spam_tracking[self._norm_id(device_id)] = time.monotonic()

    def reset_spam_tracking(self, device_id: str) -> None:
        """Clear spam tracking for a device (e.g., when it leaves Home)."""
        key = self._norm_id(device_id)
        if key in self._spam_tracking:
            del self._spam_tracking[key]
            _LOGGER.debug("Reset spam tracking for %s (left Home zone)", device_id)

    # ---------------------------------------------------------------------
    # Filter decision
    # ---------------------------------------------------------------------

    def should_filter_detection(self, device_id: str, location_name: str | None) -> tuple[bool, str | None]:
        """Return (should_filter, replacement_location).

        Semantics:
          - If the filter is disabled → (False, None).
          - If `location_name` already represents a Home zone:
              * Apply debounce only (i.e., possibly filter, but never substitute).
          - If `location_name` matches Google-Home device keywords:
              * If the device is already "home": debounce only.
              * If the device is NOT "home": substitute with the Home zone name.
          - Otherwise: do not filter and do not substitute.

        Returns:
            should_filter: True → suppress; False → pass through (optionally substituted).
            replacement_location: Optional zone name to use instead of `location_name`.
        """
        if not self._enabled:
            return False, None

        # 1) Home zone detection has priority
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
        if not self.is_google_home_device(location_name):
            return False, None

        # 3) Device already at Home → debounce only
        if self.is_device_at_home(device_id):
            if self._should_prevent_spam(device_id):
                _LOGGER.debug("Filtering Google Home spam for %s at %s", device_id, location_name)
                return True, None
            self._update_spam_tracking(device_id)
            return False, None

        # 4) Device not at Home → substitute semantic location with the Home zone name
        home_zone = home_zone_name or "Home"
        _LOGGER.info(
            "Substituting Google Home detection for %s at '%s' with zone '%s'",
            device_id,
            location_name,
            home_zone,
        )
        self._update_spam_tracking(device_id)
        return False, home_zone

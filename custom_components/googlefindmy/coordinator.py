# custom_components/googlefindmy/coordinator.py
"""Data coordinator for Google Find My Device (async-first, HA-friendly)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol

from homeassistant.components.recorder import (
    get_instance as get_recorder,
    history as recorder_history,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
    # HA session is provided by the integration and reused across I/O
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import GoogleFindMyAPI
from .const import DOMAIN, UPDATE_INTERVAL, LOCATION_REQUEST_TIMEOUT_S

_LOGGER = logging.getLogger(__name__)


class CacheProtocol(Protocol):
    """Defines the interface for a cache that the coordinator can use.

    This protocol ensures that any cache object passed to the coordinator
    provides the necessary asynchronous methods for getting and setting values.
    """
    async def async_get_cached_value(self, key: str) -> Any: ...
    async def async_set_cached_value(self, key: str, value: Any) -> None: ...


# -------------------------------------------------------------------------
# Synchronous history helper (runs in Recorder executor)
# -------------------------------------------------------------------------
def _sync_get_last_gps_from_history(
    hass: HomeAssistant, entity_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch last state change with GPS coordinates via Recorder History (sync).

    This function is designed to be run in a worker thread (specifically the
    Home Assistant Recorder's executor) to avoid blocking the event loop with
    database queries.

    IMPORTANT:
    - Must run in a worker (Recorder executor). Do not call asyncio APIs here.
    - Keep queries minimal to avoid heavy DB load.

    Args:
        hass: The Home Assistant instance.
        entity_id: The entity ID of the device_tracker to query.

    Returns:
        A dictionary containing the last known location data, or None if not found.
    """
    try:
        # Minimal query: the last single change for this entity_id
        changes = recorder_history.get_last_state_changes(hass, 1, entity_id)
        samples = changes.get(entity_id, [])
        if not samples:
            return None

        last_state = samples[-1]
        attrs = getattr(last_state, "attributes", {}) or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat is None or lon is None:
            return None

        return {
            "latitude": lat,
            "longitude": lon,
            "accuracy": attrs.get("gps_accuracy"),
            "last_seen": int(last_state.last_updated.timestamp()),
            "status": "Using historical data",
        }
    except Exception as err:
        _LOGGER.debug("History lookup failed for %s: %s", entity_id, err)
        return None


class GoogleFindMyCoordinator(DataUpdateCoordinator[List[Dict[str, Any]]]):
    """Coordinator that manages polling, cache, and push updates for Google Find My Device."""

    # ---------------------------- Lifecycle ---------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        cache: CacheProtocol,
        tracked_devices: Optional[List[str]] = None,
        location_poll_interval: int = 300,
        device_poll_delay: int = 5,
        min_poll_interval: int = 1,
        min_accuracy_threshold: int = 100,
        allow_history_fallback: bool = False,
    ) -> None:
        """Initialize the coordinator.

        This sets up the central data management for the integration, including
        API communication, state caching, and polling logic.

        Notes:
            - Credentials and related metadata are provided via the entry-scoped TokenCache.
            - The HA-managed aiohttp ClientSession is reused to avoid per-call pools.

        Args:
            hass: The Home Assistant instance.
            cache: An object implementing the CacheProtocol for persistent storage.
            tracked_devices: A list of device IDs to be tracked.
            location_poll_interval: The interval in seconds between polling cycles.
            device_poll_delay: The delay in seconds between polling individual devices.
            min_poll_interval: The minimum allowed interval between polling cycles.
            min_accuracy_threshold: The minimum GPS accuracy in meters to accept a location.
            allow_history_fallback: Whether to fall back to recorder history for location.
        """
        self.hass = hass
        self._cache = cache

        # Get the singleton aiohttp.ClientSession from Home Assistant and reuse it.
        self._session = async_get_clientsession(hass)
        self.api = GoogleFindMyAPI(cache=self._cache, session=self._session)

        # Configuration (user options; updated via update_settings())
        self.tracked_devices = list(tracked_devices or [])
        self.location_poll_interval = int(location_poll_interval)
        self.device_poll_delay = int(device_poll_delay)
        self.min_poll_interval = int(min_poll_interval)  # hard lower bound between cycles
        self._min_accuracy_threshold = int(min_accuracy_threshold)  # quality filter (meters)
        self.allow_history_fallback = bool(allow_history_fallback)

        # Internal caches & bookkeeping
        self._device_location_data: Dict[str, Dict[str, Any]] = {}  # device_id -> location dict
        self._device_names: Dict[str, str] = {}  # device_id -> human name
        self._device_caps: Dict[str, Dict[str, Any]] = {}  # device_id -> caps (e.g., {"can_ring": True})

        # Polling state
        self._poll_lock = asyncio.Lock()
        self._is_polling = False
        self._startup_complete = False
        self._last_poll_mono: float = 0.0  # monotonic timestamp for scheduling

        # Push readiness memoization and cooldown after transport errors
        self._push_ready_memo: Optional[bool] = None
        self._push_cooldown_until: float = 0.0

        # Statistics (extend as needed)
        self.stats: Dict[str, int] = {
            "skipped_duplicates": 0,
            "background_updates": 0,  # FCM/push-driven updates
            "polled_updates": 0,      # sequential poll-driven updates
            "crowd_sourced_updates": 0,
            "history_fallback_used": 0,
            "timeouts": 0,
            "invalid_coords": 0,
            "low_quality_dropped": 0,
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Debounced stats persistence (avoid flushing on every increment)
        self._stats_save_task: Optional[asyncio.Task] = None
        self._stats_debounce_seconds: float = 5.0

        # Load persistent statistics asynchronously (name the task for better debugging)
        hass.async_create_task(self._async_load_stats(), name=f"{DOMAIN}.load_stats")

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # Public read-only state for diagnostics/UI
    @property
    def is_polling(self) -> bool:
        """Expose current polling state (public read-only API).

        Returns:
            True if a polling cycle is currently in progress.
        """
        return self._is_polling

    # ---------------------------- HA Coordinator ----------------------------
    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        This method must be quick and non-blocking: it snapshots current cache,
        updates device metadata from the lightweight device list, and schedules
        a background poll cycle when the interval has elapsed.

        Returns:
            A list of dictionaries, where each dictionary represents a device's state.

        Raises:
            ConfigEntryAuthFailed: If authentication fails during device list fetching.
            UpdateFailed: For other transient or unexpected errors.
        """
        try:
            # 1) Always fetch the lightweight device list using native async API (no executor)
            all_devices = await self.api.async_get_basic_device_list()

            # 2) Filter to tracked devices if explicitly configured
            if self.tracked_devices:
                devices = [d for d in all_devices if d["id"] in self.tracked_devices]
            else:
                devices = all_devices or []

            # 3) Update internal device name and capability caches
            for dev in devices:
                dev_id = dev["id"]
                self._device_names[dev_id] = dev.get("name", dev_id)

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 4) Decide whether to trigger a poll cycle (monotonic clock)
            now_mono = time.monotonic()
            effective_interval = max(self.location_poll_interval, self.min_poll_interval)

            if not self._startup_complete:
                # Defer the first poll to avoid startup load; it will run after the first interval.
                self._startup_complete = True
                self._last_poll_mono = now_mono
                _LOGGER.debug("First startup - set poll baseline; next poll follows normal schedule")
            else:
                due = (now_mono - self._last_poll_mono) >= effective_interval
                if due and not self._is_polling and devices:
                    _LOGGER.debug(
                        "Scheduling background polling cycle (devices=%d, interval=%ds)",
                        len(devices),
                        effective_interval,
                    )
                    self.hass.async_create_task(
                        self._async_start_poll_cycle(devices),
                        name=f"{DOMAIN}.poll_cycle",
                    )
                else:
                    _LOGGER.debug(
                        "Poll not due (elapsed=%.1fs/%ss) or already running=%s",
                        now_mono - self._last_poll_mono,
                        effective_interval,
                        self._is_polling,
                    )

            # 5) Build data snapshot from cache and (optionally) minimal fallbacks
            snapshot = await self._async_build_device_snapshot_with_fallbacks(devices)
            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(max(0, effective_interval - (time.monotonic() - self._last_poll_mono))),
            )
            return snapshot

        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed:
            # Surface up to HA to trigger re-auth flow; do not wrap into UpdateFailed
            raise
        except UpdateFailed:
            # Let pre-wrapped UpdateFailed bubble as-is
            raise
        except Exception as exc:
            # Coordinator contract: raise UpdateFailed on unexpected errors
            raise UpdateFailed(exc) from exc

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(self, devices: List[Dict[str, Any]]) -> None:
        """Run a full sequential polling cycle in a background task.

        This runs with a lock to avoid overlapping cycles, updates the
        internal cache, and pushes snapshots at start and end.

        Args:
            devices: A list of device dictionaries to poll.
        """
        if not devices:
            return

        async with self._poll_lock:
            if self._is_polling:
                return

            self._is_polling = True
            # Push a snapshot from cache to signal "polling" state to listeners
            start_snapshot = self._build_snapshot_from_cache(devices, wall_now=time.time())
            self.async_set_updated_data(start_snapshot)
            _LOGGER.info("Starting sequential poll of %d devices", len(devices))

            try:
                for idx, dev in enumerate(devices):
                    dev_id = dev["id"]
                    dev_name = dev.get("name", dev_id)
                    _LOGGER.info(
                        "Sequential poll: requesting location for %s (%d/%d)",
                        dev_name,
                        idx + 1,
                        len(devices),
                    )

                    try:
                        # Protect API awaitable with timeout
                        location = await asyncio.wait_for(
                            self.api.async_get_device_location(dev_id, dev_name),
                            timeout=LOCATION_REQUEST_TIMEOUT_S,
                        )

                        if not location:
                            _LOGGER.warning("No location data returned for %s", dev_name)
                            continue

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        semantic_name = location.get("semantic_name")
                        if semantic_name and hasattr(self, "google_home_filter"):
                            try:
                                should_filter, replacement_location = self.google_home_filter.should_filter_detection(
                                    dev_id, semantic_name
                                )
                            except Exception as gf_err:
                                _LOGGER.debug(
                                    "Google Home filter error for %s: %s", dev_name, gf_err
                                )
                            else:
                                if should_filter:
                                    _LOGGER.debug(
                                        "Filtering out Google Home spam detection for %s", dev_name
                                    )
                                    continue
                                if replacement_location:
                                    _LOGGER.info(
                                        "Google Home filter: %s detected at '%s', using '%s'",
                                        dev_name,
                                        semantic_name,
                                        replacement_location,
                                    )
                                    location = dict(location)
                                    location["semantic_name"] = replacement_location
                        # ------------------------------------------------------------------

                        lat = location.get("latitude")
                        lon = location.get("longitude")
                        acc = location.get("accuracy")
                        last_seen = location.get("last_seen", 0)

                        # If we only got a semantic location, preserve previous coordinates.
                        if (lat is None or lon is None) and location.get("semantic_name"):
                            prev = self._device_location_data.get(dev_id, {})
                            if prev:
                                location["latitude"] = prev.get("latitude")
                                location["longitude"] = prev.get("longitude")
                                location["accuracy"] = prev.get("accuracy")
                                location["status"] = (
                                    "Semantic location; preserving previous coordinates"
                                )

                        # Validate coordinates
                        lat = location.get("latitude")
                        lon = location.get("longitude")
                        if not (
                            isinstance(lat, (int, float))
                            and isinstance(lon, (int, float))
                            and -90 <= lat <= 90
                            and -180 <= lon <= 180
                        ):
                            _LOGGER.warning(
                                "Invalid or out-of-range coordinates for %s: lat=%s, lon=%s",
                                dev_name,
                                lat,
                                lon,
                            )
                            self.increment_stat("invalid_coords")
                            continue

                        # Accuracy quality filter
                        if (
                            isinstance(self._min_accuracy_threshold, int)
                            and self._min_accuracy_threshold > 0
                            and isinstance(acc, (int, float))
                            and acc > self._min_accuracy_threshold
                        ):
                            _LOGGER.debug(
                                "Dropping low-quality fix for %s (accuracy=%sm > %sm)",
                                dev_name,
                                acc,
                                self._min_accuracy_threshold,
                            )
                            self.increment_stat("low_quality_dropped")
                            continue

                        # De-duplicate by identical last_seen
                        existing = self._device_location_data.get(dev_id, {})
                        existing_last_seen = existing.get("last_seen")
                        if existing_last_seen == last_seen and last_seen:
                            _LOGGER.debug(
                                "Skipping duplicate location for %s (last_seen=%s)",
                                dev_name,
                                last_seen,
                            )
                            self.increment_stat("skipped_duplicates")
                            continue

                        # Age diagnostics (informational)
                        wall_now = time.time()
                        if last_seen:
                            age_hours = max(0.0, (wall_now - float(last_seen)) / 3600.0)
                            if age_hours > 24:
                                _LOGGER.info(
                                    "Using old location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )
                            elif age_hours > 1:
                                _LOGGER.debug(
                                    "Using location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )

                        # Commit to cache and bump statistics
                        location["last_updated"] = wall_now  # wall-clock for UX
                        self._device_location_data[dev_id] = location
                        self.increment_stat("polled_updates")

                    except asyncio.TimeoutError:
                        _LOGGER.info(
                            "Location request timed out for %s after %s seconds",
                            dev_name,
                            LOCATION_REQUEST_TIMEOUT_S,
                        )
                        self.increment_stat("timeouts")
                    except Exception as err:
                        _LOGGER.error("Failed to get location for %s: %s", dev_name, err)

                    # Inter-device delay (except after the last one)
                    if idx < len(devices) - 1 and self.device_poll_delay > 0:
                        await asyncio.sleep(self.device_poll_delay)

                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
            finally:
                # Update scheduling baseline and clear flag, then push end snapshot
                self._last_poll_mono = time.monotonic()
                self._is_polling = False
                end_snapshot = self._build_snapshot_from_cache(
                    devices, wall_now=time.time()
                )
                self.async_set_updated_data(end_snapshot)

    # ---------------------------- Snapshot helpers --------------------------
    def _build_base_snapshot_entry(self, device_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Create the base snapshot entry for a device (no cache lookups here).

        This centralizes the common fields to keep snapshot builders DRY.

        Args:
            device_dict: A dictionary containing basic device info (id, name).

        Returns:
            A dictionary with default fields for a device snapshot.
        """
        dev_id = device_dict["id"]
        dev_name = device_dict.get("name", dev_id)
        return {
            "name": dev_name,
            "id": dev_id,
            "device_id": dev_id,
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "accuracy": None,
            "last_seen": None,
            "status": "Waiting for location poll",
            "is_own_report": None,
            "semantic_name": None,
            "battery_level": None,
        }

    def _update_entry_from_cache(self, entry: Dict[str, Any], wall_now: float) -> bool:
        """Update the given snapshot entry in place from the in-memory cache.

        Args:
            entry: The device snapshot entry to update.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            True if the cache contained data for this device and the entry was updated, else False.
        """
        dev_id = entry["device_id"]
        cached = self._device_location_data.get(dev_id)
        if not cached:
            return False

        entry.update(cached)
        last_updated_ts = cached.get("last_updated", 0)
        age = max(0.0, wall_now - float(last_updated_ts))
        if age < self.location_poll_interval:
            entry["status"] = "Location data current"
        elif age < self.location_poll_interval * 2:
            entry["status"] = "Location data aging"
        else:
            entry["status"] = "Location data stale"
        return True

    def _build_snapshot_from_cache(
        self, devices: List[Dict[str, Any]], wall_now: float
    ) -> List[Dict[str, Any]]:
        """Build a lightweight snapshot using only the in-memory cache.

        This never touches HA state or the database; it is safe in background tasks.

        Args:
            devices: A list of device dictionaries to include in the snapshot.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            A list of device state dictionaries built from the cache.
        """
        snapshot: List[Dict[str, Any]] = []
        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)
            # If cache has info, update status accordingly; otherwise keep default status.
            self._update_entry_from_cache(entry, wall_now)
            snapshot.append(entry)
        return snapshot

    async def _async_build_device_snapshot_with_fallbacks(
        self, devices: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build a snapshot using cache, HA state and (optionally) loud history fallback.

        Args:
            devices: A list of device dictionaries to build the snapshot for.

        Returns:
            A complete list of device state dictionaries with fallbacks applied.
        """
        snapshot: List[Dict[str, Any]] = []
        wall_now = time.time()
        ent_reg = er.async_get(self.hass)

        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)

            # Prefer cached result
            if self._update_entry_from_cache(entry, wall_now):
                snapshot.append(entry)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            dev_id = entry["device_id"]
            unique_id = f"{DOMAIN}_{dev_id}"
            entity_id = ent_reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)
            if not entity_id:
                _LOGGER.debug(
                    "No entity registry entry for device '%s' (unique_id=%s); skipping any fallback.",
                    entry["name"],
                    unique_id,
                )
                snapshot.append(entry)
                continue

            state = self.hass.states.get(entity_id)
            if state:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                acc = state.attributes.get("gps_accuracy")
                if lat is not None and lon is not None:
                    entry.update(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "accuracy": acc,
                            "last_seen": int(state.last_updated.timestamp()),
                            "status": "Using current state",
                        }
                    )
                    snapshot.append(entry)
                    continue

            # Optional loud history fallback
            if self.allow_history_fallback:
                _LOGGER.warning(
                    "No live state for %s (entity_id=%s); attempting history fallback via Recorder.",
                    entry["name"],
                    entity_id,
                )
                rec = get_recorder(self.hass)
                result = await rec.async_add_executor_job(
                    _sync_get_last_gps_from_history, self.hass, entity_id
                )
                if result:
                    entry.update(result)
                    self.increment_stat("history_fallback_used")
                else:
                    _LOGGER.warning(
                        "No historical GPS data found for %s (entity_id=%s). "
                        "Entity may be excluded from Recorder.",
                        entry["name"],
                        entity_id,
                    )

            snapshot.append(entry)

        return snapshot

    # ---------------------------- Stats persistence -------------------------
    async def _async_load_stats(self) -> None:
        """Load statistics from entry-scoped cache."""
        try:
            cached = await self._cache.async_get_cached_value("integration_stats")
            if cached and isinstance(cached, dict):
                for key in self.stats.keys():
                    if key in cached:
                        self.stats[key] = cached[key]
                _LOGGER.debug("Loaded statistics from cache: %s", self.stats)
        except Exception as err:
            _LOGGER.debug("Failed to load statistics from cache: %s", err)

    async def _async_save_stats(self) -> None:
        """Persist statistics to entry-scoped cache."""
        try:
            await self._cache.async_set_cached_value("integration_stats", self.stats.copy())
        except Exception as err:
            _LOGGER.debug("Failed to save statistics to cache: %s", err)

    async def _debounced_save_stats(self) -> None:
        """Debounce wrapper to coalesce frequent stat updates into a single write."""
        try:
            await asyncio.sleep(self._stats_debounce_seconds)
            await self._async_save_stats()
        except asyncio.CancelledError:
            # Expected if a new increment arrives before the delay elapses; do nothing.
            return
        except Exception as err:
            _LOGGER.debug("Debounced stats save failed: %s", err)

    def _schedule_stats_persist(self) -> None:
        """(Re)schedule a debounced persistence task for statistics."""
        # Cancel a pending writer, if any, and schedule a fresh one.
        if self._stats_save_task and not self._stats_save_task.done():
            self._stats_save_task.cancel()
        self._stats_save_task = self.hass.async_create_task(
            self._debounced_save_stats(), name=f"{DOMAIN}.save_stats_debounced"
        )

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter and schedule debounced persistence.

        Args:
            stat_name: The name of the statistic to increment.
        """
        if stat_name in self.stats:
            before = self.stats[stat_name]
            self.stats[stat_name] = before + 1
            _LOGGER.debug(
                "Incremented %s from %s to %s", stat_name, before, self.stats[stat_name]
            )
            self._schedule_stats_persist()
        else:
            _LOGGER.warning(
                "Tried to increment unknown stat '%s'; available=%s",
                stat_name,
                list(self.stats.keys()),
            )

    # ---------------------------- Public platform API -----------------------
    def get_device_location_data(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return current cached location dict for a device (or None).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary of location data or None if not found.
        """
        return self._device_location_data.get(device_id)

    def prime_device_location_cache(self, device_id: str, data: Dict[str, Any]) -> None:
        """Seed/update the location cache for a device with (lat/lon/accuracy).

        Args:
            device_id: The canonical ID of the device.
            data: A dictionary containing latitude, longitude, and accuracy.
        """
        slot = self._device_location_data.get(device_id, {})
        slot.update(
            {k: v for k, v in data.items() if k in ("latitude", "longitude", "accuracy")}
        )
        # Do not set last_updated/last_seen here; this is only priming.
        self._device_location_data[device_id] = slot

    def seed_device_last_seen(self, device_id: str, ts_epoch: float) -> None:
        """Seed last_seen (epoch seconds) without overriding fresh data.

        Args:
            device_id: The canonical ID of the device.
            ts_epoch: The timestamp in epoch seconds.
        """
        slot = self._device_location_data.setdefault(device_id, {})
        slot.setdefault("last_seen", float(ts_epoch))

    def update_device_cache(self, device_id: str, location_data: Dict[str, Any]) -> None:
        """Public, encapsulated update of the internal location cache for one device.

        Used by the FCM receiver (push path). Expects validated fields.

        Args:
            device_id: The canonical ID of the device.
            location_data: The new location data dictionary.
        """
        if not isinstance(location_data, dict):
            _LOGGER.debug("Ignored cache update for %s: payload is not a dict", device_id)
            return

        # Shallow copy to avoid caller-side mutation
        slot = dict(location_data)

        # Ensure last_updated is present
        slot.setdefault("last_updated", time.time())

        # Keep human-friendly name mapping up-to-date if provided alongside
        name = slot.get("name")
        if isinstance(name, str) and name:
            self._device_names[device_id] = name

        self._device_location_data[device_id] = slot

    def get_device_last_seen(self, device_id: str) -> Optional[datetime]:
        """Return last_seen as timezone-aware datetime (UTC) if cached.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A timezone-aware datetime object or None.
        """
        ts = self._device_location_data.get(device_id, {}).get("last_seen")
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            return None

    def get_device_display_name(self, device_id: str) -> Optional[str]:
        """Return the human-readable device name if known.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            The display name as a string, or None.
        """
        return self._device_names.get(device_id)

    def get_device_name_map(self) -> Dict[str, str]:
        """Return a shallow copy of the internal device-id -> name mapping.

        Returns:
            A dictionary mapping device IDs to their names.
        """
        return dict(self._device_names)

    # ---------------------------- Push updates ------------------------------
    def push_updated(self, device_ids: Optional[List[str]] = None) -> None:
        """Publish a fresh snapshot to listeners after push (FCM) cache updates.

        This **does not** trigger a poll. It:
        - Immediately pushes cache state to entities via `async_set_updated_data()`.
        - Resets the internal poll baseline to 'now' to prevent an immediate re-poll.
        - Optionally limits the snapshot to `device_ids`; otherwise includes all known devices.

        Args:
            device_ids: An optional list of device IDs to include in the update.
        """
        wall_now = time.time()
        self._last_poll_mono = time.monotonic()  # reset poll timer

        # Choose device ids for the snapshot
        if device_ids:
            ids = device_ids
        else:
            # union of all known names and cached locations
            ids = list({*self._device_names.keys(), *self._device_location_data.keys()})

        # Respect tracked_devices semantics: empty list => include all
        if self.tracked_devices:
            ids = [d for d in ids if d in self.tracked_devices]

        # Build "devices" stubs from id->name mapping
        devices_stub: List[Dict[str, Any]] = [
            {"id": dev_id, "name": self._device_names.get(dev_id, dev_id)} for dev_id in ids
        ]

        snapshot = self._build_snapshot_from_cache(devices_stub, wall_now=wall_now)
        self.async_set_updated_data(snapshot)
        _LOGGER.debug("Pushed snapshot for %d device(s) via push_updated()", len(snapshot))

    # ---------------------------- Play sound helpers ------------------------
    def _api_push_ready(self) -> bool:
        """Best-effort check whether push/FCM is initialized (backward compatible).

        Optimistic default: if we cannot determine readiness explicitly,
        return True so the UI stays usable; the API call will enforce reality.

        Returns:
            True if the push mechanism is believed to be ready.
        """
        # Short-circuit via cooldown window after a transport failure.
        now = time.monotonic()
        if now < self._push_cooldown_until:
            if self._push_ready_memo is not False:
                _LOGGER.debug("Push readiness: cooldown active -> treating as not ready")
            self._push_ready_memo = False
            return False

        ready: Optional[bool] = None
        try:
            fn = getattr(self.api, "is_push_ready", None)
            if callable(fn):
                ready = bool(fn())
            else:
                for attr in ("push_ready", "fcm_ready", "receiver_ready"):
                    val = getattr(self.api, attr, None)
                    if isinstance(val, bool):
                        ready = val
                        break
                if ready is None:
                    fcm = getattr(self.api, "fcm", None)
                    if fcm is not None:
                        for attr in ("is_ready", "ready"):
                            val = getattr(fcm, attr, None)
                            if isinstance(val, bool):
                                ready = val
                                break
        except Exception as err:
            _LOGGER.debug(
                "Push readiness check exception: %s (defaulting optimistic True)", err
            )
            ready = True

        if ready is None:
            ready = True  # optimistic default

        if ready != self._push_ready_memo:
            _LOGGER.debug("Push readiness changed: %s", ready)
            self._push_ready_memo = ready

        return ready

    def _note_push_transport_problem(self, cooldown_s: int = 90) -> None:
        """Enter a temporary cooldown after a push transport failure to avoid spamming.

        Args:
            cooldown_s: The duration of the cooldown in seconds.
        """
        self._push_cooldown_until = time.monotonic() + cooldown_s
        self._push_ready_memo = False
        _LOGGER.debug("Entering push cooldown for %ss after transport failure", cooldown_s)

    def can_play_sound(self, device_id: str) -> bool:
        """Return True if 'Play Sound' should be enabled for the device.

        **No network in availability path.**
        Strategy:
        - If capability is known from the lightweight device list -> use it (fast, cached).
        - If push readiness is explicitly False -> disable.
        - Otherwise -> optimistic True (known devices) to keep the UI usable.
          The actual action enforces reality and will start a cooldown on failure.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if playing a sound is likely possible.
        """
        # 1) Use cached capability when available (fast path, no network).
        caps = self._device_caps.get(device_id)
        if caps and isinstance(caps.get("can_ring"), bool):
            res = bool(caps["can_ring"])
            _LOGGER.debug("can_play_sound(%s) -> %s (from capability can_ring)", device_id, res)
            return res

        # 2) Short-circuit if push transport is not ready.
        ready = self._api_push_ready()
        if ready is False:
            _LOGGER.debug("can_play_sound(%s) -> False (push not ready)", device_id)
            return False

        # 3) Optimistic final decision based on whether we know the device.
        is_known = (
            device_id in self._device_names or device_id in self._device_location_data
        )
        if is_known:
            _LOGGER.debug(
                "can_play_sound(%s) -> True (optimistic; known device, push_ready=%s)",
                device_id,
                ready,
            )
            return True

        _LOGGER.debug("can_play_sound(%s) -> True (optimistic final fallback)", device_id)
        return True

    # ---------------------------- Public control API -----------------------
    def update_settings(
        self,
        *,
        tracked_devices: Optional[List[str]] = None,
        location_poll_interval: Optional[int] = None,
        device_poll_delay: Optional[int] = None,
        min_poll_interval: Optional[int] = None,
        min_accuracy_threshold: Optional[int] = None,
        allow_history_fallback: Optional[bool] = None,
    ) -> None:
        """Apply updated user settings provided by the config entry (options-first).

        This method deliberately enforces basic typing/limits to keep the coordinator sane
        regardless of where the values came from.

        Args:
            tracked_devices: A list of device IDs to track.
            location_poll_interval: The interval in seconds for location polling.
            device_poll_delay: The delay in seconds between polling devices.
            min_poll_interval: The minimum polling interval in seconds.
            min_accuracy_threshold: The minimum accuracy in meters.
            allow_history_fallback: Whether to allow falling back to recorder history.
        """
        if tracked_devices is not None:
            self.tracked_devices = list(tracked_devices)

        if location_poll_interval is not None:
            try:
                self.location_poll_interval = max(1, int(location_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid location_poll_interval=%r", location_poll_interval
                )

        if device_poll_delay is not None:
            try:
                self.device_poll_delay = max(0, int(device_poll_delay))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid device_poll_delay=%r", device_poll_delay)

        if min_poll_interval is not None:
            try:
                self.min_poll_interval = max(1, int(min_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid min_poll_interval=%r", min_poll_interval)

        if min_accuracy_threshold is not None:
            try:
                self._min_accuracy_threshold = max(0, int(min_accuracy_threshold))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid min_accuracy_threshold=%r", min_accuracy_threshold
                )

        if allow_history_fallback is not None:
            self.allow_history_fallback = bool(allow_history_fallback)

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

    # ---------------------------- Passthrough API ---------------------------
    async def async_locate_device(self, device_id: str) -> Dict[str, Any]:
        """Locate a device using the native async API (no executor).

        Args:
            device_id: The canonical ID of the device to locate.

        Returns:
            A dictionary containing the location data.
        """
        name = self.get_device_display_name(device_id) or device_id
        return await self.api.async_get_device_location(device_id, name)

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device using the native async API (no executor).

        Guard with can_play_sound(); on failure, start a short cooldown to avoid repeated errors.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
        if not self.can_play_sound(device_id):
            _LOGGER.debug(
                "Suppressing play_sound call for %s: capability/push not ready",
                device_id,
            )
            return False
        try:
            ok = await self.api.async_play_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            return bool(ok)
        except Exception as err:
            _LOGGER.debug(
                "async_play_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self._note_push_transport_problem()
            return False

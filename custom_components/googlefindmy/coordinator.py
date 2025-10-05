"""Data coordinator for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.components.recorder import (
    get_instance as get_recorder,
    history as recorder_history,
)

from .const import DOMAIN, UPDATE_INTERVAL
from .api import GoogleFindMyAPI

_LOGGER = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Synchronous history helper (runs in Recorder executor)
# -------------------------------------------------------------------------
def _sync_get_last_gps_from_history(
    hass: HomeAssistant, entity_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch last state change with GPS coordinates via Recorder History (sync).

    IMPORTANT:
    - Must run in a worker (Recorder executor). Do not call asyncio APIs here.
    - Keep queries minimal to avoid heavy DB load.
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
    """Coordinator that manages polling and cache for Google Find My Device."""

    # ---------------------------- Lifecycle ---------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        secrets_data: Optional[dict],
        tracked_devices: Optional[List[str]] = None,
        location_poll_interval: int = 300,
        device_poll_delay: int = 5,
        min_poll_interval: int = 120,
        min_accuracy_threshold: int = 100,
        allow_history_fallback: bool = False,
    ) -> None:
        """Initialize the coordinator.

        Note: The API reads credentials from the token_cache. `secrets_data` may be None
        when the integration was configured with individual credentials; in that case
        `__init__.py` already primed the token cache prior to constructing the API here.
        """
        self.hass = hass
        self.api = GoogleFindMyAPI(secrets_data=secrets_data)

        # Configuration (user options; updated via update_settings())
        self.tracked_devices = list(tracked_devices or [])
        self.location_poll_interval = int(location_poll_interval)
        self.device_poll_delay = int(device_poll_delay)
        self.min_poll_interval = int(min_poll_interval)  # hard lower bound between cycles
        self._min_accuracy_threshold = int(min_accuracy_threshold)  # quality filter (meters)
        self.allow_history_fallback = bool(allow_history_fallback)

        # Internal cache & bookkeeping
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
            "background_updates": 0,
            "crowd_sourced_updates": 0,
            "history_fallback_used": 0,
            "timeouts": 0,
            "invalid_coords": 0,
            "low_quality_dropped": 0,
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Load persistent statistics asynchronously
        hass.async_create_task(self._async_load_stats())

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # ---------------------------- HA Coordinator ----------------------------
    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        This method must be quick and non-blocking: it snapshots current cache,
        updates device metadata from the lightweight device list, and schedules
        a background poll cycle when the interval elapsed.
        """
        try:
            # 1) Always fetch the lightweight device list (sync API; run in executor)
            all_devices = await self.hass.async_add_executor_job(
                self.api.get_basic_device_list
            )

            # 2) Filter to tracked devices if explicitly configured
            if self.tracked_devices:
                devices = [d for d in all_devices if d["id"] in self.tracked_devices]
            else:
                devices = all_devices or []

            # 3) Update device names and normalize capabilities
            for dev in devices:
                dev_id = dev["id"]
                self._device_names[dev_id] = dev.get("name", dev_id)

                # Normalize "can ring" capability across possible API shapes
                can_ring = None
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                elif isinstance(dev.get("capabilities"), (list, set, tuple)):
                    caps = {str(x).lower() for x in dev["capabilities"]}
                    can_ring = ("ring" in caps) or ("play_sound" in caps)
                elif isinstance(dev.get("capabilities"), dict):
                    caps = {str(k).lower(): v for k, v in dev["capabilities"].items()}
                    can_ring = bool(caps.get("ring")) or bool(caps.get("play_sound"))
                if can_ring is not None:
                    slot = self._device_caps.get(dev_id, {})
                    slot["can_ring"] = bool(can_ring)
                    self._device_caps[dev_id] = slot

            # 4) Decide whether to trigger a poll cycle (monotonic clock)
            now_mono = time.monotonic()
            effective_interval = max(self.location_poll_interval, self.min_poll_interval)

            if not self._startup_complete:
                # First run: set baseline, do not poll immediately
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
                    self.hass.async_create_task(self._async_start_poll_cycle(devices))
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

        except Exception as exc:
            # Coordinator contract: raise UpdateFailed on unexpected errors
            raise UpdateFailed(exc) from exc

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(self, devices: List[Dict[str, Any]]) -> None:
        """Run a full sequential polling cycle in a background task.

        This runs with a lock to avoid overlapping cycles, updates the
        internal cache, and pushes snapshots at start and end.
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
                            timeout=30.0,
                        )

                        if not location:
                            _LOGGER.warning("No location data returned for %s", dev_name)
                            continue

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
                        if lat is None or lon is None:
                            _LOGGER.warning(
                                "Invalid coordinates for %s: lat=%s, lon=%s",
                                dev_name,
                                lat,
                                lon,
                            )
                            self.increment_stat("invalid_coords")
                            continue

                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            _LOGGER.warning(
                                "Coordinates out of range for %s: lat=%s, lon=%s",
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
                        self.increment_stat("background_updates")

                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "Location request timed out for %s after 30 seconds",
                            dev_name,
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
                end_snapshot = self._build_snapshot_from_cache(devices, wall_now=time.time())
                self.async_set_updated_data(end_snapshot)

    # ---------------------------- Snapshot helpers --------------------------
    def _build_snapshot_from_cache(
        self, devices: List[Dict[str, Any]], wall_now: float
    ) -> List[Dict[str, Any]]:
        """Build a lightweight snapshot using only the in-memory cache.

        This never touches HA state or the database; it is safe in background tasks.
        """
        snapshot: List[Dict[str, Any]] = []
        for dev in devices:
            dev_id = dev["id"]
            dev_name = dev.get("name", dev_id)
            info: Dict[str, Any] = {
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
            cached = self._device_location_data.get(dev_id)
            if cached:
                info.update(cached)
                last_updated_ts = cached.get("last_updated", 0)
                age = max(0.0, wall_now - float(last_updated_ts))
                if age < self.location_poll_interval:
                    info["status"] = "Location data current"
                elif age < self.location_poll_interval * 2:
                    info["status"] = "Location data aging"
                else:
                    info["status"] = "Location data stale"
            snapshot.append(info)
        return snapshot

    async def _async_build_device_snapshot_with_fallbacks(
        self, devices: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build a snapshot using cache, HA state and (optionally) loud history fallback."""
        snapshot: List[Dict[str, Any]] = []
        wall_now = time.time()
        ent_reg = er.async_get(self.hass)

        for dev in devices:
            dev_id = dev["id"]
            dev_name = dev.get("name", dev_id)

            info: Dict[str, Any] = {
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

            # Prefer cached result
            cached = self._device_location_data.get(dev_id)
            if cached:
                info.update(cached)
                last_updated_ts = cached.get("last_updated", 0)
                age = max(0.0, wall_now - float(last_updated_ts))
                if age < self.location_poll_interval:
                    info["status"] = "Location data current"
                elif age < self.location_poll_interval * 2:
                    info["status"] = "Location data aging"
                else:
                    info["status"] = "Location data stale"
                snapshot.append(info)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            unique_id = f"{DOMAIN}_{dev_id}"
            entity_id = ent_reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)
            if not entity_id:
                _LOGGER.warning(
                    "No entity registry entry for device '%s' (unique_id=%s); skipping any fallback.",
                    dev_name,
                    unique_id,
                )
                snapshot.append(info)
                continue

            state = self.hass.states.get(entity_id)
            if state:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                acc = state.attributes.get("gps_accuracy")
                if lat is not None and lon is not None:
                    info.update(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "accuracy": acc,
                            "last_seen": int(state.last_updated.timestamp()),
                            "status": "Using current state",
                        }
                    )
                    snapshot.append(info)
                    continue

            # Optional loud history fallback
            if self.allow_history_fallback:
                _LOGGER.warning(
                    "No live state for %s (entity_id=%s); attempting history fallback via Recorder.",
                    dev_name,
                    entity_id,
                )
                rec = get_recorder(self.hass)
                result = await rec.async_add_executor_job(
                    _sync_get_last_gps_from_history, self.hass, entity_id
                )
                if result:
                    info.update(result)
                    self.increment_stat("history_fallback_used")
                else:
                    _LOGGER.warning(
                        "No historical GPS data found for %s (entity_id=%s). "
                        "Entity may be excluded from Recorder.",
                        dev_name,
                        entity_id,
                    )

            snapshot.append(info)

        return snapshot

    # ---------------------------- Stats persistence -------------------------
    async def _async_load_stats(self) -> None:
        """Load statistics from cache."""
        try:
            from .Auth.token_cache import async_get_cached_value

            cached = await async_get_cached_value("integration_stats")
            if cached and isinstance(cached, dict):
                for key in self.stats.keys():
                    if key in cached:
                        self.stats[key] = cached[key]
                _LOGGER.debug("Loaded statistics from cache: %s", self.stats)
        except Exception as err:
            _LOGGER.debug("Failed to load statistics from cache: %s", err)

    async def _async_save_stats(self) -> None:
        """Persist statistics to cache."""
        try:
            from .Auth.token_cache import async_set_cached_value

            await async_set_cached_value("integration_stats", self.stats.copy())
        except Exception as err:
            _LOGGER.debug("Failed to save statistics to cache: %s", err)

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter and schedule async persistence."""
        if stat_name in self.stats:
            before = self.stats[stat_name]
            self.stats[stat_name] = before + 1
            _LOGGER.debug("Incremented %s from %s to %s", stat_name, before, self.stats[stat_name])
            self.hass.async_create_task(self._async_save_stats())
        else:
            _LOGGER.warning(
                "Tried to increment unknown stat '%s'; available=%s",
                stat_name,
                list(self.stats.keys()),
            )

    # ---------------------------- Public platform API -----------------------
    def get_device_location_data(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return current cached location dict for a device (or None)."""
        return self._device_location_data.get(device_id)

    def prime_device_location_cache(self, device_id: str, data: Dict[str, Any]) -> None:
        """Seed/update the location cache for a device with (lat/lon/accuracy)."""
        slot = self._device_location_data.get(device_id, {})
        slot.update({k: v for k, v in data.items() if k in ("latitude", "longitude", "accuracy")})
        # Do not set last_updated/last_seen here; this is only priming.
        self._device_location_data[device_id] = slot

    def seed_device_last_seen(self, device_id: str, ts_epoch: float) -> None:
        """Seed last_seen (epoch seconds) without overriding fresh data."""
        slot = self._device_location_data.setdefault(device_id, {})
        slot.setdefault("last_seen", float(ts_epoch))

    def get_device_last_seen(self, device_id: str) -> Optional[datetime]:
        """Return last_seen as timezone-aware datetime (UTC) if cached."""
        ts = self._device_location_data.get(device_id, {}).get("last_seen")
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            return None

    def get_device_display_name(self, device_id: str) -> Optional[str]:
        """Return the human-readable device name if known."""
        return self._device_names.get(device_id)

    def get_device_name_map(self) -> Dict[str, str]:
        """Return a shallow copy of the internal device-id -> name mapping."""
        return dict(self._device_names)

    # ---------------------------- Play sound helpers ------------------------
    def _api_push_ready(self) -> bool:
        """Best-effort check whether push/FCM is initialized (backward compatible).

        Optimistic default: if we cannot determine readiness explicitly,
        return True so the UI stays usable; the API call will enforce reality.
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
            _LOGGER.debug("Push readiness check exception: %s (defaulting optimistic True)", err)
            ready = True

        if ready is None:
            ready = True  # optimistic default

        if ready != self._push_ready_memo:
            _LOGGER.debug("Push readiness changed: %s", ready)
            self._push_ready_memo = ready

        return ready

    def _note_push_transport_problem(self, cooldown_s: int = 90) -> None:
        """Enter a temporary cooldown after a push transport failure to avoid spamming."""
        self._push_cooldown_until = time.monotonic() + cooldown_s
        self._push_ready_memo = False
        _LOGGER.debug("Entering push cooldown for %ss after transport failure", cooldown_s)

    def can_play_sound(self, device_id: str) -> bool:
        """Return True if 'Play Sound' should be enabled for the device.

        Optimistic strategy:
        - If API gives an explicit verdict -> use it.
        - If push readiness is explicitly False -> disable.
        - If capability is known -> use it.
        - Otherwise -> enable (optimistic); API call will guard and start cooldown on failure.
        """
        api_can = getattr(self.api, "can_play_sound", None)
        if callable(api_can):
            try:
                verdict = api_can(device_id)  # may be True/False/None
                _LOGGER.debug("can_play_sound(api) for %s -> %r", device_id, verdict)
                if verdict is not None:
                    return bool(verdict)
            except Exception as err:
                _LOGGER.debug("can_play_sound(api) failed for %s: %s; falling back", device_id, err)

        ready = self._api_push_ready()
        if ready is False:
            _LOGGER.debug("can_play_sound(%s) -> False (push not ready)", device_id)
            return False

        caps = self._device_caps.get(device_id)
        if caps and isinstance(caps.get("can_ring"), bool):
            res = bool(caps["can_ring"])
            _LOGGER.debug("can_play_sound(%s) -> %s (from capability can_ring)", device_id, res)
            return res

        is_known = device_id in self._device_names or device_id in self._device_location_data
        if is_known:
            _LOGGER.debug(
                "can_play_sound(%s) -> True (optimistic fallback; known device, push_ready=%s)",
                device_id,
                ready,
            )
            return True

        _LOGGER.debug("can_play_sound(%s) -> True (optimistic final fallback)", device_id)
        return True

    # ---------------------------- Public control API ------------------------
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
        """
        if tracked_devices is not None:
            self.tracked_devices = list(tracked_devices)

        if location_poll_interval is not None:
            try:
                self.location_poll_interval = max(1, int(location_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid location_poll_interval=%r", location_poll_interval)

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
                _LOGGER.warning("Ignoring invalid min_accuracy_threshold=%r", min_accuracy_threshold)

        if allow_history_fallback is not None:
            self.allow_history_fallback = bool(allow_history_fallback)

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

    # ---------------------------- Passthrough API ---------------------------
    async def async_locate_device(self, device_id: str) -> Dict[str, Any]:
        """Locate a device (executes blocking client code in executor)."""
        return await self.hass.async_add_executor_job(self.api.locate_device, device_id)

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device (executes blocking client code in executor).

        Guard with can_play_sound(); on failure, start a short cooldown to avoid repeated errors.
        """
        if not self.can_play_sound(device_id):
            _LOGGER.debug(
                "Suppressing play_sound call for %s: capability/push not ready", device_id
            )
            return False
        try:
            ok = await self.hass.async_add_executor_job(self.api.play_sound, device_id)
            if not ok:
                self._note_push_transport_problem()
            return bool(ok)
        except Exception as err:
            _LOGGER.debug("play_sound raised for %s: %s; entering cooldown", device_id, err)
            self._note_push_transport_problem()
            return False

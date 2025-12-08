"""Ephemeral identifier resolver for local BLE scans.

This module exposes a resolver that maps rotating Find My Device Network
EIDs to Home Assistant devices managed by the integration. The resolver
precalculates identifiers for the previous, current, and next rotation
windows so ``resolve_eid`` can respond to scans without performing
cryptographic work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid

_LOGGER = logging.getLogger(__name__)


class EIDMatch(NamedTuple):
    """Resolved mapping between an EID and a Home Assistant device.

    The ``device_id`` corresponds to the Home Assistant device registry
    identifier; ``canonical_id`` retains the integration-specific identifier
    used by the API payloads.
    """

    device_id: str
    config_entry_id: str
    canonical_id: str


@dataclass(slots=True)
class GoogleFindMyEIDResolver:
    """Resolver that precalculates rotating EIDs for known trackers.

    The resolver maintains an in-memory lookup table mapping current and
    recently rotated EIDs to Home Assistant device registry identifiers.
    It refreshes the table at the rotation cadence so that ``resolve_eid``
    can respond to BLE scan results without performing cryptographic work
    on the hot path. The shared instance lives at
    ``hass.data[DOMAIN][DATA_EID_RESOLVER]`` so other integrations can
    look up BLE scan results via ``resolve_eid`` or ``get_resolved_eid``.
    """

    hass: HomeAssistant
    _lookup: dict[bytes, EIDMatch] = field(init=False, default_factory=dict)
    _unsub_interval: CALLBACK_TYPE | None = field(init=False, default=None)
    _unsub_alignment: CALLBACK_TYPE | None = field(init=False, default=None)
    _refresh_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _pending_refresh: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        """Set up caches and schedule the first rotation-aligned refresh."""
        self._start_alignment_timer()

    def _start_alignment_timer(self) -> None:
        """Schedule the first refresh on the next rotation boundary."""

        now = int(time.time())
        seconds_until_boundary = ROTATION_PERIOD - (now % ROTATION_PERIOD)
        delay = seconds_until_boundary or ROTATION_PERIOD
        self._unsub_alignment = async_call_later(
            self.hass, delay, self._handle_alignment_refresh
        )

    def _start_interval(self) -> None:
        """Start the recurring refresh interval aligned to rotations."""

        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                self.hass,
                self._handle_refresh_interval,
                timedelta(seconds=ROTATION_PERIOD),
            )

    async def _handle_alignment_refresh(self, _now: datetime) -> None:
        """Run the initial rotation-aligned refresh then start the timer."""

        rotation_start = int(time.time())
        rotation_start -= rotation_start % ROTATION_PERIOD
        _LOGGER.debug("Boundary-aligned EID cache refresh at %s", rotation_start)
        await self.async_refresh()
        self._start_interval()

    async def _handle_refresh_interval(self, _now: datetime) -> None:
        """Refresh the cached EID lookup table on the rotation cadence."""

        await self.async_refresh()

    async def async_refresh(self) -> None:
        """Trigger a cache refresh immediately."""

        if self._refresh_lock.locked():
            self._pending_refresh = True
            return

        async with self._refresh_lock:
            await self._refresh_cache()
            while self._pending_refresh:
                self._pending_refresh = False
                await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        """Rebuild the EID lookup table for enabled, non-ignored devices."""

        identities: list[DeviceIdentity] = self._collect_device_secrets()
        lookup: dict[bytes, EIDMatch] = {}
        now = int(time.time())
        rotation_start = now - (now % ROTATION_PERIOD)
        windows = (
            rotation_start,
            max(0, rotation_start - ROTATION_PERIOD),
            rotation_start + ROTATION_PERIOD,
        )

        for identity in identities:
            if not identity.config_entry_id:
                continue
            for timestamp in windows:
                try:
                    eid = generate_eid(identity.identity_key, timestamp)
                except Exception as err:  # noqa: BLE001 - defensive guard
                    _LOGGER.debug(
                        "Failed to generate EID for %s at %s: %s",
                        identity.canonical_id,
                        timestamp,
                        err,
                    )
                    continue

                lookup[eid] = EIDMatch(
                    device_id=identity.registry_id,
                    config_entry_id=identity.config_entry_id,
                    canonical_id=identity.canonical_id,
                )

        self._lookup = lookup
        _LOGGER.debug(
            "Refreshed EID cache for %d devices (%d cached EIDs)",
            len(identities),
            len(lookup),
        )

    def _collect_device_secrets(self) -> list[DeviceIdentity]:
        """Retrieve active tracker identities from all loaded coordinators."""

        bucket = self.hass.data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return []

        # ``entries`` matches ``GoogleFindMyDomainData.entries`` (entry_id -> RuntimeData).
        entries = bucket.get("entries")
        if not isinstance(entries, dict):
            return []

        identities: list[DeviceIdentity] = []
        for runtime in entries.values():
            coordinator: GoogleFindMyCoordinator | None = None
            if isinstance(runtime, GoogleFindMyCoordinator):
                coordinator = runtime
            else:
                candidate = getattr(runtime, "coordinator", None)
                if isinstance(candidate, GoogleFindMyCoordinator):
                    coordinator = candidate

            if coordinator is None:
                continue

            identities.extend(coordinator.get_active_device_identities())

        return identities

    def resolve_eid(self, eid_bytes: bytes) -> EIDMatch | None:
        """Resolve a scanned EID to a Home Assistant device registry ID.

        Returns the matching :class:`EIDMatch` when the identifier was
        precomputed for a known tracker; otherwise returns ``None``. The
        ``device_id`` in the returned mapping corresponds to the Home
        Assistant Device Registry identifier, not an entity ID. The
        :meth:`get_resolved_eid` helper wraps this method for callers that
        prefer a ``device_id`` string or ``None``.

        EIDs are not logged to avoid leaking hardware identifiers in debug
        output.
        """

        match = self._lookup.get(eid_bytes)
        if match is not None:
            # Intentionally avoid logging raw EID bytes for privacy; only the
            # registry device identifier is included.
            _LOGGER.debug("Resolved EID to device %s", match.device_id)
        return match

    def get_resolved_eid(self, eid_bytes: bytes) -> str | None:
        """Backward compatible convenience wrapper for resolve_eid.

        Returns the Home Assistant device registry identifier for the EID when
        known; otherwise returns ``None``. This mirrors the legacy string-only
        contract while :meth:`resolve_eid` exposes the richer mapping.
        """

        match = self.resolve_eid(eid_bytes)
        return match.device_id if match is not None else None

    def stop(self) -> None:
        """Cancel background timers and clear cached state."""

        if self._unsub_alignment is not None:
            self._unsub_alignment()
            self._unsub_alignment = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        self._lookup.clear()

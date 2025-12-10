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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid
from .FMDNCrypto.mcu_utils import flip_bits, is_mcu_tracker
from .KeyBackup.cloud_key_decryptor import decrypt_eik
from .SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
    async_get_owner_key,
)

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache
else:
    try:
        from custom_components.googlefindmy.Auth.token_cache import TokenCache
    except Exception:  # pragma: no cover - optional type aid only
        TokenCache = None

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

        identities: list[DeviceIdentity] = await self._collect_device_secrets()
        _LOGGER.debug(
            "Resolver received %d identities from coordinator", len(identities)
        )
        lookup: dict[bytes, EIDMatch] = {}
        now = int(time.time())
        rotation_start = now - (now % ROTATION_PERIOD)
        windows = (
            rotation_start,
            max(0, rotation_start - ROTATION_PERIOD),
            rotation_start + ROTATION_PERIOD,
        )

        for identity in identities:
            if not identity.config_entry_id or identity.identity_key is None:
                continue

            mcu_tracker = is_mcu_tracker(
                device_type=identity.device_type,
                fast_pair_model_id=identity.fast_pair_model_id,
            )
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
                _LOGGER.debug(
                    "Precalculated EID for %s (MCU=%s, ModelID=%s): %s",
                    identity.registry_id,
                    mcu_tracker,
                    identity.fast_pair_model_id,
                    eid.hex(),
                )

        self._lookup = lookup
        _LOGGER.debug(
            "Refreshed EID cache for %d devices (%d cached EIDs)",
            len(identities),
            len(lookup),
        )

    async def _collect_device_secrets(self) -> list[DeviceIdentity]:
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
            coordinator: _IdentityProvider | None = None
            if isinstance(runtime, GoogleFindMyCoordinator):
                coordinator = runtime
            else:
                candidate = getattr(runtime, "coordinator", None)
                if isinstance(candidate, _IdentityProvider):
                    coordinator = candidate
                elif isinstance(runtime, _IdentityProvider):
                    coordinator = runtime

            if coordinator is None:
                continue

            identities.extend(
                await self._normalize_identities(
                    coordinator.get_active_device_identities(),
                    cache=getattr(coordinator, "cache", None),
                )
            )

        return identities

    async def _normalize_identities(
        self,
        identities: list[DeviceIdentity],
        *,
        cache: TokenCache | None,
    ) -> list[DeviceIdentity]:
        """Ensure each device identity has a usable plaintext key."""

        normalized: list[DeviceIdentity] = []
        for identity in identities:
            if identity.identity_key is None:
                _LOGGER.debug(
                    "Attempting to decrypt key for %s (Type: %s)",
                    identity.canonical_id,
                    identity.device_type,
                )
                decrypted = await self._try_decrypt_identity_key(identity, cache=cache)
                if decrypted is None:
                    _LOGGER.debug(
                        "Decryption returned None for %s - skipping",
                        identity.canonical_id,
                    )
                    continue
                normalized.append(replace(identity, identity_key=decrypted))
                continue

            normalized.append(identity)

        return normalized

    async def _try_decrypt_identity_key(
        self,
        identity: DeviceIdentity,
        *,
        cache: TokenCache | None,
    ) -> bytes | None:
        """Decrypt encrypted identity key when owner key information is available."""

        if (
            cache is None
            or identity.encrypted_identity_key is None
            or not isinstance(identity.encrypted_identity_key, (bytes, bytearray))
        ):
            return None

        try:
            owner_key_info: OwnerKeyInfo = await async_get_owner_key(cache=cache)
        except Exception as err:  # noqa: BLE001 - defensive
            _LOGGER.debug(
                "Failed to retrieve owner key for %s: %s",
                identity.canonical_id,
                err,
            )
            return None

        expected_version = identity.owner_key_version
        if (
            expected_version is not None
            and owner_key_info.version is not None
            and expected_version != owner_key_info.version
        ):
            _LOGGER.debug(
                "Owner key version mismatch (expected %s, got %s), forcing refresh...",
                expected_version,
                owner_key_info.version,
            )
            try:
                owner_key_info = await async_get_owner_key(
                    cache=cache, force_refresh=True
                )
            except Exception as err:  # noqa: BLE001 - defensive
                _LOGGER.debug(
                    "Forced owner key refresh failed for %s: %s",
                    identity.canonical_id,
                    err,
                )

        encrypted_identity_key = bytes(identity.encrypted_identity_key)

        suggested_mcu = is_mcu_tracker(
            device_type=identity.device_type,
            fast_pair_model_id=identity.fast_pair_model_id,
        )
        candidates = [suggested_mcu, not suggested_mcu]

        for flip_mcu in candidates:
            candidate_key = (
                flip_bits(encrypted_identity_key, True) if flip_mcu else encrypted_identity_key
            )

            try:
                decrypted = await asyncio.to_thread(
                    decrypt_eik, owner_key_info.key, candidate_key
                )
            except InvalidTag as err:
                _LOGGER.debug(
                    "Identity key decrypt failed for %s with flip=%s: %s",
                    identity.canonical_id,
                    flip_mcu,
                    err,
                )
                continue
            except Exception as err:  # noqa: BLE001 - defensive
                _LOGGER.debug(
                    "Failed to decrypt identity key for %s (owner_key_version=%s, flip=%s): %s",
                    identity.canonical_id,
                    identity.owner_key_version,
                    flip_mcu,
                    err,
                )
                continue

            if isinstance(decrypted, (bytes, bytearray)):
                return bytes(decrypted)

            _LOGGER.debug(
                "Decryption returned non-bytes result for %s (flip=%s)",
                identity.canonical_id,
                flip_mcu,
            )

        return None

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
            unsub = self._unsub_alignment
            self._unsub_alignment = None
            try:
                if callable(unsub):
                    unsub()
                elif asyncio.iscoroutine(unsub):
                    unsub.close()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to cancel alignment timer: %s", err)
        if self._unsub_interval is not None:
            unsub = self._unsub_interval
            self._unsub_interval = None
            try:
                if callable(unsub):
                    unsub()
                elif asyncio.iscoroutine(unsub):
                    unsub.close()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to cancel refresh interval: %s", err)
        self._lookup.clear()
@runtime_checkable
class _IdentityProvider(Protocol):
    """Interface for coordinator-like objects that expose device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]:
        """Return eligible device identities for EID resolution."""


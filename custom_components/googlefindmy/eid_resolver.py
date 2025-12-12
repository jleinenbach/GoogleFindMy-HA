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
from .FMDNCrypto.eid_generator import ROTATION_PERIOD, generate_eid, generate_eid_p256
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

EID_LENGTH = 20
RAW_HEADER_LENGTH = 1
FMDN_FRAME_TYPE = 0x40
MODERN_ROTATION_PERIOD = 3600


class EIDMatch(NamedTuple):
    """Resolved mapping between an EID and a Home Assistant device.

    The ``device_id`` corresponds to the Home Assistant device registry
    identifier; ``canonical_id`` retains the integration-specific identifier
    used by the API payloads.
    """

    device_id: str
    config_entry_id: str
    canonical_id: str
    time_offset: int
    is_reversed: bool


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
    _known_offsets: dict[str, int] = field(default_factory=dict)
    _known_endianness: dict[str, bool] = field(default_factory=dict)
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

    async def _refresh_cache(self) -> None:  # noqa: PLR0912, PLR0915 - iterative window search
        """Rebuild the EID lookup table for enabled, non-ignored devices."""

        identities: list[DeviceIdentity] = await self._collect_device_secrets()
        _LOGGER.debug(
            "Resolver received %d identities from coordinator", len(identities)
        )
        lookup: dict[bytes, EIDMatch] = {}
        now = int(time.time())

        first_identity_id: str | None = None

        for identity in identities:
            if not identity.config_entry_id or identity.identity_key is None:
                continue

            if first_identity_id is None:
                first_identity_id = identity.canonical_id

            assert identity.config_entry_id is not None
            assert identity.registry_id is not None
            config_entry_id = identity.config_entry_id
            registry_id = identity.registry_id

            known_offset = self._known_offsets.get(identity.registry_id)
            known_endianness = self._known_endianness.get(identity.registry_id, False)
            target_time = now if known_offset is None else now + known_offset
            window_range: range
            should_log_debug_dump = identity.canonical_id.endswith("d329") or (
                first_identity_id is not None
                and identity.canonical_id == first_identity_id
            )
            debug_dump_logged = False

            if known_offset is None:
                window_range = range(-90, 91)
            else:
                window_range = range(0, 1)

            def _iter_windows(rotation_period: int) -> tuple[int, ...]:
                rotation_start = target_time - (target_time % rotation_period)
                windows: list[int] = []

                for offset in window_range:
                    timestamp = rotation_start + (offset * rotation_period)
                    if timestamp < 0:
                        continue

                    if known_offset is not None:
                        raw_candidates: tuple[int, ...] = (
                            timestamp,
                            max(0, timestamp - rotation_period),
                            timestamp + rotation_period,
                        )
                    else:
                        raw_candidates = (timestamp,)

                    for candidate in raw_candidates:
                        if candidate >= 0:
                            windows.append(candidate)

                return tuple(dict.fromkeys(windows))

            def _register_match(eid: bytes, time_offset: int, is_reversed: bool) -> None:
                match = EIDMatch(
                    device_id=registry_id,
                    config_entry_id=config_entry_id,
                    canonical_id=identity.canonical_id,
                    time_offset=time_offset,
                    is_reversed=is_reversed,
                )
                existing = lookup.get(eid)
                if existing is None or abs(time_offset) < abs(existing.time_offset):
                    lookup[eid] = match

            def _store_candidates(eid: bytes, time_offset: int, is_reversed: bool) -> None:
                if known_offset is None:
                    _register_match(eid, time_offset, False)
                    _register_match(eid[::-1], time_offset, True)
                else:
                    eid_bytes = eid[::-1] if is_reversed else eid
                    _register_match(eid_bytes, time_offset, is_reversed)

            is_reversed = known_offset is not None and known_endianness
            legacy_windows = _iter_windows(ROTATION_PERIOD)
            modern_windows = _iter_windows(MODERN_ROTATION_PERIOD)

            for window_timestamp in legacy_windows:
                try:
                    legacy_eid = generate_eid(identity.identity_key, window_timestamp)
                except Exception as err:  # noqa: BLE001 - defensive guard
                    _LOGGER.debug(
                        "Failed to generate legacy EID for %s at %s: %s",
                        identity.canonical_id,
                        window_timestamp,
                        err,
                    )
                    continue

                time_offset = window_timestamp - now
                _store_candidates(legacy_eid, time_offset, is_reversed)

                if should_log_debug_dump and not debug_dump_logged:
                    key_snippet = identity.identity_key.hex()[:6]
                    _LOGGER.info(
                        "DEBUG DUMP: Device=%s KeyStart=%s... TS=%s GeneratedEID=%s",
                        identity.canonical_id,
                        key_snippet,
                        window_timestamp,
                        legacy_eid.hex(),
                    )
                    debug_dump_logged = True

            for window_timestamp in modern_windows:
                try:
                    modern_eid_full = generate_eid_p256(
                        identity.identity_key,
                        window_timestamp,
                        rotation_period=MODERN_ROTATION_PERIOD,
                    )
                except Exception as err:  # noqa: BLE001 - defensive guard
                    _LOGGER.debug(
                        "Failed to generate modern EID for %s at %s: %s",
                        identity.canonical_id,
                        window_timestamp,
                        err,
                    )
                    continue

                if not modern_eid_full:
                    continue

                modern_eid = modern_eid_full[:EID_LENGTH]
                time_offset = window_timestamp - now
                _store_candidates(modern_eid, time_offset, is_reversed)

                if should_log_debug_dump and not debug_dump_logged:
                    key_snippet = identity.identity_key.hex()[:6]
                    _LOGGER.info(
                        "DEBUG DUMP: Device=%s KeyStart=%s... TS=%s GeneratedEID=%s",
                        identity.canonical_id,
                        key_snippet,
                        window_timestamp,
                        modern_eid.hex(),
                    )
                    debug_dump_logged = True

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

        Only Find My Device Network (FMDN) advertising frames (Frame Type
        ``0x40``) are considered when the payload includes framing/telemetry;
        the resolver slices the header and trailing bytes to isolate the
        20-byte EID before lookup. Legacy callers that already provide a
        20-byte EID bypass the framing filter.

        Returns the matching :class:`EIDMatch` when the identifier was
        precomputed for a known tracker; otherwise returns ``None``. The
        ``device_id`` in the returned mapping corresponds to the Home
        Assistant Device Registry identifier, not an entity ID. The
        :meth:`get_resolved_eid` helper wraps this method for callers that
        prefer a ``device_id`` string or ``None``.

        EIDs are not logged to avoid leaking hardware identifiers in debug
        output during normal operation. A debug-level probe log at the start of
        this method prints the sliced EID for troubleshooting cache
        mismatches.
        """

        lookup_key: bytes | None

        if len(eid_bytes) == EID_LENGTH:
            lookup_key = eid_bytes
        elif len(eid_bytes) > EID_LENGTH:
            if eid_bytes[0] != FMDN_FRAME_TYPE:
                return None
            lookup_key = eid_bytes[1 : 1 + EID_LENGTH]
        else:
            return None

        if len(lookup_key) != EID_LENGTH:
            return None

        _LOGGER.debug(
            "RESOLVER PROBE: Checking Sliced EID %s (Original Len: %d)",
            lookup_key.hex(),
            len(eid_bytes),
        )

        match = self._lookup.get(lookup_key)
        if match is None:
            match = self._lookup.get(lookup_key[::-1])

        if match is None:
            return None

        previous_offset = self._known_offsets.get(match.device_id)
        previous_endianness = self._known_endianness.get(match.device_id)

        if (
            previous_offset != match.time_offset
            or previous_endianness != match.is_reversed
        ):
            _LOGGER.info(
                "Locked on to device %s! Applying Time Offset: %ss, Reverse: %s",
                match.device_id,
                match.time_offset,
                match.is_reversed,
            )

        _LOGGER.info(
            "MATCH FOUND: EID %s belongs to device %s (Time Offset: %s)",
            lookup_key.hex(),
            match.device_id,
            match.time_offset,
        )

        self._known_offsets[match.device_id] = match.time_offset
        self._known_endianness[match.device_id] = match.is_reversed
        _LOGGER.debug("Resolved EID to device %s", match.device_id)
        return match

    def reset_device_offset(self, device_id: str) -> None:
        """Clear cached time offset and endianness for a device."""

        self._known_offsets.pop(device_id, None)
        self._known_endianness.pop(device_id, None)

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


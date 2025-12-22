# custom_components/googlefindmy/eid_resolver.py
"""Ephemeral identifier resolver for local BLE scans.

The resolver precomputes ephemeral identifiers (EIDs) for known trackers using
the FHNA/FMDN Table 10 PRF helpers and maps BLE scan payloads to Home Assistant
device registry identifiers. Heavy cryptography and key-unwrapping happen
outside the hot path; ``resolve_eid`` only performs lookups.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    ROTATION_PERIOD,
    EidVariant,
    generate_eid_variant,
)
from .FMDNCrypto.mcu_utils import flip_bits, is_mcu_tracker
from .KeyBackup.cloud_key_decryptor import decrypt_eik
from .KeyBackup.shared_key_retrieval import async_get_shared_key
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

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_eid_locks"

FMDN_FRAME_TYPE = 0x40
MODERN_FRAME_TYPE = 0x41
RAW_HEADER_LENGTH = 1
SERVICE_DATA_OFFSET = 8
AESGCM_NONCE_LENGTH = 12

# Strategy Configuration
# ENABLE_ABSOLUTE_UNIX_BASIS: If True, scans for EIDs based on absolute Unix time (Deep Scan).
# Proven unreliable for standard trackers (Motorola/Pebblebee), so disabled by default.
ENABLE_ABSOLUTE_UNIX_BASIS: bool = False

# MIN_UNIX_WINDOW_SIZE: Safety net for absolute scans. 128 * 1024s ~= 36h.
MIN_UNIX_WINDOW_SIZE: int = 128

# MIN_RELATIVE_WINDOW_SIZE: Safety net for relative scans (pair_date).
# 5 * 1024s ~= 85 mins. Essential to absorb 1h Daylight Saving Time (DST) shifts.
MIN_RELATIVE_WINDOW_SIZE: int = 5

EID_LENGTH = LEGACY_EID_LENGTH
LOCK_TTL_SECONDS = 7 * 24 * 60 * 60
LOCK_CONFIRMATION_TTL_SECONDS = 90 * 60
LOCK_MISS_THRESHOLD = 3
TRUNCATED_FRAME_LOG_WINDOW_SECONDS = 60


class EIDMatch(NamedTuple):
    """Resolved mapping between an EID and a Home Assistant device."""

    device_id: str
    config_entry_id: str
    canonical_id: str
    time_offset: int
    is_reversed: bool


@dataclass(slots=True, frozen=True)
class DecryptionResult:
    """Decrypted identity key with contextual metadata."""

    key: bytes
    metadata: dict[str, Any]


@runtime_checkable
class _IdentityProvider(Protocol):
    """Protocol implemented by coordinators that can provide device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]: ...


@dataclass(slots=True, frozen=True)
class WorkItem:
    """Normalized device identity with optional lock context."""

    registry_id: str
    config_entry_id: str
    canonical_id: str
    key_bytes: bytes
    identity: DeviceIdentity
    lock: EIDGenerationLock | None
    locked_variant: EidVariant | None
    rotation_ts: int | None
    basis_hint: str | None


@dataclass(slots=True, frozen=True)
class WindowCandidate:
    """Single timestamp candidate for EID generation."""

    timestamp: int
    semantic_offset: int
    time_basis: str
    candidate_value: int


@dataclass(slots=True, frozen=True)
class WindowSpec:
    """Time-basis specific window candidates derived from a work item."""

    time_basis: str
    candidate_value: int
    windows: tuple[WindowCandidate, ...]


@dataclass(slots=True, frozen=True)
class VariantSpec:
    """Variant generation request for a given window."""

    key_bytes: bytes
    variant: EidVariant
    window: WindowCandidate
    include_reverse: bool = True


@dataclass(slots=True, frozen=True)
class GeneratedEid:
    """Result of generating a single EID variant."""

    eid_bytes: bytes
    is_reversed: bool
    variant: EidVariant
    window: WindowCandidate


@dataclass(slots=True, frozen=True)
class RotationParams:
    """Collection of rotation-related constants for generation."""

    rotation_period: int
    min_unix_window: int
    min_relative_window: int
    max_window: int


@dataclass(slots=True)
class CacheBuilder:
    """Immutable-safe cache builder enforcing lookup/metadata invariants."""

    lookup: dict[bytes, EIDMatch] = field(default_factory=dict)
    metadata: dict[bytes, dict[str, Any]] = field(default_factory=dict)

    def register_eid(
        self,
        eid_bytes: bytes,
        *,
        match: EIDMatch,
        variant: EidVariant,
        window: WindowCandidate,
        advertisement_reversed: bool,
    ) -> None:
        """Register an EID and metadata, resolving collisions deterministically."""

        existing = self.lookup.get(eid_bytes)
        existing_metadata = self.metadata.get(eid_bytes)
        existing_bases: set[str] | None = None
        if existing_metadata is not None:
            existing_bases = set(existing_metadata.get("timestamp_bases") or ())
            if (basis := existing_metadata.get("timestamp_basis")) and isinstance(
                basis, str
            ):
                existing_bases.add(basis)
            existing_bases.add(window.time_basis)

        if existing is not None and abs(existing.time_offset) <= abs(
            match.time_offset
        ):
            if existing_metadata is not None and existing_bases is not None:
                existing_metadata["timestamp_bases"] = existing_bases
            return

        timestamp_bases = existing_bases or {window.time_basis}
        self.lookup[eid_bytes] = match
        self.metadata[eid_bytes] = {
            "variant": variant.value,
            "rotation_timestamp": window.timestamp,
            "time_offset": match.time_offset,
            "timestamp_basis": window.time_basis,
            "timestamp_bases": timestamp_bases,
            "advertisement_reversed": advertisement_reversed,
        }
        if __debug__:
            assert set(self.lookup.keys()).issuperset(
                self.metadata.keys()
            ), "metadata keys diverged after registration"

    def finalize(self) -> tuple[dict[bytes, EIDMatch], dict[bytes, dict[str, Any]]]:
        """Return the finalized lookup tables after invariant validation."""

        lookup_keys = set(self.lookup.keys())
        metadata_keys = set(self.metadata.keys())
        if lookup_keys != metadata_keys:
            missing_metadata = lookup_keys - metadata_keys
            missing_lookup = metadata_keys - lookup_keys
            _LOGGER.warning(
                "EID cache invariant violation: metadata_missing=%s lookup_missing=%s",
                missing_metadata,
                missing_lookup,
            )
            for key in missing_metadata:
                self.metadata[key] = {}
            for key in missing_lookup:
                self.lookup.pop(key, None)
        return self.lookup, self.metadata


def iter_rotation_windows(
    target_time: int,
    *,
    rotation_period: int,
    window_range: Iterable[int],
    include_neighbors: bool,
) -> tuple[int, ...]:
    """Return rotation-aligned timestamps for cache population."""

    rotation_start = target_time - (target_time % rotation_period)
    windows: list[int] = []

    for offset in window_range:
        timestamp = rotation_start + (offset * rotation_period)
        if include_neighbors:
            previous_window = timestamp - rotation_period
            next_window = timestamp + rotation_period
            if timestamp >= 0:
                windows.append(timestamp)
            if previous_window >= 0:
                windows.append(previous_window)
            if next_window >= 0:
                windows.append(next_window)
        else:
            windows.append(timestamp)

    return tuple(dict.fromkeys(windows))


def _normalize_optional_string(value: object) -> str | None:
    """Return a trimmed string or ``None`` when empty or missing."""

    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _normalize_encrypted_blob(value: object) -> bytes | None:
    """Normalize encrypted payloads encoded as bytes or hex strings."""

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class EIDGenerationLock:
    """Persisted per-device generation profile."""

    device_id: str
    canonical_id: str
    variant: str
    advertisement_reversed: bool
    eid_length: int
    rotation_timestamp: int | None = None
    frame_type: int | None = None
    time_basis: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        """Serialize lock for storage."""

        return {
            "device_id": self.device_id,
            "canonical_id": self.canonical_id,
            "variant": self.variant,
            "advertisement_reversed": self.advertisement_reversed,
            "eid_length": self.eid_length,
            "rotation_timestamp": self.rotation_timestamp,
            "frame_type": self.frame_type,
            "time_basis": self.time_basis,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EIDGenerationLock:
        """Deserialize a stored lock."""

        variant = payload.get("variant") or ""
        advertisement_reversed = bool(payload.get("advertisement_reversed") or False)
        rotation_timestamp = payload.get("rotation_timestamp")
        rotation_ts = (
            int(rotation_timestamp)
            if isinstance(rotation_timestamp, int) and not isinstance(rotation_timestamp, bool)
            else None
        )
        if not variant:
            scalar_endianness = str(payload.get("scalar_endianness") or "big")
            length = int(payload["eid_length"])
            if length == LEGACY_EID_LENGTH and scalar_endianness == "little":
                variant = EidVariant.MODERN_P256_X20_TRUNC_LE.value
            elif length == LEGACY_EID_LENGTH:
                variant = EidVariant.LEGACY_SECP160R1_X20_BE.value
            elif scalar_endianness == "little":
                variant = EidVariant.MODERN_P256_X32_LE_SCALAR.value
            else:
                variant = EidVariant.MODERN_P256_X32_BE.value

        return cls(
            device_id=str(payload["device_id"]),
            canonical_id=str(payload.get("canonical_id") or ""),
            variant=variant,
            advertisement_reversed=advertisement_reversed,
            eid_length=int(payload["eid_length"]),
            rotation_timestamp=rotation_ts,
            frame_type=payload.get("frame_type"),
            time_basis=str(payload.get("time_basis") or "") or None,
            created_at=int(payload.get("created_at") or int(time.time())),
        )


def _normalize_counter_candidate(candidate_value: object, *, basis: str) -> int | None:
    """Return a sane u32 counter candidate or ``None`` when unusable."""

    if not isinstance(candidate_value, int) or candidate_value < 0:
        return None

    max_millis = FHNA_COUNTER_MASK
    candidate_seconds = candidate_value // 1000
    if candidate_value > max_millis and candidate_value % 1000 == 0 and candidate_seconds > 0:
        _LOGGER.debug(
            "Converted %s basis %s from milliseconds to seconds: %s",
            candidate_value,
            basis,
            candidate_seconds,
        )
        return candidate_seconds & FHNA_COUNTER_MASK

    return candidate_value


@dataclass(slots=True)
class GoogleFindMyEIDResolver:
    """Resolver that precalculates rotating EIDs for known trackers."""

    hass: HomeAssistant
    _lookup: dict[bytes, EIDMatch] = field(init=False, default_factory=dict)
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(
        init=False, default_factory=dict
    )
    _known_offsets: dict[str, int] = field(init=False, default_factory=dict)
    _known_advertisement_reversed: dict[str, bool] = field(
        init=False, default_factory=dict
    )
    _known_timebases: dict[str, str] = field(init=False, default_factory=dict)
    _persisted_locks: dict[str, EIDGenerationLock] = field(
        init=False, default_factory=dict
    )
    _decryption_status: dict[str, str] = field(init=False, default_factory=dict)
    _last_lock_confirmation: dict[str, int] = field(init=False, default_factory=dict)
    _provisioning_warn_at: dict[str, float] = field(init=False, default_factory=dict)
    _locks: dict[str, EIDGenerationLock] = field(init=False, default_factory=dict)
    _lock_miss_counts: dict[str, int] = field(init=False, default_factory=dict)
    _truncated_frame_log_at: dict[tuple[int, int], float] = field(
        init=False, default_factory=dict
    )
    _store: Store[list[dict[str, Any]] | None] = field(init=False)
    _unsub_interval: CALLBACK_TYPE | None = field(init=False, default=None)
    _unsub_alignment: CALLBACK_TYPE | None = field(init=False, default=None)
    _refresh_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _pending_refresh: bool = field(init=False, default=False)
    _load_task: asyncio.Task[None] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Set up caches and schedule the first rotation-aligned refresh."""

        self._ensure_cache_defaults()
        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        self._load_task = self.hass.async_create_task(self._async_load_locks())
        self._start_alignment_timer()

    def _ensure_cache_defaults(self) -> None:
        """Initialize optional caches when stubs bypass __init__."""

        if not hasattr(self, "_known_offsets"):
            self._known_offsets = {}
        if not hasattr(self, "_known_advertisement_reversed"):
            self._known_advertisement_reversed = {}
        if not hasattr(self, "_known_timebases"):
            self._known_timebases = {}
        if not hasattr(self, "_persisted_locks"):
            self._persisted_locks = {}
        if not hasattr(self, "_decryption_status"):
            self._decryption_status = {}
        if not hasattr(self, "_last_lock_confirmation"):
            self._last_lock_confirmation = {}
        if not hasattr(self, "_provisioning_warn_at"):
            self._provisioning_warn_at = {}
        if not hasattr(self, "_locks"):
            self._locks = {}
        if not hasattr(self, "_lock_miss_counts"):
            self._lock_miss_counts = {}
        if not hasattr(self, "_truncated_frame_log_at"):
            self._truncated_frame_log_at = {}

    def _clear_lock_state(self, device_id: str) -> bool:
        """Remove all cached state associated with a device lock."""

        removed = False
        for attr in (
            "_locks",
            "_persisted_locks",
            "_lock_miss_counts",
            "_known_offsets",
            "_known_advertisement_reversed",
            "_known_timebases",
            "_last_lock_confirmation",
        ):
            mapping = getattr(self, attr, None)
            if isinstance(mapping, dict) and device_id in mapping:
                mapping.pop(device_id, None)
                removed = True
        return removed

    def _update_lock_health(self, device_id: str, *, now: int) -> bool:
        """Track consecutive unconfirmed refresh cycles for locked devices."""

        lock = self._locks.get(device_id)
        if lock is None:
            return False

        last_confirmation = self._last_lock_confirmation.get(device_id)
        confirmed_recently = (
            isinstance(last_confirmation, int)
            and not isinstance(last_confirmation, bool)
            and (now - last_confirmation) < ROTATION_PERIOD
        )

        if confirmed_recently:
            if self._lock_miss_counts.get(device_id):
                self._lock_miss_counts[device_id] = 0
            return False

        self._lock_miss_counts[device_id] = self._lock_miss_counts.get(device_id, 0) + 1

        miss_count = self._lock_miss_counts[device_id]
        if miss_count < LOCK_MISS_THRESHOLD:
            return False

        if self._clear_lock_state(device_id):
            _LOGGER.warning(
                "Lock self-heal: clearing lock for %s after %d unconfirmed refresh cycles "
                "(last_confirmation=%s)",
                device_id,
                miss_count,
                last_confirmation,
            )
            self._schedule_lock_save()
            return True

        return False

    def _start_alignment_timer(self) -> None:
        """Schedule the first refresh on the next rotation boundary."""
        now_unix = int(time.time())
        seconds_until_boundary = ROTATION_PERIOD - (now_unix % ROTATION_PERIOD)
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

    async def _async_load_locks(self) -> None:
        """Load persisted locks from storage."""

        payload = await self._store.async_load()
        if not payload:
            return

        for entry in payload:
            try:
                lock = EIDGenerationLock.from_dict(entry)
            except Exception:  # pragma: no cover - defensive
                continue
            self._locks[lock.device_id] = lock

    async def _async_save_locks(self) -> None:
        """Persist locks to storage."""

        await self._store.async_save([lock.to_dict() for lock in self._locks.values()])

    def _schedule_lock_save(self) -> None:
        """Schedule persistence of EID locks."""

        try:
            task_name = "googlefindmy_eid_resolver_save"
            create_task = getattr(self.hass, "async_create_background_task", None) or getattr(
                self.hass, "async_create_task"
            )
            if create_task is None:
                raise AttributeError("hass is missing async_create_task helper")
            lock_save = self._async_save_locks()
            try:
                scheduled = create_task(lock_save, name=task_name)
            except TypeError:
                scheduled = create_task(lock_save)
            if scheduled is None:
                asyncio.create_task(lock_save)
                _LOGGER.warning("EID lock save was not scheduled (task helper returned None)")
            elif asyncio.iscoroutine(scheduled):
                asyncio.create_task(scheduled)
            elif not isinstance(scheduled, asyncio.Task):
                try:
                    lock_save.close()
                except Exception:  # pragma: no cover - defensive close
                    pass
                _LOGGER.warning(
                    "EID lock save task helper returned non-awaitable %s; coroutine closed",
                    type(scheduled).__name__,
                )
        except Exception as err:  # pragma: no cover - defensive log
            _LOGGER.error("Failed to schedule EID lock persistence: %s", err)

    def _purge_stale_locks(self, *, now: int) -> None:
        """Drop expired generation locks to keep cache fresh."""

        expired_by_confirmation: dict[str, int | None] = {}
        expired_by_created: list[str] = []
        for device_id, lock in list(self._locks.items()):
            last_confirmation = self._last_lock_confirmation.get(device_id)
            confirmation_age = (
                now - last_confirmation
                if isinstance(last_confirmation, int)
                and not isinstance(last_confirmation, bool)
                else None
            )
            if confirmation_age is None:
                if now - lock.created_at > LOCK_CONFIRMATION_TTL_SECONDS:
                    expired_by_confirmation[device_id] = None
                    continue
            elif confirmation_age > LOCK_CONFIRMATION_TTL_SECONDS:
                expired_by_confirmation[device_id] = confirmation_age
                continue

            if now - lock.created_at > LOCK_TTL_SECONDS:
                expired_by_created.append(device_id)

        removed: list[str] = []
        for device_id, confirmation_age in expired_by_confirmation.items():
            if self._clear_lock_state(device_id):
                removed.append(device_id)
                _LOGGER.debug(
                    "Purged stale EID lock for %s after %s seconds without confirmation",
                    device_id,
                    confirmation_age if confirmation_age is not None else "unknown",
                )
        for device_id in expired_by_created:
            if device_id in expired_by_confirmation:
                continue
            if self._clear_lock_state(device_id):
                removed.append(device_id)
        if removed:
            self._schedule_lock_save()
            _LOGGER.debug("Purged %d stale EID locks: %s", len(removed), removed)

    async def async_refresh(self) -> None:
        """Trigger a cache refresh immediately."""
        if self._refresh_lock.locked():
            self._pending_refresh = True
            return

        async with self._refresh_lock:
            self._pending_refresh = False
            await self._refresh_cache()
            while self._pending_refresh:
                self._pending_refresh = False
                await self._refresh_cache()

    def reset_device_offset(self, registry_id: str) -> None:
        """Reset resolver state for a single device to force rediscovery."""

        self._ensure_cache_defaults()
        changed = self._clear_lock_state(registry_id)

        if not changed:
            return

        try:
            self._schedule_lock_save()
        except Exception as err:  # pragma: no cover - defensive log
            _LOGGER.debug(
                "Failed to schedule lock persistence after reset for %s: %s",
                registry_id,
                err,
            )

        refresh = getattr(self, "async_refresh", None)
        create_task = getattr(self.hass, "async_create_task", None)
        if callable(refresh) and callable(create_task):
            try:
                refresh_coro = refresh()
                scheduled = create_task(
                    refresh_coro, name="googlefindmy_eid_refresh_after_reset"
                )
                if scheduled is None:
                    refresh_coro.close()
                elif asyncio.iscoroutine(scheduled):
                    asyncio.create_task(scheduled)
            except Exception as err:  # pragma: no cover - defensive log
                _LOGGER.debug(
                    "Failed to schedule refresh after reset for %s: %s",
                    registry_id,
                    err,
                )

    def _build_rotation_params(self) -> RotationParams:
        """Return the rotation constants for deterministic refresh calculations."""

        return RotationParams(
            rotation_period=ROTATION_PERIOD,
            min_unix_window=MIN_UNIX_WINDOW_SIZE,
            min_relative_window=MIN_RELATIVE_WINDOW_SIZE,
            max_window=math.ceil((24 * 60 * 60) / ROTATION_PERIOD),
        )

    def _prepare_work_item(
        self,
        identity: DeviceIdentity,
        *,
        now_unix: int,
    ) -> WorkItem | None:
        """Normalize an identity and lock into a work item."""

        basis_hint = self._known_timebases.get(identity.registry_id)
        if (
            not identity.config_entry_id
            or identity.identity_key is None
            or identity.registry_id is None
        ):
            return None

        clean_canonical_id = identity.canonical_id
        if ":" in clean_canonical_id:
            clean_canonical_id = clean_canonical_id.split(":")[-1]

        key_bytes = bytes(identity.identity_key)
        lock = self._locks.get(identity.registry_id)
        locked_variant: EidVariant | None = None
        rotation_ts: int | None = None

        if lock is not None:
            if self._update_lock_health(identity.registry_id, now=now_unix):
                lock = self._locks.get(identity.registry_id)
                basis_hint = self._known_timebases.get(identity.registry_id)

        if lock is not None:
            raw_rotation_ts = lock.rotation_timestamp
            rotation_ts = (
                raw_rotation_ts
                if isinstance(raw_rotation_ts, int)
                and not isinstance(raw_rotation_ts, bool)
                else None
            )
            lock_time_basis = _normalize_optional_string(lock.time_basis)
            if lock_time_basis:
                basis_hint = lock_time_basis
                self._known_timebases[identity.registry_id] = lock_time_basis

            is_legacy = rotation_ts is None
            try:
                locked_variant = EidVariant(lock.variant)
            except ValueError:
                locked_variant = EidVariant.MODERN_P256_X32_BE

            if is_legacy:
                valid_hint = lock_time_basis if lock_time_basis in {"unix", "pair_date", "secrets_creation_date"} else None
                _LOGGER.warning(
                    "Discarding invalid/legacy lock for %s (legacy=%s). Force re-discovery.",
                    identity.registry_id,
                    is_legacy,
                )
                self._locks.pop(identity.registry_id, None)
                self._persisted_locks.pop(identity.registry_id, None)
                if valid_hint:
                    basis_hint = valid_hint
                    self._known_timebases[identity.registry_id] = valid_hint
                else:
                    self._known_timebases.pop(identity.registry_id, None)
                lock = None
            elif lock.canonical_id != clean_canonical_id:
                _LOGGER.debug(
                    "Updating canonical_id to UUID-only for %s: %s -> %s",
                    identity.registry_id,
                    lock.canonical_id,
                    clean_canonical_id,
                )
                lock.canonical_id = clean_canonical_id
                self._schedule_lock_save()

        return WorkItem(
            registry_id=identity.registry_id,
            config_entry_id=str(identity.config_entry_id),
            canonical_id=clean_canonical_id,
            key_bytes=key_bytes,
            identity=identity,
            lock=lock,
            locked_variant=locked_variant,
            rotation_ts=rotation_ts,
            basis_hint=basis_hint,
        )

    def _collect_work_items(
        self,
        identities: Iterable[DeviceIdentity],
        *,
        now_unix: int,
    ) -> list[WorkItem]:
        """Normalize all identities into work items."""

        items: list[WorkItem] = []
        for identity in identities:
            work_item = self._prepare_work_item(identity, now_unix=now_unix)
            if work_item is not None:
                items.append(work_item)
        return items

    def _compute_time_windows(
        self,
        work_item: WorkItem,
        *,
        now_unix: int,
        params: RotationParams,
    ) -> tuple[list[WindowSpec], bool]:
        """Compute time windows for a work item and report hint validity."""

        lock_windows = self._compute_lock_windows(work_item, now_unix=now_unix, params=params)
        if lock_windows:
            return lock_windows, False

        return self._compute_relative_windows(work_item, now_unix=now_unix, params=params)

    def _compute_lock_windows(
        self,
        work_item: WorkItem,
        *,
        now_unix: int,
        params: RotationParams,
    ) -> list[WindowSpec]:
        """Return windows derived from a valid lock rotation timestamp."""

        if work_item.rotation_ts is None or work_item.lock is None:
            return []

        counter_windows: list[WindowCandidate] = []
        time_since_lock = max(0, now_unix - work_item.lock.created_at)
        periods_elapsed = time_since_lock // params.rotation_period
        for step in range(-2, 3):
            offset_periods = periods_elapsed + step
            window_ts = work_item.rotation_ts + (offset_periods * params.rotation_period)
            if window_ts < 0:
                continue
            semantic_offset = window_ts - now_unix
            counter_windows.append(
                WindowCandidate(
                    timestamp=window_ts,
                    semantic_offset=semantic_offset,
                    time_basis="lock_tracking",
                    candidate_value=work_item.rotation_ts,
                )
            )

        if not counter_windows:
            return []

        return [
            WindowSpec(
                time_basis="lock_tracking",
                candidate_value=work_item.rotation_ts,
                windows=tuple(counter_windows),
            )
        ]

    def _compute_relative_windows(
        self,
        work_item: WorkItem,
        *,
        now_unix: int,
        params: RotationParams,
    ) -> tuple[list[WindowSpec], bool]:
        """Return relative/absolute windows and whether the basis hint was invalid."""

        counter_windows: list[WindowSpec] = []
        counter_bases: tuple[tuple[str, str], ...] = (
            ("unix", "unix"),
            ("pair_date", "pair_date"),
            ("secrets_creation_date", "secrets_creation_date"),
        )
        anchor_candidates: list[int] = []
        if isinstance(work_item.identity.pair_date, int) and work_item.identity.pair_date > 0:
            anchor_candidates.append(work_item.identity.pair_date)
        if (
            isinstance(work_item.identity.secrets_creation_date, int)
            and work_item.identity.secrets_creation_date > 0
        ):
            anchor_candidates.append(work_item.identity.secrets_creation_date)
        best_anchor = max(anchor_candidates) if anchor_candidates else 0
        provisioning_counter = max(0, now_unix - best_anchor) if best_anchor > 0 else 0
        drift_seconds = provisioning_counter * 0.00005
        drift_windows = math.ceil(drift_seconds / params.rotation_period)

        available_bases = {basis for basis, _ in counter_bases}
        known_basis = (
            work_item.basis_hint if work_item.basis_hint in available_bases else None
        )
        invalid_hint = bool(work_item.basis_hint and known_basis is None)
        filtered_bases = (
            tuple((basis, label) for basis, label in counter_bases if basis == known_basis)
            if known_basis
            else counter_bases
        )

        for basis, label in filtered_bases:
            candidate_value: int | None = None
            total_window: int = 0

            if basis == "unix":
                if not ENABLE_ABSOLUTE_UNIX_BASIS:
                    continue
                candidate_value = now_unix
                try:
                    tz_offset = dt_util.now().utcoffset()
                    tz_windows = (
                        int(math.ceil(abs(tz_offset.total_seconds()) / params.rotation_period))
                        if tz_offset
                        else 0
                    )
                except Exception:
                    tz_windows = 0
                total_window = max(params.min_unix_window, 3 + drift_windows + tz_windows)
            else:
                raw_anchor = getattr(work_item.identity, basis, None)
                anchor_value = _normalize_counter_candidate(raw_anchor, basis=basis)
                if anchor_value is None:
                    continue
                candidate_value = now_unix - anchor_value
                total_window = min(params.min_relative_window + drift_windows, params.max_window)

            normalized = _normalize_counter_candidate(candidate_value, basis=basis)
            if normalized is None:
                continue

            window_candidates: list[WindowCandidate] = []
            for ts in iter_rotation_windows(
                normalized,
                rotation_period=params.rotation_period,
                window_range=range(-total_window, total_window + 1),
                include_neighbors=False,
            ):
                if ts < 0:
                    continue
                semantic_offset = ts - normalized
                window_candidates.append(
                    WindowCandidate(
                        timestamp=ts,
                        semantic_offset=semantic_offset,
                        time_basis=label,
                        candidate_value=normalized,
                    )
                )
            if window_candidates:
                counter_windows.append(
                    WindowSpec(
                        time_basis=label,
                        candidate_value=normalized,
                        windows=tuple(window_candidates),
                    )
                )

        return counter_windows, invalid_hint

    def _compute_variants(
        self,
        work_item: WorkItem,
        window: WindowSpec,
    ) -> tuple[VariantSpec, ...]:
        """Return the variants to generate for a window."""

        if work_item.locked_variant is not None:
            return tuple(
                VariantSpec(
                    key_bytes=work_item.key_bytes,
                    variant=work_item.locked_variant,
                    window=window_candidate,
                )
                for window_candidate in window.windows
            )

        return tuple(
            VariantSpec(
                key_bytes=work_item.key_bytes,
                variant=variant,
                window=window_candidate,
            )
            for variant in (
                EidVariant.LEGACY_SECP160R1_X20_BE,
                EidVariant.MODERN_P256_X32_BE,
                EidVariant.MODERN_P256_X20_TRUNC_BE,
                EidVariant.MODERN_P256_X32_LE_SCALAR,
                EidVariant.MODERN_P256_X20_TRUNC_LE,
            )
            for window_candidate in window.windows
        )

    def _generate_eids_from_spec(
        self,
        variant_spec: VariantSpec,
    ) -> Iterable[GeneratedEid]:
        """Generate EIDs for a variant specification."""

        try:
            eid_bytes = self._generate_variant(
                variant_spec.key_bytes,
                time_counter=variant_spec.window.timestamp,
                variant=variant_spec.variant,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            _LOGGER.debug(
                "Skipping EID generation for variant=%s basis=%s window_ts=%s: %s",
                variant_spec.variant.value,
                variant_spec.window.time_basis,
                variant_spec.window.timestamp,
                exc,
            )
            return []

        generated: list[GeneratedEid] = [
            GeneratedEid(
                eid_bytes=eid_bytes,
                is_reversed=False,
                variant=variant_spec.variant,
                window=variant_spec.window,
            )
        ]
        if variant_spec.include_reverse:
            generated.append(
                GeneratedEid(
                    eid_bytes=eid_bytes[::-1],
                    is_reversed=True,
                    variant=variant_spec.variant,
                    window=variant_spec.window,
                )
            )
        return generated

    async def _refresh_cache(self) -> None:
        """Rebuild the EID lookup table for all active devices."""

        self._ensure_cache_defaults()
        if self._load_task is not None:
            await asyncio.shield(self._load_task)

        now_unix = int(time.time())
        self._purge_stale_locks(now=now_unix)
        rotation_params = self._build_rotation_params()

        identities = await self._collect_device_secrets()
        _LOGGER.debug("Refresh start: %d identities discovered", len(identities))
        work_items = self._collect_work_items(identities, now_unix=now_unix)
        _LOGGER.debug("Refresh stage: collected %d work items", len(work_items))
        builder = CacheBuilder()

        for work_item in work_items:
            windows, invalid_hint = self._compute_time_windows(
                work_item, now_unix=now_unix, params=rotation_params
            )
            if invalid_hint:
                self._known_timebases.pop(work_item.registry_id, None)
            _LOGGER.debug(
                "Refresh stage: %s produced %d window groups", work_item.registry_id, len(windows)
            )
            for window in windows:
                variants = self._compute_variants(work_item, window)
                for variant_spec in variants:
                    for generated in self._generate_eids_from_spec(variant_spec):
                        match = EIDMatch(
                            device_id=work_item.registry_id,
                            config_entry_id=work_item.config_entry_id,
                            canonical_id=work_item.canonical_id,
                            time_offset=generated.window.semantic_offset,
                            is_reversed=generated.is_reversed,
                        )
                        builder.register_eid(
                            generated.eid_bytes,
                            match=match,
                            variant=generated.variant,
                            window=generated.window,
                            advertisement_reversed=generated.is_reversed,
                        )

        self._lookup, self._lookup_metadata = builder.finalize()
        _LOGGER.debug(
            "Refresh stage: finalize complete (lookup=%d, metadata=%d)",
            len(self._lookup),
            len(self._lookup_metadata),
        )
        _LOGGER.debug(
            "Refreshed EID cache for %d devices (%d cached EIDs)",
            len(work_items),
            len(self._lookup),
        )

    @staticmethod
    def _infer_variant_from_length(eid_length: int) -> str:
        """Infer a reasonable variant string from the observed EID length."""

        if eid_length == LEGACY_EID_LENGTH:
            return EidVariant.LEGACY_SECP160R1_X20_BE.value
        if eid_length == MODERN_EID_LENGTH:
            return EidVariant.MODERN_P256_X32_BE.value
        return EidVariant.MODERN_P256_X20_TRUNC_BE.value

    def _generate_variant(
        self,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        """Generate an EID for a specific profile."""

        return generate_eid_variant(
            key_bytes,
            time_counter,
            variant,
            strict=False,
        )

    async def _collect_device_secrets(self) -> list[DeviceIdentity]:
        """Retrieve active tracker identities from all loaded coordinators."""
        bucket = self.hass.data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return []

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
        identities: Sequence[DeviceIdentity | Mapping[str, Any]],
        *,
        cache: TokenCache | None,
    ) -> list[DeviceIdentity]:
        """Ensure each device identity has a usable plaintext key."""

        normalized: list[DeviceIdentity] = []
        for identity in identities:
            if isinstance(identity, Mapping):
                registry_id = identity.get("registry_id")
                canonical_id = identity.get("canonical_id")
                if not isinstance(registry_id, str) or not isinstance(
                    canonical_id, str
                ):
                    continue

                manufacturer = _normalize_optional_string(identity.get("manufacturer"))
                model = _normalize_optional_string(identity.get("model"))
                encrypted_account_key = _normalize_encrypted_blob(
                    identity.get("encrypted_account_key")
                    or identity.get("encryptedAccountKey")
                )
                public_key_address = _normalize_encrypted_blob(
                    identity.get("public_key_address")
                    or identity.get("encryptedSha256AccountKeyPublicAddress")
                )

                target_identity = DeviceIdentity(
                    registry_id=registry_id,
                    canonical_id=canonical_id,
                    identity_key=identity.get("identity_key"),
                    encrypted_identity_key=identity.get("encrypted_identity_key"),
                    owner_key_version=identity.get("owner_key_version"),
                    device_type=identity.get("device_type"),
                    config_entry_id=identity.get("config_entry_id"),
                    fast_pair_model_id=identity.get("fast_pair_model_id"),
                    manufacturer=manufacturer,
                    model=model,
                    pair_date=identity.get("pair_date"),
                    secrets_creation_date=identity.get("secrets_creation_date"),
                    encrypted_account_key=encrypted_account_key,
                    public_key_address=public_key_address,
                    time_anchors_debug=identity.get("time_anchors_debug"),
                )
            elif isinstance(identity, DeviceIdentity):
                target_identity = identity
            else:
                continue

            if target_identity.identity_key is None:
                result = await self._try_decrypt_identity_key(
                    target_identity, cache=cache
                )
                if result is None:
                    continue
                normalized.append(replace(target_identity, identity_key=result.key))
                continue

            normalized.append(target_identity)

        return normalized

    async def _try_decrypt_identity_key(
        self,
        identity: DeviceIdentity,
        *,
        cache: TokenCache | None,
    ) -> DecryptionResult | None:
        """Decrypt encrypted identity key when owner/shared key material is available."""

        self._ensure_cache_defaults()
        if (
            cache is None
            or identity.encrypted_identity_key is None
            or not isinstance(identity.encrypted_identity_key, (bytes, bytearray))
        ):
            return None

        owner_key_info = await self._resolve_owner_key(identity, cache=cache)
        if owner_key_info is None:
            return None

        key_sources: list[tuple[str, bytes]] = [("owner", owner_key_info.key)]
        try:
            shared_key = await async_get_shared_key(cache=cache)
        except Exception:  # pragma: no cover - best-effort
            shared_key = None
        if shared_key is not None:
            key_sources.append(("shared", shared_key))

        encrypted_identity_key = bytes(identity.encrypted_identity_key)
        suggested_mcu = is_mcu_tracker(
            device_type=identity.device_type,
            fast_pair_model_id=identity.fast_pair_model_id,
        )
        candidates = [suggested_mcu, not suggested_mcu]

        for key_source, wrapping_key in key_sources:
            for flip_mcu in candidates:
                candidate_key = (
                    flip_bits(encrypted_identity_key, True)
                    if flip_mcu
                    else encrypted_identity_key
                )
                try:
                    decrypted = await asyncio.to_thread(
                        decrypt_eik, wrapping_key, candidate_key
                    )
                except InvalidTag:
                    envelope = self._unwrap_aesgcm_envelope(
                        envelope=candidate_key,
                        wrapping_key=wrapping_key,
                        key_source=key_source,
                        aad_label="registry_id",
                        aad_value=identity.registry_id,
                    )
                    if envelope is not None:
                        self._decryption_status[identity.registry_id] = (
                            envelope.metadata.get("status", "")
                        )
                        return envelope
                    continue
                except Exception:  # noqa: BLE001
                    continue

                if isinstance(decrypted, (bytes, bytearray)):
                    metadata: dict[str, Any] = {
                        "status": "decrypted",
                        "key_source": key_source,
                    }
                    if owner_key_info.version is not None:
                        metadata["key_version"] = owner_key_info.version

                    result = DecryptionResult(key=bytes(decrypted), metadata=metadata)
                    self._decryption_status[identity.registry_id] = metadata["status"]
                    return result

        return None

    def _unwrap_aesgcm_envelope(
        self,
        *,
        envelope: bytes,
        wrapping_key: bytes,
        key_source: str,
        aad_label: str,
        aad_value: str,
    ) -> DecryptionResult | None:
        """Attempt to unwrap an AES-GCM envelope using the provided key."""

        if len(envelope) <= AESGCM_NONCE_LENGTH:
            return None

        nonce = envelope[:AESGCM_NONCE_LENGTH]
        ciphertext = envelope[AESGCM_NONCE_LENGTH:]
        aad = aad_value.encode()

        try:
            plaintext = AESGCM(wrapping_key).decrypt(nonce, ciphertext, aad)
        except InvalidTag:
            return None

        metadata: dict[str, Any] = {
            "status": "decrypted",
            "mode": "aesgcm_envelope",
            "aad_label": aad_label,
            "key_source": key_source,
        }
        return DecryptionResult(key=plaintext, metadata=metadata)

    async def _resolve_owner_key(
        self,
        identity: DeviceIdentity,
        *,
        cache: TokenCache,
    ) -> OwnerKeyInfo | None:
        """Fetch and refresh owner key material when needed."""

        async def _fetch(*, force_refresh: bool) -> OwnerKeyInfo | None:
            try:
                return await async_get_owner_key(
                    cache=cache, force_refresh=force_refresh
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed to retrieve owner key for %s (force_refresh=%s): %s",
                    identity.canonical_id,
                    force_refresh,
                    err,
                )
                return None

        owner_key_info = await _fetch(force_refresh=False)
        if owner_key_info is None:
            return None

        if (
            identity.owner_key_version is not None
            and owner_key_info.version is not None
            and owner_key_info.version < identity.owner_key_version
        ):
            refreshed = await _fetch(force_refresh=True)
            if refreshed is not None:
                return refreshed

        return owner_key_info

    def resolve_eid(self, eid_bytes: bytes) -> EIDMatch | None:  # noqa: PLR0912, PLR0915
        """Resolve a scanned payload to a Home Assistant device registry ID."""

        self._ensure_cache_defaults()
        if not isinstance(eid_bytes, (bytes, bytearray)):
            return None

        raw = bytes(eid_bytes)
        candidates, observed_frame = self._extract_candidates(raw)
        raw_prefix = raw[:4].hex()
        candidate_prefixes = [candidate[:4].hex() for candidate in candidates]

        if not candidates:
            return None

        if not self._lookup:
            is_locked = self._refresh_lock.locked()
            if self._pending_refresh or is_locked:
                _LOGGER.debug(
                    "RESOLVER NOT READY: cache priming (pending=%s locked=%s raw_prefix=%s)",
                    self._pending_refresh,
                    is_locked,
                    raw_prefix,
                )
                return None

            _LOGGER.debug(
                "RESOLVER NOT READY: empty cache; scheduling refresh for raw_prefix=%s",
                raw_prefix,
            )
            refresh = getattr(self, "async_refresh", None)
            create_task = getattr(self.hass, "async_create_task", None)
            if callable(refresh) and callable(create_task):
                self._pending_refresh = True
                try:
                    refresh_coro = refresh()
                    scheduled = create_task(refresh_coro)
                    if scheduled is None:
                        refresh_coro.close()
                    elif asyncio.iscoroutine(scheduled):
                        asyncio.create_task(scheduled)
                except Exception:  # pragma: no cover
                    self._pending_refresh = False
            return None

        for candidate_prefix, candidate in zip(candidate_prefixes, candidates):
            match = self._lookup.get(candidate)
            if match is None:
                continue

            metadata: dict[str, Any] = self._lookup_metadata.get(candidate) or {}
            now = int(time.time())
            self._last_lock_confirmation[match.device_id] = now

            if match.device_id not in self._locks:
                variant_str = str(metadata.get("variant") or "")
                try:
                    variant_value = variant_str or self._infer_variant_from_length(len(candidate))
                except Exception:
                    variant_value = EidVariant.MODERN_P256_X32_BE.value
                time_basis = metadata.get("timestamp_basis")
                rotation_timestamp = metadata.get("rotation_timestamp")
                lock = EIDGenerationLock(
                    device_id=match.device_id,
                    canonical_id=match.canonical_id,
                    variant=variant_value,
                    advertisement_reversed=match.is_reversed,
                    eid_length=len(candidate),
                    rotation_timestamp=int(rotation_timestamp)
                    if isinstance(rotation_timestamp, int)
                    and not isinstance(rotation_timestamp, bool)
                    else None,
                    frame_type=observed_frame,
                    time_basis=time_basis if isinstance(time_basis, str) else None,
                    created_at=now,
                )
                self._locks[match.device_id] = lock
                self._persisted_locks[match.device_id] = lock
                self._schedule_lock_save()

            self._known_offsets[match.device_id] = match.time_offset
            self._known_advertisement_reversed[match.device_id] = match.is_reversed
            self._lock_miss_counts[match.device_id] = 0

            timestamp_basis = metadata.get("timestamp_basis")
            if isinstance(timestamp_basis, str):
                self._known_timebases[match.device_id] = timestamp_basis

            _LOGGER.info(
                (
                    "HIT: device=%s canonical=%s reversed=%s offset=%s "
                    "candidate_prefix=%s raw_prefix=%s"
                ),
                match.device_id,
                match.canonical_id,
                match.is_reversed,
                match.time_offset,
                candidate_prefix,
                raw_prefix,
            )
            return match

        max_prefixes = 8
        prefix_log = ", ".join(candidate_prefixes[:max_prefixes])
        if len(candidate_prefixes) > max_prefixes:
            prefix_log = f"{prefix_log}, ..."

        _LOGGER.debug(
            "RESOLVER MISS: candidate_prefixes=%s raw_prefix=%s cache_size=%d",
            prefix_log or "<none>",
            raw_prefix,
            len(self._lookup),
        )
        return None

    def _extract_candidates(  # noqa: PLR0912
        self, payload: bytes
    ) -> tuple[list[bytes], int | None]:
        """Extract possible EID slices from a BLE payload."""

        length = len(payload)
        candidates: list[bytes] = []
        observed_frame: int | None = None
        allow_sliding_window = True

        if length in (LEGACY_EID_LENGTH, MODERN_EID_LENGTH):
            if not (
                length == MODERN_EID_LENGTH
                and payload[0] in (FMDN_FRAME_TYPE, MODERN_FRAME_TYPE)
            ):
                candidates.append(payload)
                return candidates, None

        if length >= SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH:
            frame_type = payload[7]
            if frame_type == FMDN_FRAME_TYPE:
                observed_frame = frame_type
                candidates.append(
                    payload[
                        SERVICE_DATA_OFFSET : SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH
                    ]
                )
            elif (
                frame_type == MODERN_FRAME_TYPE
                and length >= SERVICE_DATA_OFFSET + MODERN_EID_LENGTH
            ):
                observed_frame = frame_type
                candidates.append(
                    payload[
                        SERVICE_DATA_OFFSET : SERVICE_DATA_OFFSET + MODERN_EID_LENGTH
                    ]
                )

        if not candidates and length >= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH:
            frame_type = payload[0]
            if frame_type in (FMDN_FRAME_TYPE, MODERN_FRAME_TYPE):
                observed_frame = frame_type
                modern_required_length = RAW_HEADER_LENGTH + MODERN_EID_LENGTH

                def _legacy_payload_start() -> int:
                    """Return the starting index for a legacy-length payload slice."""

                    if (
                        length == RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 1
                        and payload[RAW_HEADER_LENGTH] == 0
                        and payload[-1] != 0
                    ):
                        return RAW_HEADER_LENGTH + 1
                    return RAW_HEADER_LENGTH

                if frame_type == FMDN_FRAME_TYPE and length >= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH:
                    payload_start = _legacy_payload_start()
                    candidates.append(
                        payload[payload_start : payload_start + LEGACY_EID_LENGTH]
                    )
                elif frame_type == MODERN_FRAME_TYPE:
                    if length >= modern_required_length:
                        candidates.append(
                            payload[RAW_HEADER_LENGTH : RAW_HEADER_LENGTH + MODERN_EID_LENGTH]
                        )
                        return candidates, observed_frame
                    elif RAW_HEADER_LENGTH + LEGACY_EID_LENGTH <= length <= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 1:
                        payload_start = _legacy_payload_start()
                        candidates.append(
                            payload[payload_start : payload_start + LEGACY_EID_LENGTH]
                        )
                    else:
                        allow_sliding_window = length >= modern_required_length - 1
                        self._log_truncated_frame(
                            frame_type=frame_type,
                            payload_len=length - RAW_HEADER_LENGTH,
                            raw_len=length,
                        )

        if not candidates and length > LEGACY_EID_LENGTH and allow_sliding_window:
            window = min(length - LEGACY_EID_LENGTH + 1, 64)
            for i in range(window):
                slice_20 = payload[i : i + LEGACY_EID_LENGTH]
                if len(slice_20) == LEGACY_EID_LENGTH:
                    candidates.append(slice_20)
                slice_32 = payload[i : i + MODERN_EID_LENGTH]
                if len(slice_32) == MODERN_EID_LENGTH:
                    candidates.append(slice_32)

        return candidates, observed_frame

    def _log_truncated_frame(self, *, frame_type: int, payload_len: int, raw_len: int) -> None:
        """Rate-limit warnings for truncated framed payloads."""

        now = time.time()
        key = (frame_type, payload_len)
        last_log = self._truncated_frame_log_at.get(key)
        if last_log is not None and now - last_log < TRUNCATED_FRAME_LOG_WINDOW_SECONDS:
            return

        self._truncated_frame_log_at[key] = now
        _LOGGER.warning(
            "Truncated or unexpected framed BLE payload: frame=0x%02x payload_len=%s raw_len=%s; "
            "falling back to sliding-window extraction.",
            frame_type,
            payload_len,
            raw_len,
        )

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
        self._lookup_metadata.clear()
        self._locks.clear()

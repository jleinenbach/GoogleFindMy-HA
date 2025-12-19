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
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    ROTATION_PERIOD,
    generate_eid,
    generate_eid_p256,
    generate_eid_p256_le,
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

EID_LENGTH = LEGACY_EID_LENGTH


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
            windows.extend((timestamp, previous_window, next_window))
        else:
            windows.append(timestamp)

    return tuple(dict.fromkeys(windows))


@dataclass(slots=True)
class EIDGenerationLock:
    """Persisted per-device generation profile."""

    device_id: str
    canonical_id: str
    eid_length: int
    frame_type: int | None = None
    scalar_endianness: str = "big"
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        """Serialize lock for storage."""

        return {
            "device_id": self.device_id,
            "canonical_id": self.canonical_id,
            "eid_length": self.eid_length,
            "frame_type": self.frame_type,
            "scalar_endianness": self.scalar_endianness,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EIDGenerationLock:
        """Deserialize a stored lock."""

        return cls(
            device_id=str(payload["device_id"]),
            canonical_id=str(payload.get("canonical_id") or ""),
            eid_length=int(payload["eid_length"]),
            frame_type=payload.get("frame_type"),
            scalar_endianness=str(payload.get("scalar_endianness") or "big"),
            created_at=int(payload.get("created_at") or int(time.time())),
        )


@dataclass(slots=True)
class GoogleFindMyEIDResolver:
    """Resolver that precalculates rotating EIDs for known trackers."""

    hass: HomeAssistant
    _lookup: dict[bytes, EIDMatch] = field(init=False, default_factory=dict)
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(init=False, default_factory=dict)
    _known_offsets: dict[str, int] = field(init=False, default_factory=dict)
    _known_endianness: dict[str, bool] = field(init=False, default_factory=dict)
    _known_timebases: dict[str, str] = field(init=False, default_factory=dict)
    _persisted_locks: dict[str, EIDGenerationLock] = field(init=False, default_factory=dict)
    _decryption_status: dict[str, str] = field(init=False, default_factory=dict)
    _last_lock_confirmation: dict[str, int] = field(init=False, default_factory=dict)
    _provisioning_warn_at: dict[str, float] = field(init=False, default_factory=dict)
    _locks: dict[str, EIDGenerationLock] = field(init=False, default_factory=dict)
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
        if not hasattr(self, "_known_endianness"):
            self._known_endianness = {}
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

    async def _refresh_cache(self) -> None:
        """Rebuild the EID lookup table for all active devices."""

        self._ensure_cache_defaults()
        if self._load_task is not None:
            await asyncio.shield(self._load_task)

        identities = await self._collect_device_secrets()

        now_unix = int(time.time())
        rotation_start = now_unix - (now_unix % ROTATION_PERIOD)
        windows = (
            rotation_start - ROTATION_PERIOD,
            rotation_start,
            rotation_start + ROTATION_PERIOD,
        )

        lookup: dict[bytes, EIDMatch] = {}
        lookup_metadata: dict[bytes, dict[str, Any]] = {}

        for identity in identities:
            if (
                not identity.config_entry_id
                or identity.identity_key is None
                or identity.registry_id is None
            ):
                continue

            key_bytes = bytes(identity.identity_key)
            lock = self._locks.get(identity.registry_id)

            def _register_variant(eid_bytes: bytes, name: str, ts: int, *, scalar_endianness: str, reversed_flag: bool) -> None:
                offset = int(ts - now_unix)
                match = EIDMatch(
                    device_id=identity.registry_id,
                    config_entry_id=str(identity.config_entry_id),
                    canonical_id=identity.canonical_id,
                    time_offset=offset,
                    is_reversed=reversed_flag,
                )
                existing = lookup.get(eid_bytes)
                if existing is not None and abs(existing.time_offset) <= abs(offset):
                    return

                lookup[eid_bytes] = match
                lookup_metadata[eid_bytes] = {
                    "variant": name,
                    "rotation_timestamp": ts,
                    "time_offset": offset,
                    "timestamp_basis": "unix",
                    "scalar_endianness": scalar_endianness,
                    "is_reversed": reversed_flag,
                }

            for window_ts in windows:
                if lock is not None:
                    eid_bytes = self._generate_variant(
                        key_bytes,
                        timestamp=window_ts,
                        eid_length=lock.eid_length,
                        scalar_endianness=lock.scalar_endianness,
                    )
                    _register_variant(
                        eid_bytes,
                        "locked",
                        window_ts,
                        scalar_endianness=lock.scalar_endianness,
                        reversed_flag=False,
                    )
                    _register_variant(
                        eid_bytes[::-1],
                        "locked",
                        window_ts,
                        scalar_endianness=lock.scalar_endianness,
                        reversed_flag=True,
                    )
                else:
                    legacy_eid = self._generate_variant(
                        key_bytes,
                        timestamp=window_ts,
                        eid_length=LEGACY_EID_LENGTH,
                        scalar_endianness="big",
                    )
                    modern_eid = self._generate_variant(
                        key_bytes,
                        timestamp=window_ts,
                        eid_length=MODERN_EID_LENGTH,
                        scalar_endianness="big",
                    )
                    modern_eid_le = self._generate_variant(
                        key_bytes,
                        timestamp=window_ts,
                        eid_length=MODERN_EID_LENGTH,
                        scalar_endianness="little",
                    )
                    _register_variant(
                        legacy_eid,
                        "fhna_secp160r1_rx20",
                        window_ts,
                        scalar_endianness="big",
                        reversed_flag=False,
                    )
                    _register_variant(
                        legacy_eid[::-1],
                        "fhna_secp160r1_rx20",
                        window_ts,
                        scalar_endianness="big",
                        reversed_flag=True,
                    )
                    _register_variant(
                        modern_eid,
                        "fhna_secp256r1_rx32",
                        window_ts,
                        scalar_endianness="big",
                        reversed_flag=False,
                    )
                    _register_variant(
                        modern_eid[::-1],
                        "fhna_secp256r1_rx32",
                        window_ts,
                        scalar_endianness="big",
                        reversed_flag=True,
                    )
                    _register_variant(
                        modern_eid_le,
                        "fhna_secp256r1_le_rx32",
                        window_ts,
                        scalar_endianness="little",
                        reversed_flag=False,
                    )
                    _register_variant(
                        modern_eid_le[::-1],
                        "fhna_secp256r1_le_rx32",
                        window_ts,
                        scalar_endianness="little",
                        reversed_flag=True,
                    )

        self._lookup = lookup
        self._lookup_metadata = lookup_metadata
        _LOGGER.debug(
            "Refreshed EID cache for %d devices (%d cached EIDs)",
            len(identities),
            len(lookup),
        )

    def _generate_variant(
        self,
        key_bytes: bytes,
        *,
        timestamp: int,
        eid_length: int,
        scalar_endianness: str,
    ) -> bytes:
        """Generate an EID for a specific profile."""

        if eid_length == LEGACY_EID_LENGTH:
            return generate_eid(key_bytes, timestamp)
        if scalar_endianness == "little":
            return generate_eid_p256_le(key_bytes, timestamp)
        return generate_eid_p256(key_bytes, timestamp)

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
        identities: list[DeviceIdentity],
        *,
        cache: TokenCache | None,
    ) -> list[DeviceIdentity]:
        """Ensure each device identity has a usable plaintext key."""
        normalized: list[DeviceIdentity] = []
        for identity in identities:
            if identity.identity_key is None:
                result = await self._try_decrypt_identity_key(identity, cache=cache)
                if result is None:
                    continue
                normalized.append(replace(identity, identity_key=result.key))
                continue

            normalized.append(identity)

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
                    decrypted = await asyncio.to_thread(decrypt_eik, wrapping_key, candidate_key)
                except InvalidTag:
                    envelope = self._unwrap_aesgcm_envelope(
                        envelope=candidate_key,
                        wrapping_key=wrapping_key,
                        key_source=key_source,
                        aad_label="registry_id",
                        aad_value=identity.registry_id,
                    )
                    if envelope is not None:
                        self._decryption_status[identity.registry_id] = envelope.metadata.get("status", "")
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
                return await async_get_owner_key(cache=cache, force_refresh=force_refresh)
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

        if not candidates:
            return None

        eid_prefix = raw[:4].hex()

        if not self._lookup:
            is_locked = self._refresh_lock.locked()
            if self._pending_refresh or is_locked:
                _LOGGER.debug(
                    "RESOLVER NOT READY: cache priming (pending=%s locked=%s prefix=%s)",
                    self._pending_refresh,
                    is_locked,
                    eid_prefix,
                )
                return None

            _LOGGER.debug("RESOLVER NOT READY: empty cache; scheduling refresh for prefix=%s", eid_prefix)
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

        for c in candidates:
            match = self._lookup.get(c)
            if match is None:
                continue

            if match.device_id not in self._locks:
                lock = EIDGenerationLock(
                    device_id=match.device_id,
                    canonical_id=match.canonical_id,
                    eid_length=len(c),
                    frame_type=observed_frame,
                )
                self._locks[match.device_id] = lock
                self._persisted_locks[match.device_id] = lock
                self._last_lock_confirmation[match.device_id] = int(time.time())
                save_task = getattr(self.hass, "async_create_task", None)
                if callable(save_task) and hasattr(self, "_store"):
                    try:
                        save_coro = self._async_save_locks()
                        scheduled = save_task(save_coro)
                        if scheduled is None:
                            save_coro.close()
                        elif asyncio.iscoroutine(scheduled):
                            asyncio.create_task(scheduled)
                    except Exception:  # pragma: no cover - defensive
                        _LOGGER.debug("Failed to schedule lock persistence for %s", match.device_id)

            self._known_offsets[match.device_id] = match.time_offset
            self._known_endianness[match.device_id] = match.is_reversed

            metadata = self._lookup_metadata.get(c)
            if isinstance(metadata, dict):
                timestamp_basis = metadata.get("timestamp_basis")
                if isinstance(timestamp_basis, str):
                    self._known_timebases[match.device_id] = timestamp_basis

            _LOGGER.info(
                "HIT: device=%s canonical=%s reversed=%s offset=%s eid_prefix=%s",
                match.device_id,
                match.canonical_id,
                match.is_reversed,
                match.time_offset,
                eid_prefix,
            )
            return match

        _LOGGER.debug("RESOLVER MISS: prefix=%s cache_size=%d", eid_prefix, len(self._lookup))
        return None

    def _extract_candidates(self, payload: bytes) -> tuple[list[bytes], int | None]:
        """Extract possible EID slices from a BLE payload."""

        length = len(payload)
        candidates: list[bytes] = []
        observed_frame: int | None = None

        if length in (LEGACY_EID_LENGTH, MODERN_EID_LENGTH):
            candidates.append(payload)
            return candidates, None

        if length >= SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH:
            frame_type = payload[7]
            if frame_type == FMDN_FRAME_TYPE:
                observed_frame = frame_type
                candidates.append(payload[SERVICE_DATA_OFFSET : SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH])
            elif frame_type == MODERN_FRAME_TYPE and length >= SERVICE_DATA_OFFSET + MODERN_EID_LENGTH:
                observed_frame = frame_type
                candidates.append(payload[SERVICE_DATA_OFFSET : SERVICE_DATA_OFFSET + MODERN_EID_LENGTH])

        if not candidates and length >= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH:
            frame_type = payload[0]
            if frame_type in (FMDN_FRAME_TYPE, MODERN_FRAME_TYPE):
                observed_frame = frame_type
                expected_len = LEGACY_EID_LENGTH if frame_type == FMDN_FRAME_TYPE else MODERN_EID_LENGTH
                if length >= RAW_HEADER_LENGTH + expected_len:
                    candidates.append(payload[RAW_HEADER_LENGTH : RAW_HEADER_LENGTH + expected_len])

        if not candidates and length > LEGACY_EID_LENGTH:
            if (
                length > RAW_HEADER_LENGTH
                and payload[0] == MODERN_FRAME_TYPE
                and length < RAW_HEADER_LENGTH + MODERN_EID_LENGTH
            ):
                return candidates, observed_frame

            window = min(length - LEGACY_EID_LENGTH + 1, 64)
            for i in range(window):
                slice_20 = payload[i : i + LEGACY_EID_LENGTH]
                if len(slice_20) == LEGACY_EID_LENGTH:
                    candidates.append(slice_20)
                slice_32 = payload[i : i + MODERN_EID_LENGTH]
                if len(slice_32) == MODERN_EID_LENGTH:
                    candidates.append(slice_32)

        return candidates, observed_frame

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

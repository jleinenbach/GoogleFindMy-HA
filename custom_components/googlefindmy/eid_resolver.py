# custom_components/googlefindmy/eid_resolver.py
"""Ephemeral identifier resolver for local BLE scans.

The resolver precomputes ephemeral identifiers (EIDs) for known trackers using
the FHNA/FMDN Table 10 PRF helpers and maps BLE scan payloads to Home Assistant
device registry identifiers. Heavy cryptography and key-unwrapping happen
outside the hot path; ``resolve_eid`` only performs lookups.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast, runtime_checkable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .Auth.username_provider import username_string
from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto._lazy_crypto import get_aesgcm_class, get_invalid_tag_exception
from .FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    P256_ORDER,
    ROTATION_PERIOD,
    ROTATION_PERIOD_900,
    ROTATION_PERIOD_3600,
    EidVariant,
    HeuristicBasis,
    HeuristicEidResult,
    compute_flags_xor_mask,
    generate_eid_variant,
    generate_heuristic_eid,
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

# FMDN hashed_flags byte: bit-extraction constants for the XOR-decoded byte.
# Why these constants exist: the FMDN spec numbers bits MSB-first while
# Python uses LSB-first arithmetic — a naive translation flips the
# battery and UWT fields silently. The constants make the mapping explicit
# and grep-stable; the canonical spec quote, bit-layout table and the
# anti-pattern warning live at the decode site (see
# GoogleFindMyEIDResolver._update_ble_battery — CANONICAL SPEC ANCHOR).
# Reference: https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn#hashed_flags
# (retrieved 2026-06-02). Drift between code and spec is guarded by
# tests/test_ble_battery_sensor.py::TestFmdnHashedFlagsConstantsMatchSpec.
FMDN_HASHED_FLAGS_BATTERY_MASK = 0x03  # 2-bit field after right-shift
FMDN_HASHED_FLAGS_BATTERY_SHIFT = 1
FMDN_HASHED_FLAGS_UWT_MODE_MASK = 0x01  # 1-bit field, standard LSB

# Curve parameters for compute_flags_xor_mask(), keyed by EidVariant.
# (curve_byte_len, curve_order): Legacy uses secp160r1 (None=default), P256 uses P256_ORDER.
_VARIANT_CURVE_PARAMS: dict[EidVariant, tuple[int, int | None]] = {
    EidVariant.LEGACY_SECP160R1_X20_BE: (LEGACY_EID_LENGTH, None),
    EidVariant.MODERN_P256_X32_BE: (MODERN_EID_LENGTH, P256_ORDER),
    EidVariant.MODERN_P256_X20_TRUNC_BE: (MODERN_EID_LENGTH, P256_ORDER),
    EidVariant.MODERN_P256_X32_LE_SCALAR: (MODERN_EID_LENGTH, P256_ORDER),
    EidVariant.MODERN_P256_X20_TRUNC_LE: (MODERN_EID_LENGTH, P256_ORDER),
}

# Per-variant crypto memoization (AP-A).
# Both EID derivation (generate_eid_variant) and the flags XOR mask
# (compute_flags_xor_mask) are pure functions of their inputs and run once per
# (key, time_counter, variant/curve) tuple on every rotation refresh. A bounded
# LRU per resolver instance turns the repeated within-rotation and
# back-to-back-refresh recomputation into a dict lookup while staying byte-exact.
#
# functools.lru_cache is deliberately NOT used: as a method decorator it keys on
# ``self`` (and pins it), leaking the resolver and preventing per-instance caches.
# The minimal OrderedDict+Lock helper below keys only on the crypto inputs.
#
# _EID_MEMO_MAXSIZE = 512: 13 devices x 5 variants x 4 concurrently live
# time_counter windows ~= 260, x ~2 headroom -> 512 (next power of two).
_EID_MEMO_MAXSIZE = 512
# _EID_MASK_MEMO_MAXSIZE = 512: same dimensioning; mask key cardinality is
# <= devices x windows x 2 curves < 512 (no per-variant fan-out for the mask).
_EID_MASK_MEMO_MAXSIZE = 512

# Heuristic phone-discovery memoization (AP-SWEEP).
# ``generate_heuristic_eid`` is the on-loop sibling of the build-side crypto: it
# runs inside ``_resolve_eid_internal`` -> ``_heuristic_resolve`` on every
# cache-miss FMDN advertisement (an HA-Bluetooth callback that fires on the
# event loop for EVERY advertisement). The slow path fans out over
# HEURISTIC_HYPOTHESES x HEURISTIC_VARIANTS_TO_TEST (4 x 4) and each call derives
# several EC points, yet its result list is a pure function of the rotation-
# aligned time window plus the hypothesis parameters. Consecutive advertisements
# from the same nearby phone fall in the same window and recompute identical
# lists; a bounded LRU per resolver instance collapses that into a dict lookup
# while staying byte-exact (the key carries every output-determining parameter,
# so a window-boundary mismatch can only cause a benign miss, never a stale hit).
#
# _HEURISTIC_MEMO_MAXSIZE = 256: <= identities (~13) x hypotheses (4) x variants
# (4) x ~2 concurrently live windows ~= 416 worst case for the learned + slow
# paths combined, but in practice only a handful of phones reach the slow path;
# 256 covers the realistic working set with LRU headroom and bounds memory.
_HEURISTIC_MEMO_MAXSIZE = 256


class _BoundedLRUCache:
    """Thread-safe bounded LRU keyed on hashable crypto inputs.

    A tiny instance-bound replacement for ``functools.lru_cache`` that keys on
    explicit input tuples rather than ``self``. The lock guards only the
    OrderedDict lookup/store, never the wrapped crypto call, so concurrent
    worker threads (AP-C) can run the actual derivation in parallel and only
    serialize on the cheap dict access.
    """

    def __init__(self, maxsize: int) -> None:
        """Initialize an empty cache bounded to ``maxsize`` entries."""

        self._maxsize = maxsize
        self._store: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        """Return the cached value for ``key`` or ``None`` on a miss.

        ``None`` is never stored as a value, so a ``None`` return is an
        unambiguous miss signal for the callers in this module.
        """

        with self._lock:
            if key not in self._store:
                return None
            # Mark as most-recently-used.
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key: Any, value: Any) -> None:
        """Store ``value`` under ``key``, evicting the LRU entry past maxsize."""

        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        """Return the current number of cached entries."""

        with self._lock:
            return len(self._store)


# Strategy Configuration
# ENABLE_ABSOLUTE_UNIX_BASIS: If True, scans for EIDs based on absolute Unix time (Deep Scan).
# Proven unreliable for standard trackers (Motorola/Pebblebee), so disabled by default.
ENABLE_ABSOLUTE_UNIX_BASIS: bool = False

# MIN_UNIX_WINDOW_SIZE: Safety net for absolute scans. 128 * 1024s ~= 36h.
MIN_UNIX_WINDOW_SIZE: int = 128

# MIN_RELATIVE_WINDOW_SIZE: Safety net for relative scans (pair_date).
# 5 * 1024s ~= 85 mins. Essential to absorb 1h Daylight Saving Time (DST) shifts.
MIN_RELATIVE_WINDOW_SIZE: int = 5

# LOCK_FALLBACK_RELATIVE_WINDOW_SIZE: When a lock exists, we still generate a limited
# set of relative discovery windows as a self-healing fallback.
LOCK_FALLBACK_RELATIVE_WINDOW_SIZE: int = 3

EID_LENGTH = LEGACY_EID_LENGTH
LOCK_TTL_SECONDS = 7 * 24 * 60 * 60
LOCK_CONFIRMATION_TTL_SECONDS = 90 * 60
TRUNCATED_FRAME_LOG_WINDOW_SECONDS = 60
VALID_ANCHOR_BASES: set[str] = {"unix", "pair_date", "secrets_creation_date"}
KNOWN_OFFSET_KEY_LENGTH = 2

# LOCK_TRACKING_WINDOW_STEPS: Number of rotation periods to scan around the
# expected counter when tracking a locked device. ±2 periods covers ~34 minutes
# of clock drift at 1024s rotation.
LOCK_TRACKING_WINDOW_STEPS = 2

# =============================================================================
# Heuristic Phone Discovery Configuration
# =============================================================================
# Android phones with "Offline Finding" may use different parameters than
# standard FMDN trackers. The heuristic resolver tests multiple hypotheses
# when the standard lookup fails.

# ENABLE_HEURISTIC_PHONE_DISCOVERY: Enable the reactive heuristic check for
# packets that would otherwise be logged as MISS. This only runs when the
# standard cache lookup fails.
ENABLE_HEURISTIC_PHONE_DISCOVERY: bool = True

# HEURISTIC_HYPOTHESES: Ordered list of (rotation_period, basis) tuples to test.
# Ordered by likelihood based on Android phone behavior analysis.
HEURISTIC_HYPOTHESES: tuple[tuple[int, HeuristicBasis], ...] = (
    (ROTATION_PERIOD_900, HeuristicBasis.ABSOLUTE),  # Most likely for phones
    (ROTATION_PERIOD_3600, HeuristicBasis.ABSOLUTE),  # 1-hour rotation
    (ROTATION_PERIOD_900, HeuristicBasis.RELATIVE),  # 15-min with anchor
    (ROTATION_PERIOD, HeuristicBasis.ABSOLUTE),  # 1024s with absolute time
)

# HEURISTIC_VARIANTS_TO_TEST: EID variants to test during heuristic resolution.
# P-256 variants are most common for modern Android phones.
HEURISTIC_VARIANTS_TO_TEST: tuple[EidVariant, ...] = (
    EidVariant.MODERN_P256_X20_TRUNC_BE,
    EidVariant.MODERN_P256_X32_BE,
    EidVariant.MODERN_P256_X20_TRUNC_LE,
    EidVariant.LEGACY_SECP160R1_X20_BE,
)

# HEURISTIC_LOG_INTERVAL_SECONDS: Minimum interval between heuristic miss logs
# for the same device to prevent log spam.
HEURISTIC_LOG_INTERVAL_SECONDS: int = 300  # 5 minutes


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


@dataclass(slots=True)
class BLEBatteryState:
    """Decoded battery state from FMDN hashed-flags BLE advertisement.

    Attributes:
        battery_level: Raw FMDN value (0=UNSUPPORTED, 1=NORMAL, 2=LOW, 3=CRITICALLY_LOW).
        battery_pct: Mapped percentage (100, 25, 5) or None for UNSUPPORTED (0).
        uwt_mode: True if Unwanted Tracking mode is active.
        decoded_flags: Fully decoded flags byte (after XOR).
        observed_at_wall: Wall-clock timestamp of the BLE observation (time.time()).
    """

    battery_level: int
    battery_pct: int | None
    uwt_mode: bool
    decoded_flags: int
    observed_at_wall: float


@dataclass(slots=True)
class BLEScanInfo:
    """Last observed BLE scan metadata for a device.

    Stored during EID resolution when a ``ble_address`` is provided by the
    caller (typically Bermuda or another BLE scanner).  Used by the future
    BLE ring fallback (Phase 2) to locate the device for a direct GATT
    connection.

    Attributes:
        ble_address: Current BLE MAC address (rotates every ~15 min on FMDN).
        observed_at: Monotonic timestamp (:func:`time.monotonic`) of the scan.
        observed_at_wall: Wall-clock timestamp (:func:`time.time`) of the scan.
    """

    ble_address: str
    observed_at: float
    observed_at_wall: float


# Mapping from FMDN 2-bit battery level to percentage.
# Per Google FMDN spec: 0=Unsupported, 1=Normal, 2=Low, 3=Critically Low.
# HA icon thresholds in homeassistant/helpers/icon.py:
#   100% → mdi:battery, 25% → mdi:battery-20, 5% → mdi:battery-alert
FMDN_BATTERY_PCT: dict[int, int | None] = {0: None, 1: 100, 2: 25, 3: 5}


@runtime_checkable
class _IdentityProvider(Protocol):
    """Protocol implemented by coordinators that can provide device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]: ...


@runtime_checkable
class GoogleFindMyEIDResolverProtocol(Protocol):  # pylint: disable=unnecessary-ellipsis
    """Protocol defining the public interface for EID resolution.

    This abstraction layer enables:
    - Bermuda and other integrations to resolve EIDs without tight coupling
    - Easy mocking in tests without depending on concrete implementation
    - Clear documentation of the public EID resolver API surface

    The resolver maintains a lookup table mapping ephemeral identifiers (EIDs)
    to device identities, refreshing periodically as EIDs rotate.
    """

    async def async_refresh(self) -> None:
        """Refresh the EID lookup table from all active coordinators.

        This rebuilds the mapping of EID bytes to device identities by:
        1. Collecting identities from all registered coordinators
        2. Decrypting identity keys where owner keys are available
        3. Generating EIDs for the current rotation window
        """
        ...

    def reset_device_offset(self, registry_id: str) -> None:
        """Clear cached time offset for a specific device.

        Use when a device's EID timing needs to be re-learned, e.g.,
        after firmware updates or clock drift corrections.
        """
        ...

    def resolve_eid(
        self, eid_bytes: bytes, *, ble_address: str | None = None
    ) -> EIDMatch | None:
        """Resolve EID bytes to a matching device identity.

        Args:
            eid_bytes: Raw EID bytes from a BLE advertisement.
            ble_address: Optional BLE MAC address of the advertising device.
                When provided, stored for future direct GATT connections.

        Returns:
            EIDMatch with device identity info, or None if no match found.
            When multiple accounts share the same device, returns the match
            with the smallest time_offset (best match).
            Use resolve_eid_all() to get all matches for shared devices.
        """
        ...

    def resolve_eid_all(
        self, eid_bytes: bytes, *, ble_address: str | None = None
    ) -> list[EIDMatch]:
        """Resolve EID bytes to all matching device identities.

        This method supports shared devices: when the same physical tracker
        is shared between accounts, all accounts' matches are returned.

        Args:
            eid_bytes: Raw EID bytes from a BLE advertisement.
            ble_address: Optional BLE MAC address of the advertising device.
                When provided, stored for future direct GATT connections.

        Returns:
            List of EIDMatch entries for all accounts that share this device.
            Empty list if no match found.
        """
        ...

    def get_ble_scan_info(self, device_id: str) -> BLEScanInfo | None:
        """Return last observed BLE scan metadata for a device, or None.

        Args:
            device_id: The canonical_id (Google API device identifier).
        """
        ...

    def stop(self) -> None:
        """Stop the resolver and release resources.

        Cancels periodic refresh timers and cleanup tasks.
        """
        ...


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
class LearnedHeuristicParams:
    """Learned heuristic parameters for a device discovered via heuristic resolution.

    When a device is successfully matched using heuristic parameters, those
    parameters are cached here to speed up future lookups.
    """

    device_id: str
    canonical_id: str
    rotation_period: int
    basis: HeuristicBasis
    variant: EidVariant
    discovered_at: int  # Unix timestamp when discovered
    last_confirmed_at: int  # Unix timestamp of last successful match
    confirmation_count: int = 1  # Number of successful matches

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "device_id": self.device_id,
            "canonical_id": self.canonical_id,
            "rotation_period": self.rotation_period,
            "basis": self.basis.value,
            "variant": self.variant.value,
            "discovered_at": self.discovered_at,
            "last_confirmed_at": self.last_confirmed_at,
            "confirmation_count": self.confirmation_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LearnedHeuristicParams:
        """Deserialize from storage."""
        return cls(
            device_id=str(data["device_id"]),
            canonical_id=str(data.get("canonical_id", "")),
            rotation_period=int(data["rotation_period"]),
            basis=HeuristicBasis(data["basis"]),
            variant=EidVariant(data["variant"]),
            discovered_at=int(data.get("discovered_at", 0)),
            last_confirmed_at=int(data.get("last_confirmed_at", 0)),
            confirmation_count=int(data.get("confirmation_count", 1)),
        )


@dataclass(slots=True)
class CacheBuilder:
    """Immutable-safe cache builder enforcing lookup/metadata invariants.

    Supports multiple matches per EID to handle shared devices across accounts.
    When the same physical tracker is shared between accounts, each account
    registers its own EIDMatch for the same EID bytes.
    """

    lookup: dict[bytes, list[EIDMatch]] = field(default_factory=dict)
    metadata: dict[bytes, dict[str, Any]] = field(default_factory=dict)

    def register_eid(  # noqa: PLR0913
        self,
        eid_bytes: bytes,
        *,
        match: EIDMatch,
        variant: EidVariant,
        window: WindowCandidate,
        advertisement_reversed: bool,
        flags_xor_mask: int | None = None,
    ) -> None:
        """Register an EID and metadata, supporting multiple matches per EID.

        When the same EID is registered by multiple accounts (shared devices),
        all matches are stored. For metadata, the entry with the smallest
        time_offset is used as the primary.
        """
        existing_matches = self.lookup.get(eid_bytes, [])
        existing_metadata = self.metadata.get(eid_bytes)
        existing_bases: set[str] | None = None
        if existing_metadata is not None:
            existing_bases = set(existing_metadata.get("timestamp_bases") or ())
            if (basis := existing_metadata.get("timestamp_basis")) and isinstance(
                basis, str
            ):
                existing_bases.add(basis)
            existing_bases.add(window.time_basis)

        # Check if this device_id already has a match for this EID
        existing_device_match: EIDMatch | None = None
        existing_device_idx: int | None = None
        for idx, existing in enumerate(existing_matches):
            if existing.device_id == match.device_id:
                existing_device_match = existing
                existing_device_idx = idx
                break

        # If this device already has a match with better or equal offset, skip
        if existing_device_match is not None and abs(
            existing_device_match.time_offset
        ) <= abs(match.time_offset):
            if existing_metadata is not None and existing_bases is not None:
                existing_metadata["timestamp_bases"] = existing_bases
            return

        # Add or update the match for this device
        if existing_device_idx is not None:
            # Replace existing match for this device
            existing_matches[existing_device_idx] = match
        else:
            # Add new match
            existing_matches.append(match)

        self.lookup[eid_bytes] = existing_matches

        # Update metadata - use the match with smallest time_offset as primary
        best_match = min(existing_matches, key=lambda m: abs(m.time_offset))
        timestamp_bases = existing_bases or {window.time_basis}

        # Only update metadata if this match is the best (smallest offset)
        if best_match.device_id == match.device_id:
            meta: dict[str, Any] = {
                "variant": variant.value,
                "rotation_timestamp": window.timestamp,
                "time_offset": match.time_offset,
                "timestamp_basis": window.time_basis,
                "timestamp_bases": timestamp_bases,
                "advertisement_reversed": advertisement_reversed,
            }
            if flags_xor_mask is not None:
                meta["flags_xor_mask"] = flags_xor_mask
            self.metadata[eid_bytes] = meta
        elif existing_metadata is not None and existing_bases is not None:
            existing_metadata["timestamp_bases"] = existing_bases

        if __debug__:
            assert set(self.lookup.keys()).issuperset(self.metadata.keys()), (
                "metadata keys diverged after registration"
            )

    def finalize(
        self,
    ) -> tuple[dict[bytes, list[EIDMatch]], dict[bytes, dict[str, Any]]]:
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


def _normalize_anchor_basis(value: object) -> str | None:
    """Return a valid anchor basis or ``None``."""

    if not isinstance(value, str):
        return None
    basis = value.strip()
    return basis if basis in VALID_ANCHOR_BASES else None


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
    drift_offset: int = (
        0  # Tracked drift in seconds (positive = ahead, negative = behind)
    )
    last_seen_at: int | None = None  # Last successful EID match timestamp

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
            "drift_offset": self.drift_offset,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EIDGenerationLock:
        """Deserialize a stored lock."""

        variant = payload.get("variant") or ""
        advertisement_reversed = bool(payload.get("advertisement_reversed") or False)
        rotation_timestamp = payload.get("rotation_timestamp")
        rotation_ts = (
            int(rotation_timestamp)
            if isinstance(rotation_timestamp, int)
            and not isinstance(rotation_timestamp, bool)
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
            drift_offset=int(payload.get("drift_offset") or 0),
            last_seen_at=int(payload["last_seen_at"])
            if payload.get("last_seen_at") is not None
            else None,
        )


def _normalize_counter_candidate(candidate_value: object, *, basis: str) -> int | None:
    """Return a sane u32 counter candidate or ``None`` when unusable.

    Rejects values that are:
    - Not integers
    - Booleans (which are technically int subclass in Python)
    - Negative or zero (handles phones with pair_date=0 that lack deviceRegistration)
    """
    if (
        not isinstance(candidate_value, int)
        or isinstance(candidate_value, bool)
        or candidate_value <= 0
    ):
        return None

    max_millis = FHNA_COUNTER_MASK
    candidate_seconds = candidate_value // 1000
    if (
        candidate_value > max_millis
        and candidate_value % 1000 == 0
        and candidate_seconds > 0
    ):
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
    """Resolver that precalculates rotating EIDs for known trackers.

    Supports shared devices: when the same physical tracker is shared between
    accounts, all accounts receive EID match updates via resolve_eid_all().
    """

    hass: HomeAssistant
    _lookup: dict[bytes, list[EIDMatch]] = field(init=False, default_factory=dict)
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(
        init=False, default_factory=dict
    )
    _known_offsets: dict[tuple[str, str], int] = field(init=False, default_factory=dict)
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
    _truncated_frame_log_at: dict[tuple[int, int], float] = field(
        init=False, default_factory=dict
    )
    _store: Store[list[dict[str, Any]] | None] = field(init=False)
    _unsub_interval: CALLBACK_TYPE | None = field(init=False, default=None)
    _unsub_alignment: CALLBACK_TYPE | None = field(init=False, default=None)
    _refresh_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _pending_refresh: bool = field(init=False, default=False)
    _load_task: asyncio.Task[None] | None = field(init=False, default=None)
    # Heuristic phone discovery state
    _learned_heuristic_params: dict[str, LearnedHeuristicParams] = field(
        init=False, default_factory=dict
    )
    _heuristic_miss_log_at: dict[str, float] = field(init=False, default_factory=dict)
    _flags_logged_devices: set[str] = field(init=False, default_factory=set)
    _ble_battery_state: dict[str, BLEBatteryState] = field(
        init=False, default_factory=dict
    )
    _ble_scan_info: dict[str, BLEScanInfo] = field(init=False, default_factory=dict)
    _cached_identities: list[DeviceIdentity] = field(init=False, default_factory=list)
    # Per-variant crypto memoization caches (AP-A). Declared as slots-backed
    # dataclass fields; stubs that bypass __init__ get them seeded by
    # _ensure_cache_defaults under its hasattr guard.
    _eid_memo: _BoundedLRUCache = field(
        init=False, default_factory=lambda: _BoundedLRUCache(_EID_MEMO_MAXSIZE)
    )
    _flags_mask_memo: _BoundedLRUCache = field(
        init=False,
        default_factory=lambda: _BoundedLRUCache(_EID_MASK_MEMO_MAXSIZE),
    )
    # On-loop heuristic phone-discovery memoization (AP-SWEEP). Same shape as the
    # two AP-A caches; absorbs the per-advertisement ``generate_heuristic_eid``
    # fan-out on the resolve hot path.
    _heuristic_memo: _BoundedLRUCache = field(
        init=False,
        default_factory=lambda: _BoundedLRUCache(_HEURISTIC_MEMO_MAXSIZE),
    )
    # Deterministic signature of the last successful build phase (AP-B1).
    # ``None`` means "no successful build yet", so the first refresh never
    # skips. Declared as a slots-backed dataclass field; stubs that bypass
    # __init__ get it seeded by _ensure_cache_defaults under its hasattr guard.
    _last_build_signature: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Set up caches and schedule the first rotation-aligned refresh."""

        self._ensure_cache_defaults()
        self._store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
        load_coro = self._async_load_locks()
        self._load_task = self.hass.async_create_task(load_coro)
        if self._load_task is None or not isinstance(self._load_task, asyncio.Task):
            # Task creation returned None or non-Task (e.g., in tests with mocks)
            # Close the coroutine to prevent "coroutine never awaited" warnings
            try:
                load_coro.close()
            except Exception:  # pragma: no cover - defensive close
                pass
            self._load_task = None
        self._start_alignment_timer()

    def _ensure_cache_defaults(self) -> None:  # noqa: PLR0912
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
        if not hasattr(self, "_truncated_frame_log_at"):
            self._truncated_frame_log_at = {}
        # Heuristic phone discovery state
        if not hasattr(self, "_learned_heuristic_params"):
            self._learned_heuristic_params = {}
        if not hasattr(self, "_heuristic_miss_log_at"):
            self._heuristic_miss_log_at = {}
        if not hasattr(self, "_flags_logged_devices"):
            self._flags_logged_devices = set()
        if not hasattr(self, "_ble_battery_state"):
            self._ble_battery_state = {}
        if not hasattr(self, "_ble_scan_info"):
            self._ble_scan_info = {}
        if not hasattr(self, "_cached_identities"):
            self._cached_identities = []
        # Per-variant crypto memoization caches (AP-A). Initialized here under
        # the same hasattr guard as the _known_* defaults so stubs that bypass
        # __init__ (and call _ensure_cache_defaults directly) get them too.
        if not hasattr(self, "_eid_memo"):
            self._eid_memo = _BoundedLRUCache(_EID_MEMO_MAXSIZE)
        if not hasattr(self, "_flags_mask_memo"):
            self._flags_mask_memo = _BoundedLRUCache(_EID_MASK_MEMO_MAXSIZE)
        # On-loop heuristic discovery memoization (AP-SWEEP), same guard.
        if not hasattr(self, "_heuristic_memo"):
            self._heuristic_memo = _BoundedLRUCache(_HEURISTIC_MEMO_MAXSIZE)
        # Build-phase skip-guard signature (AP-B1). Same hasattr guard as the
        # AP-A memo caches so stubs that bypass __init__ get the default too.
        if not hasattr(self, "_last_build_signature"):
            self._last_build_signature = None

    def _clear_lock_state(self, device_id: str) -> bool:
        """Remove all cached state associated with a device lock."""

        removed = False
        if self._purge_known_offsets_for_device(device_id):
            removed = True

        for attr in (
            "_locks",
            "_persisted_locks",
            "_known_advertisement_reversed",
            "_known_timebases",
            "_last_lock_confirmation",
        ):
            mapping = getattr(self, attr, None)
            if isinstance(mapping, dict) and device_id in mapping:
                mapping.pop(device_id, None)
                removed = True
        return removed

    def _purge_known_offsets_for_device(self, device_id: str) -> bool:
        """Remove any stored known offsets for the device across bases."""

        mapping = getattr(self, "_known_offsets", None)
        if not isinstance(mapping, dict) or not mapping:
            return False

        to_delete: list[tuple[str, str]] = []
        for key in list(mapping.keys()):
            if (
                isinstance(key, tuple)
                and len(key) == KNOWN_OFFSET_KEY_LENGTH
                and isinstance(key[0], str)
                and key[0] == device_id
            ):
                to_delete.append(key)
        for key in to_delete:
            mapping.pop(key, None)

        return bool(to_delete)

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
            # Restore drift offset to _known_offsets for EID calculation
            if lock.drift_offset != 0 and lock.time_basis:
                self._known_offsets[(lock.device_id, lock.time_basis)] = (
                    lock.drift_offset
                )
                self._known_timebases[lock.device_id] = lock.time_basis

    async def _async_save_locks(self) -> None:
        """Persist locks to storage."""

        await self._store.async_save([lock.to_dict() for lock in self._locks.values()])

    def _schedule_lock_save(self) -> None:
        """Schedule persistence of EID locks."""

        lock_save: Coroutine[Any, Any, None] | None = None
        try:
            task_name = "googlefindmy_eid_resolver_save"
            create_task = getattr(
                self.hass, "async_create_background_task", None
            ) or getattr(self.hass, "async_create_task")
            if create_task is None:
                raise AttributeError("hass is missing async_create_task helper")
            lock_save = self._async_save_locks()
            try:
                scheduled = create_task(lock_save, name=task_name)
            except TypeError:
                scheduled = create_task(lock_save)
            if scheduled is None:
                asyncio.create_task(lock_save)
                lock_save = None  # Ownership transferred to asyncio.create_task
                _LOGGER.warning(
                    "EID lock save was not scheduled (task helper returned None)"
                )
            elif asyncio.iscoroutine(scheduled):
                asyncio.create_task(scheduled)
                lock_save = None  # Ownership transferred
            elif not isinstance(scheduled, asyncio.Task):
                try:
                    lock_save.close()
                except Exception:  # pragma: no cover - defensive close
                    pass
                lock_save = None
                _LOGGER.warning(
                    "EID lock save task helper returned non-awaitable %s; coroutine closed",
                    type(scheduled).__name__,
                )
            else:
                lock_save = None  # Ownership transferred to task
        except Exception as err:  # pragma: no cover - defensive log
            _LOGGER.error("Failed to schedule EID lock persistence: %s", err)
            if lock_save is not None:
                try:
                    lock_save.close()
                except Exception:  # pragma: no cover - defensive close
                    pass

    def _purge_stale_locks(self, *, now: int) -> None:  # noqa: PLR0912
        """Drop expired generation locks to keep cache fresh."""

        expired_by_confirmation: dict[str, int | None] = {}
        expired_by_created: list[str] = []
        for device_id, lock in list(self._locks.items()):
            last_confirmation = self._last_lock_confirmation.get(device_id)
            if isinstance(last_confirmation, int) and not isinstance(
                last_confirmation, bool
            ):
                confirmation_age: int | None = now - last_confirmation
                last_activity = last_confirmation
            else:
                confirmation_age = None
                last_activity = lock.created_at
            if confirmation_age is None:
                if now - lock.created_at > LOCK_CONFIRMATION_TTL_SECONDS:
                    expired_by_confirmation[device_id] = None
                    continue
            elif confirmation_age > LOCK_CONFIRMATION_TTL_SECONDS:
                expired_by_confirmation[device_id] = confirmation_age
                continue

            if now - last_activity > LOCK_TTL_SECONDS:
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
        """Forget cached offsets/locks for a given device registry ID."""

        if not registry_id:
            return

        self._ensure_cache_defaults()
        removed = self._clear_lock_state(registry_id)
        removed = self._purge_known_offsets_for_device(registry_id) or removed
        removed = (
            self._known_advertisement_reversed.pop(registry_id, None) is not None
            or removed
        )
        removed = self._known_timebases.pop(registry_id, None) is not None or removed
        removed = (
            self._last_lock_confirmation.pop(registry_id, None) is not None or removed
        )

        if removed:
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
            self._pending_refresh = True
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
            _LOGGER.debug(
                "Skipping work item for %s: config_entry_id=%s, identity_key=%s, registry_id=%s",
                identity.canonical_id,
                bool(identity.config_entry_id),
                identity.identity_key is not None,
                bool(identity.registry_id),
            )
            return None

        clean_canonical_id = identity.canonical_id
        if ":" in clean_canonical_id:
            clean_canonical_id = clean_canonical_id.split(":")[-1]

        # FIX: Handle both bytes and hex string formats for identity_key
        key_bytes = _normalize_encrypted_blob(identity.identity_key)
        if key_bytes is None:
            return None

        lock = self._locks.get(identity.registry_id)
        locked_variant: EidVariant | None = None
        rotation_ts: int | None = None

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
                valid_hint = (
                    lock_time_basis
                    if lock_time_basis
                    in {
                        "unix",
                        "pair_date",
                        "secrets_creation_date",
                    }
                    else None
                )
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

        lock_windows = self._compute_lock_windows(
            work_item, now_unix=now_unix, params=params
        )
        fallback_discovery = bool(lock_windows)
        min_relative_window = (
            LOCK_FALLBACK_RELATIVE_WINDOW_SIZE
            if fallback_discovery
            else params.min_relative_window
        )
        relative_windows, invalid_hint = self._compute_relative_windows(
            work_item,
            now_unix=now_unix,
            params=params,
            min_relative_window=min_relative_window,
            allow_unix_basis=not fallback_discovery,
        )
        if lock_windows:
            lock_windows.extend(relative_windows)
            return self._dedupe_windows(lock_windows), invalid_hint

        return self._dedupe_windows(relative_windows), invalid_hint

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
        expected_counter = work_item.rotation_ts + (
            periods_elapsed * params.rotation_period
        )
        for step in range(-LOCK_TRACKING_WINDOW_STEPS, LOCK_TRACKING_WINDOW_STEPS + 1):
            offset_periods = periods_elapsed + step
            window_ts = work_item.rotation_ts + (
                offset_periods * params.rotation_period
            )
            if window_ts < 0:
                continue
            semantic_offset = window_ts - expected_counter
            counter_windows.append(
                WindowCandidate(
                    timestamp=window_ts,
                    semantic_offset=semantic_offset,
                    time_basis="lock_tracking",
                    candidate_value=expected_counter,
                )
            )

        if not counter_windows:
            return []

        return [
            WindowSpec(
                time_basis="lock_tracking",
                candidate_value=expected_counter,
                windows=tuple(counter_windows),
            )
        ]

    def _compute_relative_windows(  # noqa: PLR0912, PLR0915
        self,
        work_item: WorkItem,
        *,
        now_unix: int,
        params: RotationParams,
        min_relative_window: int,
        allow_unix_basis: bool,
    ) -> tuple[list[WindowSpec], bool]:
        """Return relative/absolute windows and whether the basis hint was invalid."""

        counter_windows: list[WindowSpec] = []
        counter_bases: tuple[tuple[str, str], ...] = (
            ("unix", "unix"),
            ("pair_date", "pair_date"),
            ("secrets_creation_date", "secrets_creation_date"),
        )
        if not ENABLE_ABSOLUTE_UNIX_BASIS:
            counter_bases = tuple(
                (basis, label) for basis, label in counter_bases if basis != "unix"
            )
        if not allow_unix_basis:
            counter_bases = tuple(
                (basis, label) for basis, label in counter_bases if basis != "unix"
            )
        anchor_candidates: list[int] = []
        if (
            isinstance(work_item.identity.pair_date, int)
            and work_item.identity.pair_date > 0
        ):
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
            tuple(
                (basis, label) for basis, label in counter_bases if basis == known_basis
            )
            if known_basis
            else counter_bases
        )

        for basis, label in filtered_bases:
            candidate_value: int | None = None
            reference_ts = now_unix
            total_window: int = 0

            if basis == "unix":
                if not ENABLE_ABSOLUTE_UNIX_BASIS:
                    continue
                candidate_value = now_unix
                reference_ts = now_unix
                try:
                    tz_offset = dt_util.now().utcoffset()
                    tz_windows = (
                        int(
                            math.ceil(
                                abs(tz_offset.total_seconds()) / params.rotation_period
                            )
                        )
                        if tz_offset
                        else 0
                    )
                except Exception:
                    tz_windows = 0
                total_window = max(
                    params.min_unix_window, 3 + drift_windows + tz_windows
                )
            else:
                raw_anchor = getattr(work_item.identity, basis, None)
                anchor_value = _normalize_counter_candidate(raw_anchor, basis=basis)
                if anchor_value is None:
                    _LOGGER.debug(
                        "Skipping basis %s for %s: raw_anchor=%s (normalized=None)",
                        basis,
                        work_item.registry_id,
                        raw_anchor,
                    )
                    continue
                candidate_value = now_unix - anchor_value
                reference_ts = candidate_value

                safety_buffer = 2
                known_offset = self._known_offsets.get((work_item.registry_id, basis))
                if known_offset is not None:
                    max_offset = 5 * params.rotation_period
                    if abs(known_offset) <= max_offset:
                        candidate_value = candidate_value + known_offset
                        reference_ts = candidate_value
                        total_window = min(3, params.max_window)
                    else:
                        _LOGGER.debug(
                            "Ignoring implausible known_offset=%s for %s (basis=%s); falling back to discovery window",
                            known_offset,
                            work_item.registry_id,
                            basis,
                        )
                        known_offset = None
                if known_offset is None:
                    total_window = min(
                        max(min_relative_window, 3 + drift_windows + safety_buffer),
                        params.max_window,
                    )

            normalized = _normalize_counter_candidate(candidate_value, basis=basis)
            if normalized is None:
                _LOGGER.debug(
                    "Skipping basis %s for %s: candidate_value=%s (counter normalized=None)",
                    basis,
                    work_item.registry_id,
                    candidate_value,
                )
                continue
            reference_ts = normalized

            window_candidates: list[WindowCandidate] = []
            for ts in iter_rotation_windows(
                normalized,
                rotation_period=params.rotation_period,
                window_range=range(-total_window, total_window + 1),
                include_neighbors=False,
            ):
                if ts < 0:
                    continue
                semantic_offset = ts - reference_ts
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

    @staticmethod
    def _dedupe_windows(specs: list[WindowSpec]) -> list[WindowSpec]:
        """Remove duplicate windows by (basis, timestamp) while preserving order."""

        basis_order: list[str] = []
        basis_windows: dict[str, list[WindowCandidate]] = {}
        basis_candidate_value: dict[str, int] = {}
        key_index: dict[tuple[str, int], int] = {}

        for spec in specs:
            if spec.time_basis not in basis_order:
                basis_order.append(spec.time_basis)
                basis_windows.setdefault(spec.time_basis, [])
            for window in spec.windows:
                key = (spec.time_basis, window.timestamp)
                if key not in key_index:
                    key_index[key] = len(basis_windows[spec.time_basis])
                    basis_windows[spec.time_basis].append(window)
                    basis_candidate_value.setdefault(
                        spec.time_basis, window.candidate_value
                    )
                    continue

                idx = key_index[key]
                current = basis_windows[spec.time_basis][idx]
                if abs(window.semantic_offset) < abs(current.semantic_offset):
                    basis_windows[spec.time_basis][idx] = window
                    basis_candidate_value[spec.time_basis] = window.candidate_value

        return [
            WindowSpec(
                time_basis=basis,
                candidate_value=basis_candidate_value[basis],
                windows=tuple(basis_windows[basis]),
            )
            for basis in basis_order
            if basis_windows.get(basis)
        ]

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

    def _build_signature(
        self,
        work_items: list[WorkItem],
        *,
        now_unix: int,
        params: RotationParams,
    ) -> str:
        """Return a deterministic signature of the active-device build inputs.

        The signature is the skip-guard key for the expensive build phase: if it
        is unchanged, re-running the build would reproduce a byte-identical
        lookup table, so it can be safely skipped.

        Correctness model (Variant B -- fold the computed specs, not a time
        formula). The expensive, skipped part of the build is the per-variant
        crypto in ``_generate_eids_from_spec`` / ``_compute_flags_xor_mask``. Its
        only inputs are the ``VariantSpec`` items the build loop fans out, which
        are a deterministic, *pure* function of each work item and ``now_unix``:
        ``_compute_time_windows`` (-> ``_compute_lock_windows`` /
        ``_compute_relative_windows`` / ``_dedupe_windows``) reads the learned
        ``_known_offsets`` snapshot but mutates no resolver state, and
        ``_compute_variants`` is pure. The signature therefore replays exactly
        that window/variant derivation and folds its *content* in: if the spec
        set is identical the crypto output is byte-identical, so the skip is
        correct by construction.

        Folding the realized window timestamps (rather than a ``now_unix //
        period`` term) closes the phase-relative drift gap: the lock-tracking
        index is ``(now_unix - created_at) // period`` and the relative index is
        ``(now_unix - anchor) // period``, so a non-period-aligned anchor rolls
        a drift window at a different ``now_unix`` than the absolute rollover --
        two clocks could share the old absolute-counter signature yet need
        different window sets. By hashing the windows the build actually
        produces, a missing or added drift window always changes the digest.

        Per work item the digest additionally folds the identity fields that
        reach the build output but never the window math -- ``registry_id`` /
        ``canonical_id`` / ``config_entry_id`` are emitted verbatim into every
        ``EIDMatch``. Sorting makes the digest independent of iteration order.
        """

        hasher = hashlib.blake2b(digest_size=32)

        def _item_key(item: WorkItem) -> tuple[str, ...]:
            # Identity fields that flow straight into ``EIDMatch`` output but are
            # invisible to the window/variant derivation below.
            fields: list[str] = [
                item.registry_id,
                item.canonical_id,
                item.config_entry_id,
                item.key_bytes.hex(),
                "" if item.locked_variant is None else item.locked_variant.value,
            ]
            # Replay the pure window/variant derivation and fold the realized
            # specs. ``_compute_time_windows`` is read-only with respect to
            # ``self`` (it only reads the ``_known_offsets`` snapshot); see the
            # AP-C offload contract. The realized spec set fully determines the
            # skipped crypto output, so identical specs => identical lookup.
            windows, invalid_hint = self._compute_time_windows(
                item, now_unix=now_unix, params=params
            )
            fields.append("1" if invalid_hint else "0")
            for window in windows:
                for variant_spec in self._compute_variants(item, window):
                    fields.append(
                        "|".join(
                            (
                                variant_spec.variant.value,
                                str(int(variant_spec.include_reverse)),
                                window.time_basis,
                                str(variant_spec.window.timestamp),
                                str(variant_spec.window.semantic_offset),
                            )
                        )
                    )
            return tuple(fields)

        for key in sorted(_item_key(item) for item in work_items):
            hasher.update(b"W")
            for field_value in key:
                hasher.update(field_value.encode())
                hasher.update(b"\x00")

        return hasher.hexdigest()

    async def _refresh_cache(self) -> None:
        """Rebuild the EID lookup table for all active devices."""

        self._ensure_cache_defaults()
        if self._load_task is not None:
            await asyncio.shield(self._load_task)

        now_unix = int(time.time())
        self._purge_stale_locks(now=now_unix)
        rotation_params = self._build_rotation_params()

        identities = await self._collect_device_secrets()
        # Cache identities for heuristic phone discovery
        self._cached_identities = list(identities)
        _LOGGER.debug("Refresh start: %d identities discovered", len(identities))
        work_items = self._collect_work_items(identities, now_unix=now_unix)
        _LOGGER.debug("Refresh stage: collected %d work items", len(work_items))

        # Skip-guard (AP-B1): the build phase below is a pure function of the
        # work items and the per-period rotation counters. When the signature is
        # unchanged, rebuilding would reproduce a byte-identical lookup table, so
        # we keep the existing one and skip the expensive CacheBuilder fan-out.
        # The cheap mandatory side effects above (_purge_stale_locks,
        # _cached_identities, _collect_device_secrets) and the invalid-hint
        # bookkeeping in _collect_work_items still run on every trigger. The
        # build-loop's own ``_known_timebases.pop`` (invalid-hint) is safe to
        # skip: ``invalid_hint`` is a pure function of the work item's
        # ``basis_hint`` (plus its lock-derived window basis), so an unchanged
        # signature means the hint state is unchanged; any newly invalid hint
        # changes ``basis_hint`` (read back from _known_timebases), which changes
        # the signature and forces a rebuild that runs the pop.
        build_signature = self._build_signature(
            work_items, now_unix=now_unix, params=rotation_params
        )
        if self._last_build_signature == build_signature:
            _LOGGER.debug(
                "Refresh stage: build signature unchanged, skipping rebuild "
                "(%d work items, %d cached EIDs)",
                len(work_items),
                len(self._lookup),
            )
            return

        # Offload the pure synchronous build to a worker thread (AP-C). The
        # build block touches ``self`` only through the two thread-safe AP-A
        # memo caches (``_eid_memo``/``_flags_mask_memo``), whose locks guard
        # the dict access only, and reads the immutable learned ``_known_offsets``
        # snapshot via ``_compute_time_windows``. Every genuine ``self.*``
        # mutation stays on the loop: the invalid-hint ``_known_timebases.pop``
        # below (driven by the registry ids returned from the build) and the
        # ``_lookup``/``_lookup_metadata`` assignment after the executor returns.
        # The cooperative ``asyncio.sleep(0)`` yield is gone because no event
        # loop exists inside the worker thread; running the whole build off-loop
        # keeps the loop reactive without per-item yields.
        lookup, lookup_metadata, invalid_hint_ids = (
            await self.hass.async_add_executor_job(
                self._build_lookup_sync,
                work_items,
                now_unix,
                rotation_params,
            )
        )

        # Loop-side ``self.*`` mutations (single-threaded, no executor in flight).
        for registry_id in invalid_hint_ids:
            self._known_timebases.pop(registry_id, None)
        self._lookup, self._lookup_metadata = lookup, lookup_metadata
        # Record the signature only after a successful build so a failure leaves
        # the guard cleared and the next trigger rebuilds (AP-B1).
        self._last_build_signature = build_signature
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

    def _build_lookup_sync(
        self,
        work_items: list[WorkItem],
        now_unix: int,
        rotation_params: RotationParams,
    ) -> tuple[
        dict[bytes, list[EIDMatch]],
        dict[bytes, dict[str, Any]],
        list[str],
    ]:
        """Build the EID lookup table off the event loop (AP-C).

        Pure synchronous build over the loop-side inputs ``work_items`` /
        ``now_unix`` / ``rotation_params``. Designed to run inside a worker
        thread via ``hass.async_add_executor_job``: it performs no ``await``,
        touches no event loop, and mutates no resolver state except the two
        thread-safe AP-A memo caches (``_eid_memo`` / ``_flags_mask_memo``),
        which serialize only on the bounded dict access. ``_compute_time_windows``
        and its callees read the learned ``_known_offsets`` snapshot but never
        write it, and the ``CacheBuilder`` is thread-local to this call.

        Returns the finalized ``(lookup, metadata)`` tables plus the list of
        registry ids whose basis hint was invalid; the caller pops their
        ``_known_timebases`` entries on the loop, where it is safe to mutate.
        """

        builder = CacheBuilder()
        invalid_hint_ids: list[str] = []

        for work_item in work_items:
            windows, invalid_hint = self._compute_time_windows(
                work_item, now_unix=now_unix, params=rotation_params
            )
            if invalid_hint:
                invalid_hint_ids.append(work_item.registry_id)
            _LOGGER.debug(
                "Refresh stage: %s produced %d window groups",
                work_item.registry_id,
                len(windows),
            )
            for window in windows:
                variants = self._compute_variants(work_item, window)
                for variant_spec in variants:
                    xor_mask: int | None = None
                    curve_len, curve_ord = _VARIANT_CURVE_PARAMS.get(
                        variant_spec.variant, (LEGACY_EID_LENGTH, None)
                    )
                    try:
                        xor_mask = self._compute_flags_xor_mask(
                            variant_spec.key_bytes,
                            variant_spec.window.timestamp,
                            curve_byte_len=curve_len,
                            curve_order=curve_ord,
                        )
                    except Exception:  # noqa: BLE001
                        pass
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
                            flags_xor_mask=xor_mask,
                        )

        lookup, lookup_metadata = builder.finalize()
        return lookup, lookup_metadata, invalid_hint_ids

    def _normalize_variant_value(self, raw: object, *, eid_len: int) -> str:
        """Return a valid `EidVariant` value string for persistence."""

        try:
            return EidVariant(str(raw)).value
        except Exception:
            pass

        if isinstance(raw, str):
            stripped = raw.strip()
            if stripped:
                try:
                    return EidVariant(stripped).value
                except Exception:
                    if stripped.isdigit():
                        try:
                            return EidVariant(str(int(stripped))).value
                        except Exception:
                            pass
        elif isinstance(raw, int) and not isinstance(raw, bool):
            try:
                return EidVariant(str(raw)).value
            except Exception:
                pass

        inferred = self._infer_variant_from_length(eid_len)
        try:
            return EidVariant(inferred).value
        except Exception:
            return inferred

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
        """Generate an EID for a specific profile.

        Memoized per resolver instance on ``(key_bytes, time_counter,
        variant)``. The derivation is pure, so a hit returns the byte-identical
        EID without recomputation. Cache invalidation is implicit: key rotation
        changes ``time_counter`` and re-keying changes ``key_bytes``, both of
        which produce a fresh key and let stale entries age out via the LRU.
        """

        cache_key = (key_bytes, time_counter, variant)
        cached = self._eid_memo.get(cache_key)
        if cached is not None:
            return cast(bytes, cached)

        # Compute outside the cache lock so parallel workers (AP-C) are not
        # serialized on the crypto call.
        eid_bytes = generate_eid_variant(
            key_bytes,
            time_counter,
            variant,
            strict=False,
        )
        self._eid_memo.put(cache_key, eid_bytes)
        return eid_bytes

    def _compute_flags_xor_mask(
        self,
        key_bytes: bytes,
        time_counter: int,
        *,
        curve_byte_len: int,
        curve_order: int | None,
    ) -> int:
        """Memoized wrapper around ``compute_flags_xor_mask``.

        Keyed per resolver instance on ``(key_bytes, time_counter,
        curve_byte_len, curve_order)``. The mask is a pure function of these
        inputs, so a hit returns the identical integer without recomputation.
        Invalidation is implicit via key rotation, identical to
        ``_generate_variant``.
        """

        cache_key = (key_bytes, time_counter, curve_byte_len, curve_order)
        cached = self._flags_mask_memo.get(cache_key)
        if cached is not None:
            return cast(int, cached)

        # Compute outside the cache lock so parallel workers (AP-C) are not
        # serialized on the crypto call.
        mask = compute_flags_xor_mask(
            key_bytes,
            time_counter,
            curve_byte_len=curve_byte_len,
            curve_order=curve_order,
        )
        self._flags_mask_memo.put(cache_key, mask)
        return mask

    def _generate_heuristic_eid(  # noqa: PLR0913
        self,
        key_bytes: bytes,
        now_unix: int,
        *,
        rotation_period: int,
        basis: HeuristicBasis,
        variant: EidVariant,
        anchor: int | None,
        drift_offsets: tuple[int, ...] = (-1, 0, 1),
    ) -> list[HeuristicEidResult]:
        """Memoized wrapper around ``generate_heuristic_eid`` (AP-SWEEP).

        ``generate_heuristic_eid`` runs on the event loop for every cache-miss
        FMDN advertisement (``_resolve_eid_internal`` -> ``_heuristic_resolve``).
        Its output list is a pure function of the rotation-aligned time window
        and the hypothesis parameters, so consecutive advertisements from the
        same nearby phone within one window recompute identical lists. This
        wrapper turns that repeat into a dict lookup.

        The cache key carries every parameter that influences the output --
        ``key_bytes``, the rotation-aligned window start, ``rotation_period``,
        ``basis``, ``variant`` and ``anchor`` -- so a stale value can never be
        served: a window-boundary rounding difference can at worst miss the
        cache and recompute (still byte-exact), never collide across distinct
        inputs. The window start mirrors ``_align_to_rotation_flexible``'s
        ``(t // period) * period`` over the same time value the crypto aligns
        (``now_unix`` for ABSOLUTE, ``now_unix - anchor`` for RELATIVE).
        Invalidation is implicit: a new window or re-keying yields a fresh key
        and stale entries age out via the LRU. ``drift_offsets`` defaults to the
        same value the underlying generator uses; it is folded into the key (and
        forwarded explicitly rather than relying on the callee default) so a
        future caller that varies it can never be served a stale entry. The
        added key cardinality is negligible (one constant tuple in practice).
        """

        if basis == HeuristicBasis.RELATIVE and anchor is not None and anchor > 0:
            aligned_source = max(now_unix - anchor, 0)
        else:
            aligned_source = now_unix
        window_start = (
            (aligned_source // rotation_period) * rotation_period
            if rotation_period > 0
            else aligned_source
        )

        cache_key = (
            key_bytes,
            window_start,
            rotation_period,
            basis,
            variant,
            anchor,
            drift_offsets,
        )
        cached = self._heuristic_memo.get(cache_key)
        if cached is not None:
            return cast(list[HeuristicEidResult], cached)

        results = generate_heuristic_eid(
            key_bytes,
            now_unix,
            rotation_period=rotation_period,
            basis=basis,
            variant=variant,
            anchor=anchor,
            drift_offsets=drift_offsets,
        )
        self._heuristic_memo.put(cache_key, results)
        return results

    async def _collect_device_secrets(self) -> list[DeviceIdentity]:
        """Retrieve active tracker identities from all loaded coordinators."""
        bucket = self.hass.data.get(DOMAIN)
        if not isinstance(bucket, dict):
            _LOGGER.warning(
                "EID pipeline disabled: hass.data[%s] is not a dict (type=%s). "
                "This may indicate the integration was not properly initialized.",
                DOMAIN,
                type(bucket).__name__,
            )
            return []

        entries = bucket.get("entries")
        if not isinstance(entries, dict):
            _LOGGER.warning(
                "EID pipeline disabled: hass.data[%s]['entries'] is not a dict (type=%s). "
                "Falling back to empty identity list.",
                DOMAIN,
                type(entries).__name__ if entries is not None else "None",
            )
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
        # Lazy-load crypto exception for startup performance
        InvalidTag = get_invalid_tag_exception()

        self._ensure_cache_defaults()
        if cache is None:
            return None

        # FIX: Handle both bytes and hex string formats for encrypted_identity_key
        encrypted_identity_key = _normalize_encrypted_blob(
            identity.encrypted_identity_key
        )
        if encrypted_identity_key is None:
            return None

        owner_key_info = await self._resolve_owner_key(identity, cache=cache)
        if owner_key_info is None:
            return None

        key_sources: list[tuple[str, bytes]] = [("owner", owner_key_info.key)]
        try:
            _cached_user = await cache.get(username_string)
            shared_key = await async_get_shared_key(
                cache=cache,
                username=_cached_user if isinstance(_cached_user, str) else None,
            )
        except Exception:  # pragma: no cover - best-effort
            shared_key = None
        if shared_key is not None:
            key_sources.append(("shared", shared_key))
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
        # Lazy-load crypto classes for startup performance
        AESGCM = get_aesgcm_class()
        InvalidTag = get_invalid_tag_exception()

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
            _LOGGER.info(
                "Owner Key Version mismatch detected: Tracker requires V%s, "
                "Cache has V%s. Refreshing...",
                identity.owner_key_version,
                owner_key_info.version,
            )
            refreshed = await _fetch(force_refresh=True)
            if refreshed is not None:
                return refreshed

        return owner_key_info

    # =========================================================================
    # Heuristic Phone Discovery
    # =========================================================================
    # These methods implement a reactive heuristic check for Android phones
    # that don't follow the standard FMDN tracker protocol. When a packet
    # fails the normal cache lookup, these methods test alternative hypotheses
    # (different rotation periods and time bases) to find a match.
    # =========================================================================

    def _heuristic_resolve(
        self,
        candidates: list[bytes],
        *,
        now_unix: int,
    ) -> EIDMatch | None:
        """Attempt heuristic resolution for packets that missed the standard cache.

        This method tests multiple hypotheses about the EID generation parameters:
        - Rotation periods: 900s, 1024s, 3600s
        - Time bases: ABSOLUTE (now // period) vs RELATIVE ((now - anchor) // period)

        Args:
            candidates: EID byte candidates extracted from the BLE payload.
            now_unix: Current Unix timestamp in seconds.

        Returns:
            EIDMatch if a heuristic match is found, None otherwise.
        """
        if not ENABLE_HEURISTIC_PHONE_DISCOVERY:
            return None

        if not self._cached_identities:
            _LOGGER.debug("Heuristic resolve skipped: no cached identities")
            return None

        # Build a lookup set for fast candidate matching
        candidate_set = set(candidates)

        # First, check devices with learned parameters (fast path)
        for device_id, learned in self._learned_heuristic_params.items():
            match = self._heuristic_check_learned(
                device_id,
                learned,
                candidate_set,
                now_unix=now_unix,
            )
            if match is not None:
                return match

        # Fall back to full hypothesis testing (slow path)
        for identity in self._cached_identities:
            if identity.identity_key is None:
                continue

            match = self._heuristic_test_hypotheses(
                identity,
                candidate_set,
                now_unix=now_unix,
            )
            if match is not None:
                return match

        return None

    def _heuristic_check_learned(
        self,
        device_id: str,
        learned: LearnedHeuristicParams,
        candidate_set: set[bytes],
        *,
        now_unix: int,
    ) -> EIDMatch | None:
        """Check if a payload matches a device with previously learned parameters.

        This is the fast path for devices that have been successfully matched
        using heuristic parameters before.
        """
        # Find the identity for this device
        identity: DeviceIdentity | None = None
        for ident in self._cached_identities:
            if ident.registry_id == device_id:
                identity = ident
                break

        if identity is None or identity.identity_key is None:
            return None

        key_bytes = self._normalize_key_bytes(identity.identity_key)
        if key_bytes is None:
            return None

        anchor = self._get_best_anchor(identity)

        try:
            results = self._generate_heuristic_eid(
                key_bytes,
                now_unix,
                rotation_period=learned.rotation_period,
                basis=learned.basis,
                variant=learned.variant,
                anchor=anchor if learned.basis == HeuristicBasis.RELATIVE else None,
            )
        except Exception as exc:
            _LOGGER.debug(
                "Heuristic learned check failed for %s: %s",
                device_id,
                exc,
            )
            return None

        for result in results:
            if result.eid_bytes in candidate_set:
                # Update confirmation tracking
                learned.last_confirmed_at = now_unix
                learned.confirmation_count += 1

                _LOGGER.info(
                    "HEURISTIC HIT (learned): device=%s period=%ds basis=%s "
                    "variant=%s drift=%d reversed=%s",
                    device_id,
                    learned.rotation_period,
                    learned.basis.value,
                    learned.variant.value,
                    result.drift_offset,
                    result.is_reversed,
                )

                return EIDMatch(
                    device_id=device_id,
                    config_entry_id=str(identity.config_entry_id or ""),
                    canonical_id=identity.canonical_id,
                    time_offset=result.drift_offset * learned.rotation_period,
                    is_reversed=result.is_reversed,
                )

        return None

    def _heuristic_test_hypotheses(
        self,
        identity: DeviceIdentity,
        candidate_set: set[bytes],
        *,
        now_unix: int,
    ) -> EIDMatch | None:
        """Test all hypotheses against a device identity.

        This is the slow path that tests all combinations of rotation periods,
        time bases, and EID variants.
        """
        key_bytes = self._normalize_key_bytes(identity.identity_key)
        if key_bytes is None:
            return None

        anchor = self._get_best_anchor(identity)
        device_id = identity.registry_id

        for rotation_period, basis in HEURISTIC_HYPOTHESES:
            # Skip RELATIVE basis if no valid anchor
            if basis == HeuristicBasis.RELATIVE and (anchor is None or anchor <= 0):
                continue

            for variant in HEURISTIC_VARIANTS_TO_TEST:
                try:
                    results = self._generate_heuristic_eid(
                        key_bytes,
                        now_unix,
                        rotation_period=rotation_period,
                        basis=basis,
                        variant=variant,
                        anchor=anchor if basis == HeuristicBasis.RELATIVE else None,
                    )
                except Exception as exc:
                    _LOGGER.debug(
                        "Heuristic generation failed: device=%s period=%d basis=%s variant=%s: %s",
                        device_id,
                        rotation_period,
                        basis.value,
                        variant.value,
                        exc,
                    )
                    continue

                for result in results:
                    if result.eid_bytes in candidate_set:
                        # Found a match! Store the learned parameters
                        self._learned_heuristic_params[device_id] = (
                            LearnedHeuristicParams(
                                device_id=device_id,
                                canonical_id=identity.canonical_id,
                                rotation_period=rotation_period,
                                basis=basis,
                                variant=variant,
                                discovered_at=now_unix,
                                last_confirmed_at=now_unix,
                                confirmation_count=1,
                            )
                        )

                        _LOGGER.info(
                            "HEURISTIC HIT (discovery): device=%s period=%ds basis=%s "
                            "variant=%s drift=%d reversed=%s. Caching parameters.",
                            device_id,
                            rotation_period,
                            basis.value,
                            variant.value,
                            result.drift_offset,
                            result.is_reversed,
                        )

                        return EIDMatch(
                            device_id=device_id,
                            config_entry_id=str(identity.config_entry_id or ""),
                            canonical_id=identity.canonical_id,
                            time_offset=result.drift_offset * rotation_period,
                            is_reversed=result.is_reversed,
                        )

        return None

    def _normalize_key_bytes(self, key: Any) -> bytes | None:
        """Normalize an identity key to bytes."""
        if isinstance(key, (bytes, bytearray)):
            return bytes(key)
        if isinstance(key, str):
            try:
                return bytes.fromhex(key)
            except ValueError:
                return None
        return None

    def _get_best_anchor(self, identity: DeviceIdentity) -> int | None:
        """Get the best available time anchor for an identity."""
        anchors: list[int] = []
        if isinstance(identity.pair_date, int) and identity.pair_date > 0:
            anchors.append(identity.pair_date)
        if (
            isinstance(identity.secrets_creation_date, int)
            and identity.secrets_creation_date > 0
        ):
            anchors.append(identity.secrets_creation_date)
        return max(anchors) if anchors else None

    def _should_log_heuristic_miss(self, raw_prefix: str) -> bool:
        """Check if a heuristic miss should be logged (rate limited)."""
        now = time.time()
        last_log = self._heuristic_miss_log_at.get(raw_prefix)
        if last_log is not None and now - last_log < HEURISTIC_LOG_INTERVAL_SECONDS:
            return False
        self._heuristic_miss_log_at[raw_prefix] = now
        return True

    def _update_match_state(  # noqa: PLR0913
        self,
        match: EIDMatch,
        *,
        metadata: dict[str, Any],
        candidate: bytes,
        observed_frame: int | None,
        candidate_prefix: str,
        raw_prefix: str,
        now: int,
    ) -> None:
        """Update internal state (locks, offsets, etc.) for a single match."""
        self._last_lock_confirmation[match.device_id] = now
        previous_basis = _normalize_anchor_basis(
            self._known_timebases.get(match.device_id)
        )
        raw_time_basis = metadata.get("timestamp_basis")
        anchor_basis = _normalize_anchor_basis(raw_time_basis)
        basis_explicitly_invalid = raw_time_basis is not None and anchor_basis is None
        if anchor_basis is None:
            if raw_time_basis is None:
                anchor_basis = previous_basis
        elif raw_time_basis != anchor_basis:
            self._known_timebases.pop(match.device_id, None)
        if anchor_basis is None and not basis_explicitly_invalid:
            self._known_timebases.pop(match.device_id, None)

        existing_lock = self._locks.get(match.device_id)
        if existing_lock is None:
            variant_value = self._normalize_variant_value(
                metadata.get("variant"),
                eid_len=len(candidate),
            )
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
                time_basis=anchor_basis,
                created_at=now,
                drift_offset=match.time_offset,
                last_seen_at=now,
            )
            self._locks[match.device_id] = lock
            self._persisted_locks[match.device_id] = lock
            self._schedule_lock_save()
        else:
            # Update drift tracking on existing lock
            drift_changed = existing_lock.drift_offset != match.time_offset
            existing_lock.drift_offset = match.time_offset
            existing_lock.last_seen_at = now
            if drift_changed and match.time_offset != 0:
                _LOGGER.debug(
                    "Drift update for %s: offset=%d (using %s window)",
                    match.device_id,
                    match.time_offset,
                    "+1/+2" if match.time_offset > 0 else "-1/-2",
                )
            self._schedule_lock_save()

        self._known_advertisement_reversed[match.device_id] = match.is_reversed

        if anchor_basis is not None and not basis_explicitly_invalid:
            self._known_offsets[(match.device_id, anchor_basis)] = match.time_offset
            self._known_timebases[match.device_id] = anchor_basis

        _LOGGER.debug(
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

    def _resolve_eid_internal(  # noqa: PLR0911, PLR0912
        self, eid_bytes: bytes
    ) -> tuple[list[EIDMatch], bytes | None, int | None]:
        """Internal EID resolution returning all matches.

        Returns:
            Tuple of (matches, matched_candidate, observed_frame).
            matches is empty if no match found.
        """
        self._ensure_cache_defaults()
        if not isinstance(eid_bytes, (bytes, bytearray)):
            return [], None, None

        raw = bytes(eid_bytes)
        candidates, observed_frame = self._extract_candidates(raw)
        raw_prefix = raw[:4].hex()
        candidate_prefixes = [candidate[:4].hex() for candidate in candidates]

        if not candidates:
            _LOGGER.debug(
                "No candidates extracted from payload (raw_prefix=%s, len=%d)",
                raw_prefix,
                len(raw),
            )
            return [], None, None

        if not self._lookup:
            is_locked = self._refresh_lock.locked()
            if self._pending_refresh or is_locked:
                _LOGGER.debug(
                    "RESOLVER NOT READY: cache priming (pending=%s locked=%s raw_prefix=%s)",
                    self._pending_refresh,
                    is_locked,
                    raw_prefix,
                )
                return [], None, None

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
            return [], None, None

        for candidate_prefix, candidate in zip(candidate_prefixes, candidates):
            matches = self._lookup.get(candidate)
            if not matches:
                continue

            metadata: dict[str, Any] = self._lookup_metadata.get(candidate) or {}
            now = int(time.time())

            # Update state for ALL matches (shared devices)
            for match in matches:
                self._update_match_state(
                    match,
                    metadata=metadata,
                    candidate=candidate,
                    observed_frame=observed_frame,
                    candidate_prefix=candidate_prefix,
                    raw_prefix=raw_prefix,
                    now=now,
                )

            # ---------------------------------------------------------
            # FMDN BLE battery: decode flags + store per device
            # ---------------------------------------------------------
            self._update_ble_battery(raw, observed_frame, metadata, matches)

            return matches, candidate, observed_frame

        # =================================================================
        # Heuristic Phone Discovery: Reactive check before logging MISS
        # =================================================================
        # If the standard cache lookup failed, attempt heuristic resolution
        # for Android phones that may use different rotation periods or
        # time bases than standard FMDN trackers.
        now_unix = int(time.time())
        heuristic_match = self._heuristic_resolve(candidates, now_unix=now_unix)
        if heuristic_match is not None:
            # Update standard tracking state for the heuristic match
            self._last_lock_confirmation[heuristic_match.device_id] = now_unix
            self._known_advertisement_reversed[heuristic_match.device_id] = (
                heuristic_match.is_reversed
            )
            return [heuristic_match], None, None

        # Log MISS with rate limiting for heuristic failures
        max_prefixes = 8
        prefix_log = ", ".join(candidate_prefixes[:max_prefixes])
        if len(candidate_prefixes) > max_prefixes:
            prefix_log = f"{prefix_log}, ..."

        if self._should_log_heuristic_miss(raw_prefix):
            _LOGGER.debug(
                "MISS (after heuristic): candidate_prefixes=%s raw_prefix=%s "
                "cache_size=%d identities=%d",
                prefix_log or "<none>",
                raw_prefix,
                len(self._lookup),
                len(self._cached_identities),
            )
        return [], None, None

    # ------------------------------------------------------------------
    # FMDN BLE battery decode + store
    # ------------------------------------------------------------------
    def _update_ble_battery(  # noqa: PLR0912
        self,
        raw: bytes,
        observed_frame: int | None,
        metadata: dict[str, Any],
        matches: list[EIDMatch],
    ) -> None:
        """Decode the FMDN hashed-flags byte and store battery state.

        Extracts the optional flags byte from the BLE payload, XOR-decodes
        it, and persists a :class:`BLEBatteryState` keyed by
        ``canonical_id`` (the Google API device identifier) for **every**
        matched device (shared-device propagation).

        The canonical_id key must match the ``device["id"]`` used by the
        coordinator snapshot and :class:`GoogleFindMyBLEBatterySensor` so
        that :meth:`get_ble_battery_state` lookups succeed.

        On first successful decode per device an INFO-level
        ``FMDN_FLAGS_PROBE`` log is emitted; subsequent updates log at
        DEBUG level only when the battery level changes.
        """
        if not matches:
            return

        length = len(raw)
        xor_mask: int | None = metadata.get("flags_xor_mask")

        # ---- Determine the hashed-flags byte position ----
        # FMDN frame-type semantics for 0x40/0x41 are not publicly fixed and
        # are accessory-generation specific (see docs/FMDN.md). The strict
        # mapping in docs/BLE_BATTERY_SENSOR.md (0x40<->20-byte EID,
        # 0x41<->32-byte EID) describes the common case, but variants with
        # 0x41 carrying a 20-byte legacy EID have been observed and are
        # already tolerated in `_extract_candidates` (raw-header path, see
        # the `RAW_HEADER_LENGTH + LEGACY_EID_LENGTH <= length <= +1` branch).
        # The lenient elif branches below mirror that tolerance for the
        # flags-byte lookup so a 29-byte service-data payload or a 22-byte
        # raw-header payload with `byte[frame_pos] == MODERN_FRAME_TYPE`
        # still decodes the UT-mode/battery flags instead of silently
        # dropping them. Range is intentionally narrow (legacy_min..legacy_min+1)
        # to match the existing tolerance and limit false-positive decodes.
        flags_byte: int | None = None
        # Service-data format: [header(7)][frame(1)][EID(N)][flags(1)]
        if (
            length >= SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH + 1
            and raw[7] == FMDN_FRAME_TYPE
        ):
            flags_byte = raw[SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH]
        elif (
            length >= SERVICE_DATA_OFFSET + MODERN_EID_LENGTH + 1
            and raw[7] == MODERN_FRAME_TYPE
        ):
            flags_byte = raw[SERVICE_DATA_OFFSET + MODERN_EID_LENGTH]
        elif (
            SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH + 1
            <= length
            <= SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH + 2
            and raw[7] == MODERN_FRAME_TYPE
        ):
            # Lenient: 0x41 frame with legacy-length payload (UT-mode variant).
            flags_byte = raw[SERVICE_DATA_OFFSET + LEGACY_EID_LENGTH]
        # Raw-header format: [frame(1)][EID(N)][flags(1)]
        elif (
            length >= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 1
            and raw[0] == FMDN_FRAME_TYPE
        ):
            flags_byte = raw[RAW_HEADER_LENGTH + LEGACY_EID_LENGTH]
        elif (
            length >= RAW_HEADER_LENGTH + MODERN_EID_LENGTH + 1
            and raw[0] == MODERN_FRAME_TYPE
        ):
            flags_byte = raw[RAW_HEADER_LENGTH + MODERN_EID_LENGTH]
        elif (
            RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 1
            <= length
            <= RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 2
            and raw[0] == MODERN_FRAME_TYPE
        ):
            # Lenient: 0x41 frame with legacy-length payload (UT-mode variant).
            flags_byte = raw[RAW_HEADER_LENGTH + LEGACY_EID_LENGTH]

        # ---- Decode and store ----
        # CANONICAL SPEC ANCHOR for FMDN hashed_flags decoding.
        # SPEC QUOTE (verbatim, retrieved 2026-06-02):
        #   "bits are referenced from most significant to least significant"
        # https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn#hashed_flags
        #
        # Spec bit numbering vs. standard LSB-first arithmetic:
        #   spec bit:  0    1    2    3    4    5    6    7
        #   value:    0x80 0x40 0x20 0x10 0x08 0x04 0x02 0x01   (spec bit 0 = MSB)
        #   meaning:  rsv  rsv  rsv  rsv  rsv  bat  bat  uwt
        #
        # Mapping to standard (LSB-first) bit indices used below:
        #   spec bits 5-6 (battery, 2 bit)  == standard bits 2-1 (mask 0x06, shift >> 1)
        #   spec bit  7   (UWT mode, 1 bit) == standard bit  0   (mask 0x01)
        # Module constants `FMDN_HASHED_FLAGS_BATTERY_MASK`,
        # `FMDN_HASHED_FLAGS_BATTERY_SHIFT`, `FMDN_HASHED_FLAGS_UWT_MODE_MASK`
        # in `eid_resolver.py` (top of module, FMDN frame-type cluster)
        # encode this mapping; their numeric values are drift-guarded by
        # `tests/test_ble_battery_sensor.py::TestFmdnHashedFlagsConstantsMatchSpec`.
        #
        # ANTI-PATTERN -- DO NOT "fix" this to (decoded >> 5) & 0x03 / decoded & 0x80.
        # That reads spec bits 2-3 (reserved zeros) instead of the battery field
        # and silently breaks normal/low/critical advertisements. The MSB-first
        # qualifier in the spec is the load-bearing detail. Doing so also fails:
        #   tests/test_ble_battery_sensor.py::TestHashedFlagsMsbFirstConvention
        #   tests/test_ble_battery_sensor.py::TestMsbFirstNotLsbFirstDemonstratesCodexMisinterpretation
        # See also docs/BLE_BATTERY_SENSOR.md L45.
        if flags_byte is not None and xor_mask is not None:
            decoded = flags_byte ^ xor_mask
            # Mask/shift centralized in FMDN_HASHED_FLAGS_* constants
            # (top of module, FMDN frame-type cluster).
            battery_raw = (
                decoded >> FMDN_HASHED_FLAGS_BATTERY_SHIFT
            ) & FMDN_HASHED_FLAGS_BATTERY_MASK
            uwt_mode = bool(decoded & FMDN_HASHED_FLAGS_UWT_MODE_MASK)
            battery_pct = FMDN_BATTERY_PCT.get(battery_raw)
            now_wall = time.time()

            state = BLEBatteryState(
                battery_level=battery_raw,
                battery_pct=battery_pct,
                uwt_mode=uwt_mode,
                decoded_flags=decoded,
                observed_at_wall=now_wall,
            )

            battery_labels = {
                0: "UNSUPPORTED",
                1: "NORMAL",
                2: "LOW",
                3: "CRITICALLY_LOW",
            }
            battery_label = battery_labels.get(battery_raw, f"UNKNOWN({battery_raw})")

            # Store for ALL matches (shared-device propagation).
            # Key by canonical_id (Google API device ID) — this is the same
            # identifier used by the coordinator snapshot (device["id"]) and
            # by GoogleFindMyBLEBatterySensor._device_id so that
            # get_ble_battery_state() lookups succeed.
            for match in matches:
                storage_key = match.canonical_id or match.device_id
                prev = self._ble_battery_state.get(storage_key)
                self._ble_battery_state[storage_key] = state

                # First decode per device → INFO probe log (once per device)
                if storage_key not in self._flags_logged_devices:
                    _LOGGER.info(
                        "FMDN_FLAGS_PROBE device=%s canonical=%s "
                        "flags_byte=0x%02x xor_mask=0x%02x decoded=0x%02x "
                        "battery=%s(%d) battery_pct=%s uwt_mode=%s "
                        "observed_frame=%s payload_len=%d",
                        match.device_id,
                        match.canonical_id,
                        flags_byte,
                        xor_mask,
                        decoded,
                        battery_label,
                        battery_raw,
                        battery_pct,
                        uwt_mode,
                        f"0x{observed_frame:02x}"
                        if observed_frame is not None
                        else None,
                        length,
                    )
                    self._flags_logged_devices.add(storage_key)
                elif prev is not None and prev.battery_level != battery_raw:
                    # Battery level changed → DEBUG log
                    _LOGGER.debug(
                        "BLE battery changed device=%s %s(%d)→%s(%d)",
                        storage_key,
                        battery_labels.get(prev.battery_level, "?"),
                        prev.battery_level,
                        battery_label,
                        battery_raw,
                    )
        else:
            # Cannot decode — log once per device at INFO for diagnostics
            for match in matches:
                storage_key = match.canonical_id or match.device_id
                if storage_key not in self._flags_logged_devices:
                    _max_hex = 40  # noqa: PLR2004
                    raw_hex = (
                        raw.hex()
                        if length <= _max_hex
                        else raw[:_max_hex].hex() + "..."
                    )
                    _LOGGER.info(
                        "FMDN_FLAGS_PROBE device=%s canonical=%s "
                        "CANNOT_DECODE observed_frame=%s payload_len=%d "
                        "has_xor_mask=%s flags_byte_found=%s raw_hex=%s",
                        match.device_id,
                        match.canonical_id,
                        f"0x{observed_frame:02x}"
                        if observed_frame is not None
                        else None,
                        length,
                        xor_mask is not None,
                        flags_byte is not None,
                        raw_hex,
                    )
                    self._flags_logged_devices.add(storage_key)

    # ------------------------------------------------------------------
    # Public BLE battery API
    # ------------------------------------------------------------------
    def get_ble_battery_state(self, device_id: str) -> BLEBatteryState | None:
        """Return the last observed BLE battery state for a device, or None.

        The *device_id* parameter is the **canonical_id** (Google API device
        identifier, i.e. ``device["id"]`` from the coordinator snapshot),
        not the HA device-registry ID.
        """
        return self._ble_battery_state.get(device_id)

    # ------------------------------------------------------------------
    # Public BLE scan info API (Phase 2.2 preparation)
    # ------------------------------------------------------------------
    def get_ble_scan_info(self, device_id: str) -> BLEScanInfo | None:
        """Return last observed BLE scan metadata for a device, or None.

        The *device_id* parameter is the **canonical_id** (Google API device
        identifier, i.e. ``device["id"]`` from the coordinator snapshot),
        not the HA device-registry ID.

        The returned :class:`BLEScanInfo` contains the current (rotated) BLE
        MAC address and the timestamp of the last observation.  FMDN trackers
        rotate their MAC every ~15 minutes, so callers should check
        ``observed_at`` freshness before attempting GATT connections.
        """
        return self._ble_scan_info.get(device_id)

    def _record_ble_scan_info(self, matches: list[EIDMatch], ble_address: str) -> None:
        """Store the BLE address for all matched devices.

        Called from :meth:`resolve_eid` when the caller provides a
        ``ble_address``.  Uses the same canonical_id keying pattern
        as :attr:`_ble_battery_state`.
        """
        now_mono = time.monotonic()
        now_wall = time.time()
        for match in matches:
            storage_key = match.canonical_id or match.device_id
            self._ble_scan_info[storage_key] = BLEScanInfo(
                ble_address=ble_address,
                observed_at=now_mono,
                observed_at_wall=now_wall,
            )

    def resolve_eid(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        eid_bytes: bytes,
        *,
        ble_address: str | None = None,
    ) -> EIDMatch | None:
        """Resolve a scanned payload to a Home Assistant device registry ID.

        For shared devices (same tracker across multiple accounts), this returns
        the match with the smallest time_offset (best match).
        Use resolve_eid_all() to get all matches.

        Args:
            eid_bytes: Raw EID bytes from a BLE advertisement.
            ble_address: Optional BLE MAC address of the advertising device.
                When provided, the address is stored for future direct GATT
                connections (e.g. BLE ring fallback).  This parameter is
                backward-compatible: existing callers that omit it are
                unaffected.
        """
        matches, _, _ = self._resolve_eid_internal(eid_bytes)
        if not matches:
            return None
        if ble_address is not None:
            self._record_ble_scan_info(matches, ble_address)
        # Return the match with the smallest absolute time_offset (best match)
        return min(matches, key=lambda m: abs(m.time_offset))

    def resolve_eid_all(
        self,
        eid_bytes: bytes,
        *,
        ble_address: str | None = None,
    ) -> list[EIDMatch]:
        """Resolve a scanned payload to all matching Home Assistant device registry IDs.

        This method supports shared devices: when the same physical tracker
        is shared between accounts, all accounts' matches are returned.
        Each match represents a different Home Assistant device registry entry
        for the same physical tracker.

        Args:
            eid_bytes: Raw EID bytes from a BLE advertisement.
            ble_address: Optional BLE MAC address of the advertising device.
                When provided, the address is stored for future direct GATT
                connections (e.g. BLE ring fallback).
        """
        matches, _, _ = self._resolve_eid_internal(eid_bytes)
        if matches and ble_address is not None:
            self._record_ble_scan_info(matches, ble_address)
        return matches

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

                if frame_type == FMDN_FRAME_TYPE and length >= (
                    RAW_HEADER_LENGTH + LEGACY_EID_LENGTH
                ):
                    candidates.append(
                        payload[
                            RAW_HEADER_LENGTH : RAW_HEADER_LENGTH + LEGACY_EID_LENGTH
                        ]
                    )
                elif frame_type == MODERN_FRAME_TYPE:
                    if length >= modern_required_length:
                        candidates.append(
                            payload[
                                RAW_HEADER_LENGTH : RAW_HEADER_LENGTH
                                + MODERN_EID_LENGTH
                            ]
                        )
                        return candidates, observed_frame
                    elif (
                        RAW_HEADER_LENGTH + LEGACY_EID_LENGTH
                        <= length
                        <= (RAW_HEADER_LENGTH + LEGACY_EID_LENGTH + 1)
                    ):
                        candidates.append(
                            payload[
                                RAW_HEADER_LENGTH : RAW_HEADER_LENGTH
                                + LEGACY_EID_LENGTH
                            ]
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

    def _log_truncated_frame(
        self, *, frame_type: int, payload_len: int, raw_len: int
    ) -> None:
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

    @staticmethod
    def _cancel_callback(unsub: CALLBACK_TYPE | None, name: str) -> None:
        """Cancel a callback or close a coroutine, logging any errors."""
        if unsub is None:
            return
        try:
            if callable(unsub):
                unsub()
            elif asyncio.iscoroutine(unsub):
                unsub.close()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Failed to cancel %s: %s", name, err)

    def stop(self) -> None:
        """Cancel background timers and clear cached state."""

        self._cancel_callback(self._unsub_alignment, "alignment timer")
        self._unsub_alignment = None

        self._cancel_callback(self._unsub_interval, "refresh interval")
        self._unsub_interval = None

        if self._load_task is not None:
            load_task = self._load_task
            self._load_task = None
            if not load_task.done():
                load_task.cancel()

        self._lookup.clear()
        self._lookup_metadata.clear()
        self._locks.clear()
        self._persisted_locks.clear()

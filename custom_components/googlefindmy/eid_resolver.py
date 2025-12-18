# custom_components/googlefindmy/eid_resolver.py
"""Ephemeral identifier resolver for local BLE scans.

This module exposes a resolver that maps rotating Find My Device Network
EIDs to Home Assistant devices managed by the integration.

Design goals (2025-12):
- The resolver must only generate EIDs from *relative* provisioning counters
  (seconds since pairing/provisioning anchors). Using Unix time directly is
  not compatible with the Find My Device Network EID PRF inputs.
- A device "lock" must be created *only after a real HIT* for that device.
  The lock stores only stable parameters needed to compute future EIDs
  without repeating wide brute-force searches:
    * timebase (pair_date vs secrets_creation_date)
    * curve family (legacy secp160r1 vs modern P-256)
    * P-256 scalar endianness (big vs little endian) when applicable
    * rotation_shift (integer number of rotation periods relative to the
      predicted rotation boundary for the chosen anchor)
    * (optional) whether the observed EID bytes are reversed on the scan path
- Locks are per-device only. There is no inference from one tracker to another.
- Persisted locks must not store volatile values such as the current EID,
  raw counters, or anchor timestamps that can be re-derived from API data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import (
    EIK_LENGTH,
    LEGACY_EID_LENGTH,
    ROTATION_PERIOD,
    EidCandidate,
)
from .FMDNCrypto.eid_generator import (
    generate_eid as _legacy_generate_eid,
)
from .FMDNCrypto.eid_generator import (
    generate_eid_p256 as _modern_generate_eid,
)
from .FMDNCrypto.eid_generator import (
    generate_eid_p256_le as _modern_generate_eid_le,
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

generate_eid = _legacy_generate_eid
generate_eid_p256 = _modern_generate_eid
generate_eid_p256_le = _modern_generate_eid_le

_LOGGER = logging.getLogger(__name__)

EID_LENGTH = LEGACY_EID_LENGTH
RAW_HEADER_LENGTH = 1
FMDN_FRAME_TYPE = 0x40
FHNA_FRAME_TYPE_LEGACY = 0x40
FHNA_FRAME_TYPE_MODERN = 0x41
MODERN_EID_LENGTH = 32
FHNA_SERVICE_OFFSET = 8
FHNA_LEGACY_SERVICE_TOTAL = FHNA_SERVICE_OFFSET + EID_LENGTH
FHNA_MODERN_SERVICE_TOTAL = FHNA_SERVICE_OFFSET + MODERN_EID_LENGTH
FHNA_BACKCOMP_LEGACY_TOTAL = RAW_HEADER_LENGTH + EID_LENGTH
FHNA_BACKCOMP_MODERN_TOTAL = RAW_HEADER_LENGTH + MODERN_EID_LENGTH

# Rotation search parameters (unlocked brute-force).
NARROW_SCAN_RANGE: tuple[int, ...] = (0, -1, 1, -2, 2, -3, 3)
REL_DEEP_SCAN_DENSE_RADIUS = 96
REL_DEEP_SCAN_MAX_DRIFT = 180
REL_DEEP_SCAN_SPARSE_STEP = 4
REL_FALLBACK_VARIANT_LIMIT = 8

# Lock parameters (locked fast-path).
LOCK_SCAN_RANGE: tuple[int, ...] = (-2, -1, 0, 1, 2)
LOCK_CUSTOM_FIELD = "eid_timebase_lock"
LOCK_SCHEMA_VERSION = 2

# Be conservative: devices may be out of BLE range for hours/days.
# We only treat a lock as stale after multiple full rotations without a HIT.
LOCK_STALE_AFTER_SECONDS = 12 * ROTATION_PERIOD

DEBUG_LOG_LIMIT = 50
FUTURE_ANCHOR_MAX_DRIFT = 86400

ENABLE_P256_TAIL_TRUNCATION = True
_TAIL_TRUNCATION_LOG_FLAG: list[bool] = [False]
_LE_GENERATION_WARN_FLAG: list[bool] = [False]


class TimebaseLabel:
    """Timebase candidates used when generating EID windows.

    IMPORTANT: ABSOLUTE is retained for backward compatibility with older
    artifacts, but the resolver no longer generates EIDs from Unix time.
    """

    ABSOLUTE = "ABSOLUTE"
    REL_PAIR = "REL_PAIR"
    REL_SECRETS = "REL_SECRETS"


@dataclass(slots=True, frozen=True)
class _TimebaseCandidate:
    """Candidate clock anchor used during EID generation.

    ``reference_time`` is the provisioning counter value "now" for the anchor:
    seconds since the anchor epoch (pairing / secrets creation), unmasked.
    """

    label: str
    reference_time: int
    anchor_epoch: int


class CurveLabel:
    """Curve families supported by the resolver."""

    LEGACY_SECP160R1 = "secp160r1"
    P256 = "p256"


class P256Endian:
    """Endianness labels for P-256 scalar derivation."""

    BIG = "be"
    LITTLE = "le"


@dataclass(slots=True, frozen=True)
class _DeviceLock:
    """Per-device lock derived from a confirmed HIT.

    The lock stores only stable parameters required to compute future EIDs
    without repeating deep brute force.
    """

    schema: int
    timebase: str
    rotation_shift: int
    curve: str
    p256_endian: str | None
    eid_bytes_reversed: bool
    variant: str | None
    confirmed_at: int
    hit_count: int = 1


def iter_rotation_windows(
    target_time: int,
    *,
    rotation_period: int,
    window_range: Iterable[int],
    include_neighbors: bool,
    allow_negative: bool = False,
) -> tuple[int, ...]:
    """Return rotation-aligned timestamps for cache population.

    ``target_time`` is the provisioning counter (seconds since anchor).
    The helper aligns it to a rotation boundary and walks ``window_range`` to
    build candidate windows.
    """

    rotation_start = target_time - (target_time % rotation_period)
    windows: list[int] = []

    for offset in window_range:
        timestamp = rotation_start + (offset * rotation_period)
        if timestamp < 0 and not allow_negative:
            continue
        if include_neighbors:
            previous_window = timestamp - rotation_period
            next_window = timestamp + rotation_period
            for candidate in (timestamp, previous_window, next_window):
                if candidate < 0 and not allow_negative:
                    continue
                windows.append(candidate)
        else:
            windows.append(timestamp)

    # Preserve insertion order while removing duplicates.
    return tuple(dict.fromkeys(windows))


def _normalize_identity_key(identity_key: bytes) -> bytes:
    """Return a fixed-length identity key for candidate generation."""

    if len(identity_key) == EIK_LENGTH:
        return identity_key
    if len(identity_key) < EIK_LENGTH:
        return identity_key.ljust(EIK_LENGTH, b"\x00")
    return identity_key[:EIK_LENGTH]


def _coerce_int(value: Any) -> int | None:
    """Return a best-effort integer conversion for optional payloads."""

    seconds: Any | None
    nanos_only = False

    if isinstance(value, Mapping):
        seconds = value.get("seconds")
        nanos_only = seconds is None and ("nanos" in value or "nsec" in value)
    else:
        seconds = getattr(value, "seconds", None)

    if nanos_only:
        return None

    if seconds is not None:
        try:
            return int(seconds)
        except (TypeError, ValueError):
            return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mask_u32(value: int) -> int:
    """Return a 32-bit counter value aligned with tracker expectations."""

    return value & 0xFFFFFFFF


def _eid_prefix(eid: bytes, length: int = 8) -> str:
    """Return a short hexadecimal prefix for log safety."""

    if length <= 0:
        return ""
    return eid.hex()[:length]


def _infer_curve_and_endian(variant: str | None) -> tuple[str | None, str | None]:
    """Infer curve family and P-256 endianness from an EidCandidate name."""

    if not variant:
        return None, None

    name = variant.lower()
    if "secp160r1" in name:
        return CurveLabel.LEGACY_SECP160R1, None

    # P-256 big endian candidates include:
    # - fhna_secp256r1_rx32
    # - fhna_p256_truncated_*
    if "p256_le" in name or "_le_" in name:
        return CurveLabel.P256, P256Endian.LITTLE
    if "secp256r1" in name or name.startswith("fhna_p256_truncated"):
        return CurveLabel.P256, P256Endian.BIG

    return None, None


def _is_variant_compatible(candidate_name: str, lock: _DeviceLock) -> bool:
    """Return True when a candidate variant matches the lock parameters."""

    name = candidate_name.lower()
    if lock.curve == CurveLabel.LEGACY_SECP160R1:
        return "secp160r1" in name

    if lock.curve == CurveLabel.P256:
        if lock.p256_endian == P256Endian.LITTLE:
            return "p256_le" in name or "_le_" in name
        # Default: big endian
        return ("secp256r1" in name) or name.startswith("fhna_p256_truncated")

    return True


def _parse_persisted_lock(
    raw: Mapping[str, Any],
) -> _DeviceLock | None:
    """Parse a persisted lock from registry custom fields.

    Supported schema: LOCK_SCHEMA_VERSION (v2).
    Legacy payloads are treated as invalid and must be cleaned up by caller.
    """

    schema = _coerce_int(raw.get("schema"))
    if schema != LOCK_SCHEMA_VERSION:
        return None

    timebase_raw = raw.get("timebase")
    curve_raw = raw.get("curve")
    endian_raw = raw.get("p256_endian")
    shift_raw = raw.get("rotation_shift")
    reversed_raw = raw.get("eid_bytes_reversed")
    confirmed_raw = raw.get("confirmed_at")
    hit_count_raw = raw.get("hit_count")
    variant_raw = raw.get("variant")

    try:
        timebase = str(timebase_raw)
        curve = str(curve_raw)
    except Exception:  # pragma: no cover - defensive
        return None

    if timebase not in (TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS):
        return None
    if curve not in (CurveLabel.LEGACY_SECP160R1, CurveLabel.P256):
        return None

    rotation_shift = _coerce_int(shift_raw)
    confirmed_at = _coerce_int(confirmed_raw)
    if rotation_shift is None or confirmed_at is None:
        return None

    p256_endian: str | None = None
    if curve == CurveLabel.P256:
        try:
            p256_endian = str(endian_raw) if endian_raw is not None else None
        except Exception:  # pragma: no cover
            p256_endian = None
        if p256_endian not in (P256Endian.BIG, P256Endian.LITTLE):
            # Default to big endian if unset/unknown.
            p256_endian = P256Endian.BIG

    try:
        eid_bytes_reversed = bool(reversed_raw)
    except Exception:  # pragma: no cover
        eid_bytes_reversed = False

    hit_count = _coerce_int(hit_count_raw) or 1

    variant: str | None
    try:
        variant = str(variant_raw) if variant_raw is not None else None
    except Exception:  # pragma: no cover
        variant = None

    return _DeviceLock(
        schema=LOCK_SCHEMA_VERSION,
        timebase=timebase,
        rotation_shift=rotation_shift,
        curve=curve,
        p256_endian=p256_endian,
        eid_bytes_reversed=eid_bytes_reversed,
        variant=variant,
        confirmed_at=confirmed_at,
        hit_count=max(1, hit_count),
    )


def _serialize_lock(lock: _DeviceLock) -> dict[str, Any]:
    """Serialize a lock into the persisted registry payload."""

    payload: dict[str, Any] = {
        "schema": LOCK_SCHEMA_VERSION,
        "timebase": lock.timebase,
        "rotation_shift": lock.rotation_shift,
        "curve": lock.curve,
        "eid_bytes_reversed": lock.eid_bytes_reversed,
        "confirmed_at": lock.confirmed_at,
        "hit_count": lock.hit_count,
    }
    if lock.variant is not None:
        payload["variant"] = lock.variant
    if lock.curve == CurveLabel.P256 and lock.p256_endian is not None:
        payload["p256_endian"] = lock.p256_endian
    return payload


def _iter_debug_timebases(anchors: Any, *, now_unix: int) -> list[_TimebaseCandidate]:
    """Parse optional debug anchors into timebase candidates.

    The resolver tolerates a handful of shapes when the server returns anchor
    diagnostics. The candidates returned are still *relative* counters.
    """

    if not isinstance(anchors, Mapping):
        return []

    candidates: list[_TimebaseCandidate] = []
    hint_list: Iterable[Any]
    raw_anchors = anchors.get("anchors")
    if isinstance(raw_anchors, Iterable) and not isinstance(
        raw_anchors, (str, bytes, bytearray)
    ):
        hint_list = raw_anchors
    else:
        hint_list = [anchors]

    for raw in hint_list:
        if not isinstance(raw, Mapping):
            continue

        anchor_epoch_raw = (
            raw.get("anchor_epoch") or raw.get("epoch") or raw.get("timestamp")
        )
        anchor_epoch = _coerce_int(anchor_epoch_raw)
        if anchor_epoch is None:
            continue

        offset_raw = raw.get("time_offset") or raw.get("offset") or raw.get("delta")
        offset_seconds = _coerce_int(offset_raw) or 0

        label = str(raw.get("label") or "REL_DEBUG")
        anchor_epoch_adjusted = anchor_epoch + offset_seconds
        counter_now = now_unix - anchor_epoch_adjusted
        candidates.append(
            _TimebaseCandidate(
                label=label,
                reference_time=counter_now,
                anchor_epoch=anchor_epoch_adjusted,
            )
        )

    unique: dict[tuple[str, int, int], _TimebaseCandidate] = {}
    for candidate in candidates:
        unique[(candidate.label, candidate.reference_time, candidate.anchor_epoch)] = (
            candidate
        )
    return list(unique.values())


def _build_timebase_candidates(
    identity: DeviceIdentity,
    *,
    now_unix: int,
) -> list[_TimebaseCandidate]:
    """Return candidate timebases derived from identity anchors.

    Only relative timebases are returned. If the backend did not provide any
    anchor information, no candidates are produced.
    """

    candidates: list[_TimebaseCandidate] = []

    def _add_anchor_candidate(label: str, anchor_epoch: Any) -> None:
        anchor_value = _coerce_int(anchor_epoch)
        if anchor_value is None:
            return

        anchor_age = now_unix - anchor_value
        if anchor_age < -FUTURE_ANCHOR_MAX_DRIFT:
            # Anchor appears far in the future; ignore.
            return

        candidates.append(
            _TimebaseCandidate(
                label=label,
                reference_time=anchor_age,
                anchor_epoch=anchor_value,
            )
        )

    _add_anchor_candidate(TimebaseLabel.REL_SECRETS, identity.secrets_creation_date)
    _add_anchor_candidate(TimebaseLabel.REL_PAIR, identity.pair_date)

    if identity.time_anchors_debug is not None:
        candidates.extend(_iter_debug_timebases(identity.time_anchors_debug, now_unix=now_unix))

    # De-duplicate.
    unique: dict[tuple[str, int, int], _TimebaseCandidate] = {}
    for candidate in candidates:
        unique[(candidate.label, candidate.reference_time, candidate.anchor_epoch)] = (
            candidate
        )
    return list(unique.values())


@lru_cache(maxsize=512)
def _cached_candidates(identity_key: bytes, timestamp: int) -> tuple[EidCandidate, ...]:
    """Return cached EID candidates for an identity key and window timestamp.

    ``timestamp`` must be the provisioning counter (seconds since pairing),
    masked to uint32, to mirror tracker-side PRF input.
    """

    normalized_key = _normalize_identity_key(identity_key)

    try:
        legacy_eid = generate_eid(identity_key, timestamp)
    except ValueError:
        legacy_eid = generate_eid(normalized_key, timestamp)
    candidates: list[EidCandidate] = [
        EidCandidate(name="fhna_secp160r1_rx20", eid=legacy_eid)
    ]

    if len(normalized_key) == EIK_LENGTH:
        try:
            modern_eid = generate_eid_p256(identity_key, timestamp)
        except ValueError:
            modern_eid = generate_eid_p256(normalized_key, timestamp)

        candidates.append(EidCandidate(name="fhna_secp256r1_rx32", eid=modern_eid))
        candidates.append(
            EidCandidate(
                name="fhna_p256_truncated_rx20",
                eid=modern_eid[:LEGACY_EID_LENGTH],
            )
        )
        if ENABLE_P256_TAIL_TRUNCATION:
            candidates.append(
                EidCandidate(
                    name="fhna_p256_truncated_tail_rx20",
                    eid=modern_eid[-LEGACY_EID_LENGTH:],
                )
            )

        # Little-endian P-256 scalar derivation (Moto Tag / Chipolo-like trackers).
        try:
            modern_eid_le = _modern_generate_eid_le(identity_key, timestamp)
        except (ValueError, TypeError) as exc:
            try:
                modern_eid_le = _modern_generate_eid_le(normalized_key, timestamp)
            except (ValueError, TypeError):
                if not _LE_GENERATION_WARN_FLAG[0]:
                    _LOGGER.warning(
                        "EID generation failed for LE variant (key type %s): %s",
                        type(identity_key),
                        exc,
                    )
                    _LE_GENERATION_WARN_FLAG[0] = True
                modern_eid_le = b""

        if modern_eid_le:
            candidates.append(EidCandidate(name="fhna_p256_le_rx32", eid=modern_eid_le))
            candidates.append(
                EidCandidate(
                    name="fhna_p256_le_truncated_rx20",
                    eid=modern_eid_le[:LEGACY_EID_LENGTH],
                )
            )
            if ENABLE_P256_TAIL_TRUNCATION:
                candidates.append(
                    EidCandidate(
                        name="fhna_p256_le_truncated_tail_rx20",
                        eid=modern_eid_le[-LEGACY_EID_LENGTH:],
                    )
                )

        if ENABLE_P256_TAIL_TRUNCATION and not _TAIL_TRUNCATION_LOG_FLAG[0]:
            _LOGGER.debug("Tail truncation & LE variants enabled for P-256 EIDs")
            _TAIL_TRUNCATION_LOG_FLAG[0] = True

    unique_candidates: dict[bytes, EidCandidate] = {}
    for candidate in candidates:
        if candidate.eid not in unique_candidates:
            unique_candidates[candidate.eid] = candidate

    return tuple(unique_candidates.values())


class EIDMatch(NamedTuple):
    """Resolved mapping between an EID and a Home Assistant device."""

    device_id: str
    config_entry_id: str
    canonical_id: str
    time_offset: int
    is_reversed: bool


class IdentityKeyDecryptionResult(NamedTuple):
    """Result from attempting to decrypt a device identity key."""

    key: bytes | None
    metadata: dict[str, Any]


@dataclass(slots=True)
class GoogleFindMyEIDResolver:
    """Resolver that precalculates rotating EIDs for known trackers."""

    hass: HomeAssistant
    _lookup: dict[bytes, EIDMatch] = field(init=False, default_factory=dict)
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(init=False, default_factory=dict)
    _decryption_status: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)

    # Per-device locks (derived from HITs) and persisted payload snapshots.
    _device_locks: dict[str, _DeviceLock] = field(init=False, default_factory=dict)
    _persisted_locks: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)

    # HIT timestamps for staleness evaluation.
    _last_lock_confirmation: dict[str, int] = field(init=False, default_factory=dict)

    _unsub_interval: CALLBACK_TYPE | None = field(init=False, default=None)
    _unsub_alignment: CALLBACK_TYPE | None = field(init=False, default=None)
    _refresh_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _pending_refresh: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        """Set up caches and schedule the first rotation-aligned refresh."""
        self._start_alignment_timer()

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

    async def async_refresh(self) -> None:
        """Trigger a cache refresh immediately."""
        if self._refresh_lock.locked():
            self._pending_refresh = True
            return

        async with self._refresh_lock:
            pending_refresh = self._pending_refresh
            self._pending_refresh = False
            await self._refresh_cache()
            while self._pending_refresh:
                self._pending_refresh = False
                await self._refresh_cache()
            if pending_refresh:
                self._pending_refresh = False

    async def _refresh_cache(self) -> None:  # noqa: PLR0915
        """Rebuild the EID lookup table for enabled, non-ignored devices."""
        cache_clear = getattr(_cached_candidates, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()

        identities: list[DeviceIdentity] = await self._collect_device_secrets()
        now_unix = int(time.time())
        stale_cutoff_ts = now_unix - LOCK_STALE_AFTER_SECONDS

        lookup: dict[bytes, EIDMatch] = {}
        lookup_metadata: dict[bytes, dict[str, Any]] = {}
        debug_log_state: dict[str, dict[str, Any]] = {}

        # Rehydrate persisted locks (best-effort; do not spam logs when unsupported).
        device_reg = None
        try:
            device_reg = dr.async_get(self.hass)
        except Exception:  # pragma: no cover - defensive
            device_reg = None

        if device_reg is not None:
            for identity in identities:
                registry_id = identity.registry_id
                if not registry_id:
                    continue

                entry = device_reg.async_get(registry_id)
                if entry is None:
                    continue

                custom_fields = getattr(entry, "custom_fields", None)
                if not isinstance(custom_fields, Mapping):
                    # Registry does not expose custom fields for this entry/version.
                    continue

                lock_payload = custom_fields.get(LOCK_CUSTOM_FIELD)
                if lock_payload is None:
                    continue

                if not isinstance(lock_payload, Mapping):
                    # Malformed; remove it silently.
                    cleaned = dict(custom_fields)
                    cleaned.pop(LOCK_CUSTOM_FIELD, None)
                    try:
                        device_reg.async_update_device(registry_id, custom_fields=cleaned)
                    except Exception:  # pragma: no cover
                        pass
                    continue

                parsed = _parse_persisted_lock(lock_payload)
                if parsed is None:
                    # Legacy / unsupported schema. Remove to prevent stale wrong locks.
                    cleaned = dict(custom_fields)
                    cleaned.pop(LOCK_CUSTOM_FIELD, None)
                    try:
                        device_reg.async_update_device(registry_id, custom_fields=cleaned)
                    except Exception:  # pragma: no cover
                        pass
                    self._device_locks.pop(registry_id, None)
                    self._persisted_locks.pop(registry_id, None)
                    self._last_lock_confirmation.pop(registry_id, None)
                    continue

                if parsed.confirmed_at < stale_cutoff_ts:
                    cleaned = dict(custom_fields)
                    cleaned.pop(LOCK_CUSTOM_FIELD, None)
                    try:
                        device_reg.async_update_device(registry_id, custom_fields=cleaned)
                    except Exception:  # pragma: no cover
                        pass
                    self._device_locks.pop(registry_id, None)
                    self._persisted_locks.pop(registry_id, None)
                    self._last_lock_confirmation.pop(registry_id, None)
                    continue

                self._device_locks[registry_id] = parsed
                self._persisted_locks[registry_id] = dict(lock_payload)
                self._last_lock_confirmation[registry_id] = parsed.confirmed_at

        # Build lookup table.
        first_identity_id: str | None = None

        for identity in identities:
            if (
                not identity.config_entry_id
                or identity.identity_key is None
                or identity.registry_id is None
            ):
                continue

            if first_identity_id is None:
                first_identity_id = identity.canonical_id

            config_entry_id = identity.config_entry_id
            registry_id = identity.registry_id
            identity_key_bytes = bytes(identity.identity_key)

            device_lock = self._device_locks.get(registry_id)

            status = self._decryption_status.get(identity.canonical_id, {})
            skip_deep_scan = bool(status and status.get("status") not in {"decrypted", "wrapped_decrypted"})

            def _register_with_metadata(
                eid_value: bytes,
                reversed_flag: bool,
                metadata_payload: dict[str, Any],
            ) -> None:
                # Strict invariant: lookup keys must be real EIDs only (20B legacy / 32B modern).
                if len(eid_value) not in (EID_LENGTH, MODERN_EID_LENGTH):
                    return

                match = EIDMatch(
                    device_id=registry_id,
                    config_entry_id=config_entry_id,
                    canonical_id=identity.canonical_id,
                    time_offset=int(metadata_payload.get("time_offset", 0)),
                    is_reversed=reversed_flag,
                )

                existing = lookup.get(eid_value)
                existing_metadata = lookup_metadata.get(eid_value)

                def _score(meta: Mapping[str, Any], is_reversed_local: bool) -> tuple[int, int, int, str]:
                    # Prefer:
                    # 1) entries consistent with an active device lock
                    # 2) smaller absolute time offsets (closer windows)
                    # 3) non-reversed scan bytes (stable default)
                    # 4) deterministic tie-breaker by variant name
                    lock_rank = 1
                    if device_lock is not None:
                        if (
                            meta.get("timebase") == device_lock.timebase
                            and _is_variant_compatible(str(meta.get("variant") or ""), device_lock)
                        ):
                            lock_rank = 0
                    abs_offset = abs(_coerce_int(meta.get("time_offset")) or 0)
                    reverse_rank = 0 if not is_reversed_local else 1
                    variant_rank = str(meta.get("variant") or "")
                    return (lock_rank, abs_offset, reverse_rank, variant_rank)

                history: list[dict[str, Any]] = []
                best_metadata: dict[str, Any] | None = None
                best_match = existing
                best_score: tuple[int, int, int, str] | None = None
                best_is_reversed = False

                if isinstance(existing_metadata, dict):
                    prior = existing_metadata.get("timebases")
                    if isinstance(prior, list):
                        history = [item for item in prior if isinstance(item, dict)]
                    base_metadata = {
                        key: value
                        for key, value in existing_metadata.items()
                        if key not in {"timebases", "is_reversed"}
                    }
                    if not history:
                        history = [base_metadata]
                    best_metadata = base_metadata
                    best_is_reversed = bool(existing_metadata.get("is_reversed", False))
                    best_score = _score(base_metadata, best_is_reversed)

                if metadata_payload not in history:
                    history.append(metadata_payload)

                candidate_score = _score(metadata_payload, reversed_flag)

                if best_metadata is None or best_score is None or candidate_score < best_score:
                    best_metadata = metadata_payload
                    best_match = match
                    best_score = candidate_score
                    best_is_reversed = reversed_flag

                if best_metadata is None:
                    return

                lookup[eid_value] = best_match or match
                lookup_metadata[eid_value] = {
                    **best_metadata,
                    "is_reversed": best_is_reversed,
                    "timebases": history,
                }

            base_candidates = _build_timebase_candidates(identity, now_unix=now_unix)
            if not base_candidates:
                _LOGGER.debug(
                    "No relative anchors available for %s; skipping EID generation",
                    identity.canonical_id,
                )
                continue

            # If a lock exists, restrict to the locked timebase only (per device).
            if device_lock is not None:
                base_candidates = [c for c in base_candidates if c.label == device_lock.timebase] or base_candidates

            # Debug dump only for the first device to avoid log spam.
            should_log_debug_dump = (
                first_identity_id is not None and identity.canonical_id == first_identity_id
            )
            device_debug_state = debug_log_state.setdefault(
                registry_id, {"count": 0, "seen": set()}
            )

            recorded_timebases: set[str] = set()

            for candidate in base_candidates:
                recorded_timebases.add(candidate.label)
                allow_negative = candidate.reference_time < 0

                if device_lock is not None and candidate.label == device_lock.timebase:
                    # Locked fast-path: compute the predicted rotation boundary for the current
                    # counter and apply the integer rotation_shift.
                    predicted_rotation = candidate.reference_time - (candidate.reference_time % ROTATION_PERIOD)
                    locked_rotation = predicted_rotation + (device_lock.rotation_shift * ROTATION_PERIOD)

                    rotation_windows = iter_rotation_windows(
                        locked_rotation,
                        rotation_period=ROTATION_PERIOD,
                        window_range=LOCK_SCAN_RANGE,
                        include_neighbors=False,
                        allow_negative=allow_negative,
                    )
                    passes: list[tuple[tuple[int, ...], bool, bool]] = [
                        (rotation_windows, False, True)
                    ]
                else:
                    # Unlocked brute-force: search around predicted rotation boundary.
                    passes = []
                    base_target_time = candidate.reference_time
                    rotation_windows = iter_rotation_windows(
                        base_target_time,
                        rotation_period=ROTATION_PERIOD,
                        window_range=NARROW_SCAN_RANGE,
                        include_neighbors=False,
                        allow_negative=allow_negative,
                    )
                    passes.append((rotation_windows, False, False))

                    if not skip_deep_scan:
                        dense = iter_rotation_windows(
                            base_target_time,
                            rotation_period=ROTATION_PERIOD,
                            window_range=range(-REL_DEEP_SCAN_DENSE_RADIUS, REL_DEEP_SCAN_DENSE_RADIUS + 1),
                            include_neighbors=False,
                            allow_negative=allow_negative,
                        )
                        passes.append((dense, False, False))

                        if REL_DEEP_SCAN_MAX_DRIFT > REL_DEEP_SCAN_DENSE_RADIUS:
                            sparse_negative = range(
                                -REL_DEEP_SCAN_MAX_DRIFT,
                                -REL_DEEP_SCAN_DENSE_RADIUS,
                                REL_DEEP_SCAN_SPARSE_STEP,
                            )
                            sparse_positive = range(
                                REL_DEEP_SCAN_DENSE_RADIUS + REL_DEEP_SCAN_SPARSE_STEP,
                                REL_DEEP_SCAN_MAX_DRIFT + 1,
                                REL_DEEP_SCAN_SPARSE_STEP,
                            )
                            sparse_range = tuple(sparse_negative) + tuple(sparse_positive)
                            sparse = iter_rotation_windows(
                                base_target_time,
                                rotation_period=ROTATION_PERIOD,
                                window_range=sparse_range,
                                include_neighbors=False,
                                allow_negative=allow_negative,
                            )
                            passes.append((sparse, False, False))

                for rotation_windows, _include_neighbors, locked_pass in passes:
                    for window_timestamp in rotation_windows:
                        time_offset = window_timestamp - candidate.reference_time
                        masked_timestamp = _mask_u32(window_timestamp)

                        try:
                            eid_candidates = _cached_candidates(identity_key_bytes, masked_timestamp)
                        except Exception as err:  # noqa: BLE001 - defensive
                            _LOGGER.debug(
                                "Failed to generate EID candidates for %s at %s: %s",
                                identity.canonical_id,
                                window_timestamp,
                                err,
                            )
                            continue

                        for eid_candidate in eid_candidates:
                            if locked_pass and device_lock is not None:
                                if not _is_variant_compatible(eid_candidate.name, device_lock):
                                    continue

                            metadata = {
                                "timebase": candidate.label,
                                "anchor_epoch": candidate.anchor_epoch,
                                "rotation_timestamp": window_timestamp,
                                "masked_rotation_timestamp": masked_timestamp,
                                "time_offset": time_offset,
                                "variant": eid_candidate.name,
                                "locked": bool(locked_pass),
                            }

                            if should_log_debug_dump and device_debug_state["count"] < DEBUG_LOG_LIMIT:
                                message_key = (
                                    candidate.label,
                                    eid_candidate.name,
                                    masked_timestamp,
                                    locked_pass,
                                )
                                if message_key not in device_debug_state["seen"]:
                                    device_debug_state["seen"].add(message_key)
                                    device_debug_state["count"] += 1
                                    _LOGGER.debug(
                                        "DEBUG DUMP: Device=%s Timebase=%s Variant=%s Locked=%s Anchor=%s Offset=%s TS=%s EID_PREFIX=%s",
                                        identity.canonical_id,
                                        candidate.label,
                                        eid_candidate.name,
                                        locked_pass,
                                        candidate.anchor_epoch,
                                        time_offset,
                                        window_timestamp,
                                        _eid_prefix(eid_candidate.eid),
                                    )

                            # Register both byte orders; scan stacks may expose either.
                            _register_with_metadata(eid_candidate.eid, False, metadata)
                            _register_with_metadata(eid_candidate.eid[::-1], True, metadata)

            # Minimal relative fallbacks (only if the timebase was not generated for some reason).
            for rel_label in (TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS):
                if rel_label in recorded_timebases:
                    continue

                rel_candidate = next((c for c in base_candidates if c.label == rel_label), None)
                if rel_candidate is None:
                    continue

                fallback_rotation = rel_candidate.reference_time - (rel_candidate.reference_time % ROTATION_PERIOD)
                try:
                    fallback_candidates = _cached_candidates(identity_key_bytes, _mask_u32(fallback_rotation))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Failed to generate fallback %s candidates for %s: %s",
                        rel_label,
                        identity.canonical_id,
                        err,
                    )
                    continue

                for eid_candidate in fallback_candidates[:REL_FALLBACK_VARIANT_LIMIT]:
                    fallback_metadata = {
                        "timebase": rel_label,
                        "anchor_epoch": rel_candidate.anchor_epoch,
                        "rotation_timestamp": fallback_rotation,
                        "masked_rotation_timestamp": _mask_u32(fallback_rotation),
                        "time_offset": fallback_rotation - rel_candidate.reference_time,
                        "variant": eid_candidate.name,
                        "locked": False,
                    }
                    _register_with_metadata(eid_candidate.eid, False, fallback_metadata)
                    _register_with_metadata(eid_candidate.eid[::-1], True, fallback_metadata)

        self._lookup = lookup
        self._lookup_metadata = lookup_metadata
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
                self._decryption_status[identity.canonical_id] = result.metadata
                if result.key is None:
                    continue
                normalized.append(replace(identity, identity_key=result.key))
                continue

            normalized.append(identity)

        return normalized

    async def _try_decrypt_identity_key(  # noqa: PLR0912
        self,
        identity: DeviceIdentity,
        *,
        cache: TokenCache | None,
    ) -> IdentityKeyDecryptionResult:
        """Decrypt encrypted identity key when owner/shared key material is available."""
        if (
            cache is None
            or identity.encrypted_identity_key is None
            or not isinstance(identity.encrypted_identity_key, (bytes, bytearray))
        ):
            return IdentityKeyDecryptionResult(
                None,
                {"status": "skipped", "reason": "missing_cache_or_ciphertext"},
            )

        metadata: dict[str, Any] = {
            "status": "pending",
            "ciphertext_length": len(identity.encrypted_identity_key),
        }
        try:
            owner_key_info: OwnerKeyInfo = await async_get_owner_key(cache=cache)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to retrieve owner key for %s: %s", identity.canonical_id, err)
            return IdentityKeyDecryptionResult(
                None,
                {**metadata, "status": "failed", "reason": "owner_key_unavailable"},
            )

        expected_version = identity.owner_key_version
        if (
            expected_version is not None
            and owner_key_info.version is not None
            and expected_version != owner_key_info.version
        ):
            try:
                owner_key_info = await async_get_owner_key(cache=cache, force_refresh=True)
            except Exception:  # pragma: no cover - best-effort
                pass

        key_sources: list[tuple[str, bytes]] = [("owner", owner_key_info.key)]
        try:
            shared_key = await async_get_shared_key(cache=cache)
        except Exception:  # pragma: no cover - best-effort
            shared_key = None
        if shared_key is not None:
            key_sources.append(("shared", shared_key))

        metadata["key_sources"] = [source for source, _ in key_sources]

        encrypted_identity_key = bytes(identity.encrypted_identity_key)

        suggested_mcu = is_mcu_tracker(
            device_type=identity.device_type,
            fast_pair_model_id=identity.fast_pair_model_id,
        )
        candidates = [suggested_mcu, not suggested_mcu]

        wrapped_failure: IdentityKeyDecryptionResult | None = None
        gcm_result = self._unwrap_aes_gcm_identity_key(
            identity=identity,
            encrypted_identity_key=encrypted_identity_key,
            key_sources=key_sources,
            metadata=metadata,
        )
        if gcm_result is not None:
            if gcm_result.key is not None:
                return gcm_result
            wrapped_failure = gcm_result

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
                    continue
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Failed to decrypt identity key for %s (flip=%s, source=%s): %s",
                        identity.canonical_id,
                        flip_mcu,
                        key_source,
                        err,
                    )
                    continue

                if isinstance(decrypted, (bytes, bytearray)):
                    return IdentityKeyDecryptionResult(
                        bytes(decrypted),
                        {
                            **metadata,
                            "status": "decrypted",
                            "mode": "owner_key",
                            "key_source": key_source,
                            "flip_mcu": flip_mcu,
                        },
                    )

        if wrapped_failure is not None:
            return wrapped_failure

        return IdentityKeyDecryptionResult(None, {**metadata, "status": "failed", "mode": "owner_key"})

    def _unwrap_aes_gcm_identity_key(
        self,
        *,
        identity: DeviceIdentity,
        encrypted_identity_key: bytes,
        key_sources: list[tuple[str, bytes]],
        metadata: dict[str, Any],
    ) -> IdentityKeyDecryptionResult | None:
        """Attempt AES-GCM unwrapping of structured identity key blobs."""
        expected_length = 60  # 12-byte nonce + 32-byte ciphertext + 16-byte tag
        if len(encrypted_identity_key) != expected_length:
            return None

        nonce_length = 12
        tag_length = 16
        ciphertext_length = expected_length - (nonce_length + tag_length)
        if ciphertext_length <= 0:
            return IdentityKeyDecryptionResult(
                None,
                {
                    **metadata,
                    "status": "wrapped_failed",
                    "mode": "aesgcm_envelope",
                    "reason": "invalid_lengths",
                },
            )

        aad_candidates: list[tuple[str, bytes]] = [("empty", b"")]
        if identity.registry_id:
            aad_candidates.append(("registry_id", identity.registry_id.encode("utf-8", "ignore")))
        if identity.canonical_id and identity.canonical_id != identity.registry_id:
            aad_candidates.append(("canonical_id", identity.canonical_id.encode("utf-8", "ignore")))

        cipher_variants = [
            (False, encrypted_identity_key),
            (True, flip_bits(encrypted_identity_key, True)),
        ]

        for key_source, key_bytes in key_sources:
            try:
                aesgcm = AESGCM(key_bytes)
            except Exception:  # pragma: no cover
                continue

            for flip_mcu, ciphertext_blob in cipher_variants:
                nonce = ciphertext_blob[:nonce_length]
                ciphertext = ciphertext_blob[nonce_length:-tag_length]
                tag = ciphertext_blob[-tag_length:]
                payload = ciphertext + tag

                for aad_label, aad in aad_candidates:
                    try:
                        plaintext = aesgcm.decrypt(nonce, payload, aad)
                    except InvalidTag:
                        continue
                    except Exception:  # pragma: no cover
                        continue

                    if len(plaintext) != EIK_LENGTH:
                        continue

                    return IdentityKeyDecryptionResult(
                        plaintext,
                        {
                            **metadata,
                            "status": "decrypted",
                            "mode": "aesgcm_envelope",
                            "key_source": key_source,
                            "aad_label": aad_label,
                            "flip_mcu": flip_mcu,
                            "nonce_length": nonce_length,
                            "tag_length": tag_length,
                        },
                    )

        return IdentityKeyDecryptionResult(
            None,
            {
                **metadata,
                "status": "wrapped_failed",
                "mode": "aesgcm_envelope",
                "aad_candidates": [label for label, _ in aad_candidates],
                "key_sources": [source for source, _ in key_sources],
                "nonce_length": nonce_length,
                "tag_length": tag_length,
            },
        )

    def resolve_eid(self, eid_bytes: bytes) -> EIDMatch | None:  # noqa: PLR0911, PLR0912, PLR0915
        """Resolve a scanned EID to a Home Assistant device registry ID.

        Raw 20-byte and 32-byte payloads are accepted as-is.
        FHNA service data frames are also accepted (EID at offset 8).
        """

        eid_length = len(eid_bytes)
        lookup_candidates: list[bytes] = []

        if eid_length == EID_LENGTH:
            lookup_candidates.append(eid_bytes)
        elif eid_length == MODERN_EID_LENGTH:
            lookup_candidates.append(eid_bytes)
        else:
            if eid_length >= FHNA_LEGACY_SERVICE_TOTAL and eid_bytes[7] == FHNA_FRAME_TYPE_LEGACY:
                lookup_candidates.append(eid_bytes[FHNA_SERVICE_OFFSET:FHNA_LEGACY_SERVICE_TOTAL])
            if eid_length >= FHNA_MODERN_SERVICE_TOTAL and eid_bytes[7] == FHNA_FRAME_TYPE_MODERN:
                lookup_candidates.append(eid_bytes[FHNA_SERVICE_OFFSET:FHNA_MODERN_SERVICE_TOTAL])

            if not lookup_candidates and eid_length >= RAW_HEADER_LENGTH:
                frame_type = eid_bytes[0]
                if frame_type == FHNA_FRAME_TYPE_LEGACY and eid_length >= FHNA_BACKCOMP_LEGACY_TOTAL:
                    lookup_candidates.append(eid_bytes[RAW_HEADER_LENGTH:FHNA_BACKCOMP_LEGACY_TOTAL])
                elif frame_type == FHNA_FRAME_TYPE_MODERN and eid_length >= FHNA_BACKCOMP_MODERN_TOTAL:
                    lookup_candidates.append(eid_bytes[RAW_HEADER_LENGTH:FHNA_BACKCOMP_MODERN_TOTAL])

        if not lookup_candidates:
            _LOGGER.debug("RESOLVER PROBE: Unexpected EID length received (length=%d)", eid_length)
            return None

        eid_prefix = _eid_prefix(lookup_candidates[0])
        _LOGGER.debug(
            "RESOLVER PROBE: Checking sliced EID prefix %s (original_len=%d)",
            eid_prefix,
            eid_length,
        )

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
                    create_task(refresh())
                except Exception:  # pragma: no cover
                    self._pending_refresh = False
                    _LOGGER.debug("RESOLVER REFRESH SCHEDULING FAILED (prefix=%s)", eid_prefix)
            return None

        match: EIDMatch | None = None
        metadata: dict[str, Any] | None = None

        for lookup_key in lookup_candidates:
            match = self._lookup.get(lookup_key)
            if match is None:
                match = self._lookup.get(lookup_key[::-1])
            if match is None:
                continue

            lookup_metadata = self._lookup_metadata
            if isinstance(lookup_metadata, dict):
                metadata = lookup_metadata.get(lookup_key) or lookup_metadata.get(lookup_key[::-1])
            break

        if match is None:
            _LOGGER.debug(
                "RESOLVER MISS: prefix=%s cache_size=%d locks_cached=%d",
                eid_prefix,
                len(self._lookup),
                len(self._device_locks),
            )
            return None

        # Derive and persist a per-device lock from this HIT (best-effort).
        now_unix = int(time.time())
        new_lock = self._derive_lock_from_hit(match, metadata, now_unix=now_unix)
        if new_lock is not None:
            old_lock = self._device_locks.get(match.device_id)
            if old_lock != new_lock:
                self._device_locks[match.device_id] = new_lock
                _LOGGER.info(
                    "[LOCK-ON] %s lock for %s: timebase=%s curve=%s endian=%s rotation_shift=%s reversed=%s variant=%s",
                    "updated" if old_lock is not None else "created",
                    match.device_id,
                    new_lock.timebase,
                    new_lock.curve,
                    new_lock.p256_endian,
                    new_lock.rotation_shift,
                    new_lock.eid_bytes_reversed,
                    new_lock.variant,
                )
                self._schedule_lock_persistence(match.device_id, new_lock)

        self._last_lock_confirmation[match.device_id] = now_unix

        timebase_label = str(metadata.get("timebase")) if isinstance(metadata, dict) else "UNKNOWN"
        variant = metadata.get("variant") if isinstance(metadata, dict) else None
        rotation_timestamp = _coerce_int(metadata.get("rotation_timestamp")) if isinstance(metadata, dict) else None
        anchor_epoch = _coerce_int(metadata.get("anchor_epoch")) if isinstance(metadata, dict) else None
        chosen_offset = _coerce_int(metadata.get("time_offset")) if isinstance(metadata, dict) else match.time_offset

        _LOGGER.info(
            "HIT: device=%s canonical=%s timebase=%s variant=%s reversed=%s "
            "offset=%s rotation=%s anchor=%s eid_prefix=%s",
            match.device_id,
            match.canonical_id,
            timebase_label,
            variant,
            match.is_reversed,
            chosen_offset,
            rotation_timestamp,
            anchor_epoch,
            eid_prefix,
        )
        _LOGGER.debug("Resolved EID to device %s", match.device_id)
        return match

    def _derive_lock_from_hit(
        self,
        match: EIDMatch,
        metadata: dict[str, Any] | None,
        *,
        now_unix: int,
    ) -> _DeviceLock | None:
        """Build a per-device lock from a HIT metadata payload."""
        if not isinstance(metadata, dict):
            return None

        timebase = str(metadata.get("timebase") or "")
        if timebase not in (TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS):
            return None

        anchor_epoch = _coerce_int(metadata.get("anchor_epoch"))
        rotation_timestamp = _coerce_int(metadata.get("rotation_timestamp"))
        if anchor_epoch is None or rotation_timestamp is None:
            return None

        counter_now = now_unix - anchor_epoch
        predicted_rotation = counter_now - (counter_now % ROTATION_PERIOD)
        # rotation_shift is an integer number of rotations relative to prediction.
        rotation_shift = int(round((rotation_timestamp - predicted_rotation) / ROTATION_PERIOD))

        variant = metadata.get("variant")
        variant_str = str(variant) if variant is not None else None
        curve, endian = _infer_curve_and_endian(variant_str)
        if curve is None:
            return None

        previous = self._device_locks.get(match.device_id)
        hit_count = (previous.hit_count + 1) if previous is not None else 1

        return _DeviceLock(
            schema=LOCK_SCHEMA_VERSION,
            timebase=timebase,
            rotation_shift=rotation_shift,
            curve=curve,
            p256_endian=endian,
            eid_bytes_reversed=bool(match.is_reversed),
            variant=variant_str,
            confirmed_at=now_unix,
            hit_count=hit_count,
        )

    def reset_device_offset(self, device_id: str) -> None:
        """Clear cached lock state for a device (including persisted payload when possible)."""
        self._device_locks.pop(device_id, None)
        self._persisted_locks.pop(device_id, None)
        self._last_lock_confirmation.pop(device_id, None)

        # Best-effort removal from device registry.
        try:
            device_reg = dr.async_get(self.hass)
        except Exception:  # pragma: no cover
            return
        entry = device_reg.async_get(device_id)
        if entry is None:
            return
        custom_fields = dict(getattr(entry, "custom_fields", {}) or {})
        if LOCK_CUSTOM_FIELD not in custom_fields:
            return
        custom_fields.pop(LOCK_CUSTOM_FIELD, None)
        try:
            device_reg.async_update_device(device_id, custom_fields=custom_fields)
        except Exception:  # pragma: no cover
            pass

    def _schedule_lock_persistence(self, device_id: str, lock: _DeviceLock) -> None:
        """Persist lock metadata for restart resilience (best-effort)."""
        payload = _serialize_lock(lock)
        if self._persisted_locks.get(device_id) == payload:
            return

        async def _async_store() -> None:
            try:
                device_reg = dr.async_get(self.hass)
            except Exception:  # pragma: no cover
                return
            if device_reg is None:
                return
            entry = device_reg.async_get(device_id)
            if entry is None:
                return

            custom_fields = getattr(entry, "custom_fields", None)
            if custom_fields is None:
                # Registry/version does not support custom fields.
                return

            merged = dict(custom_fields or {})
            merged[LOCK_CUSTOM_FIELD] = payload
            try:
                device_reg.async_update_device(device_id, custom_fields=merged)
            except Exception:  # pragma: no cover
                return

            self._persisted_locks[device_id] = payload

        create_task = getattr(self.hass, "async_create_task", asyncio.create_task)
        try:
            create_task(_async_store())
        except Exception:  # pragma: no cover
            asyncio.create_task(_async_store())

    async def async_trigger_immediate_refresh(self) -> None:
        """Force an immediate re-calculation of EID candidates."""
        if self._refresh_lock.locked():
            _LOGGER.debug("Immediate refresh requested but resolver is busy; skipping.")
            return
        _LOGGER.info("EVENT-DRIVEN: Critical Key Update detected. Triggering immediate EID recalculation.")
        await self._refresh_cache()

    def get_resolved_eid(self, eid_bytes: bytes) -> str | None:
        """Backward compatible convenience wrapper for resolve_eid."""
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
            except Exception as err:  # pragma: no cover
                _LOGGER.debug("Failed to cancel alignment timer: %s", err)

        if self._unsub_interval is not None:
            unsub = self._unsub_interval
            self._unsub_interval = None
            try:
                if callable(unsub):
                    unsub()
                elif asyncio.iscoroutine(unsub):
                    unsub.close()
            except Exception as err:  # pragma: no cover
                _LOGGER.debug("Failed to cancel refresh interval: %s", err)

        self._lookup.clear()
        self._lookup_metadata.clear()
        self._device_locks.clear()
        self._persisted_locks.clear()
        self._last_lock_confirmation.clear()


@runtime_checkable
class _IdentityProvider(Protocol):
    """Interface for coordinator-like objects that expose device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]:
        """Return eligible device identities for EID resolution."""

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
NARROW_SCAN_RANGE: tuple[int, ...] = (0, -1, 1, -2, 2, -3, 3)
REL_DEEP_SCAN_DENSE_RADIUS = 96
REL_DEEP_SCAN_MAX_DRIFT = 180
REL_DEEP_SCAN_SPARSE_STEP = 4
LOCK_CUSTOM_FIELD = "eid_timebase_lock"
DEBUG_LOG_LIMIT = 50
PROVISIONING_WARN_COOLDOWN = 3600
FUTURE_ANCHOR_MAX_DRIFT = 86400
ENABLE_P256_TAIL_TRUNCATION = True
_TAIL_TRUNCATION_LOG_FLAG: list[bool] = [False]
_LE_GENERATION_WARN_FLAG: list[bool] = [False]


class TimebaseLabel:
    """Timebase candidates used when generating EID windows."""

    ABSOLUTE = "ABSOLUTE"
    REL_PAIR = "REL_PAIR"
    REL_SECRETS = "REL_SECRETS"


@dataclass(slots=True, frozen=True)
class _TimebaseCandidate:
    """Candidate clock anchor used during EID generation."""

    label: str
    reference_time: int
    anchor_epoch: int | None


@dataclass(slots=True)
class _TimebaseLock:
    """Winning timebase metadata captured after a successful lock-on."""

    label: str
    anchor_epoch: int | None
    rotation_timestamp: int
    offset: int
    variant: str | None = None


def iter_rotation_windows(
    target_time: int,
    *,
    rotation_period: int,
    window_range: Iterable[int],
    include_neighbors: bool,
    allow_negative: bool = False,
) -> tuple[int, ...]:
    """Return rotation-aligned timestamps for cache population.

    This helper aligns ``target_time`` to the rotation boundary and walks the
    provided ``window_range`` to build candidate windows. When
    ``include_neighbors`` is True, each aligned window is expanded to include
    the immediately previous and next rotation boundaries so cached lookups can
    tolerate known drift offsets.
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


def _clamp_and_mask_u32(delta_seconds: int) -> int:
    """Mask any delta to the uint32 counter space without clamping.

    The name is retained for backward compatibility with fixtures that refer
    to the helper, but the implementation now preserves negative deltas by
    wrapping them into the uint32 space so drift-heavy anchors still surface in
    diagnostics.
    """

    return _mask_u32(delta_seconds)


def _parse_persisted_lock(raw: Mapping[str, Any]) -> tuple[_TimebaseLock, bool] | None:
    """Return a parsed lock payload from registry custom fields when present."""

    label_raw = raw.get("label")
    rotation_raw = raw.get("rotation_timestamp")
    offset_raw = raw.get("time_offset")
    anchor_raw = raw.get("anchor_epoch")
    variant_raw = raw.get("variant")

    try:
        label = str(label_raw)
    except Exception:  # pragma: no cover - defensive
        return None

    rotation_timestamp = _coerce_int(rotation_raw)
    offset = _coerce_int(offset_raw) or 0
    anchor_epoch = _coerce_int(anchor_raw)

    variant: str | None
    try:
        variant = str(variant_raw) if variant_raw is not None else None
    except Exception:  # pragma: no cover - defensive
        variant = None

    is_reversed = bool(raw.get("is_reversed", False))

    if rotation_timestamp is None:
        return None

    return _TimebaseLock(
        label=label,
        anchor_epoch=anchor_epoch,
        rotation_timestamp=rotation_timestamp,
        offset=offset,
        variant=variant,
    ), is_reversed


def _iter_debug_timebases(anchors: Any, *, now: int) -> list[_TimebaseCandidate]:
    """Parse optional debug anchors into timebase candidates.

    The resolver tolerates a handful of shapes when the server returns anchor
    diagnostics. Known shapes include a mapping with an ``anchors`` list of
    dicts or a single mapping containing ``anchor_epoch``/``time_offset``
    fields. Unknown shapes are ignored while still being preserved in
    diagnostics so future adjustments can refine the parsing strategy.
    """

    if isinstance(anchors, Mapping):
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
            reference_time = _mask_u32(now - anchor_epoch + offset_seconds)
            candidates.append(
                _TimebaseCandidate(
                    label=label,
                    reference_time=reference_time,
                    anchor_epoch=anchor_epoch + offset_seconds,
                )
            )

        return candidates

    return []


def _compute_provisioning_counter(
    identity: DeviceIdentity, *, now: int
) -> tuple[int, str | None, int | None]:
    """Return the provisioning counter used as ``ts_bytes`` for EID derivation.

    The EID PRF expects ``ts_bytes`` to represent **seconds since provisioning**
    instead of Unix time. We derive the counter from pairing or secrets
    creation anchors when available and mask to 32 bits to match tracker-side
    rollover behavior.
    """

    secrets_anchor = _coerce_int(identity.secrets_creation_date)
    pair_anchor = _coerce_int(identity.pair_date)

    selected_label: str | None = None
    selected_anchor: int | None = None

    if secrets_anchor is not None and (
        pair_anchor is None or secrets_anchor >= pair_anchor
    ):
        selected_label = "secrets_creation_date"
        selected_anchor = secrets_anchor
    elif pair_anchor is not None:
        selected_label = "pair_date"
        selected_anchor = pair_anchor

    if selected_label is not None and selected_anchor is not None:
        counter = now - selected_anchor
        _LOGGER.debug(
            "Anchor Selected: Type=%s Value=%s Counter=%s (pair_date=%s secrets_creation_date=%s)",
            selected_label,
            selected_anchor,
            counter,
            pair_anchor,
            secrets_anchor,
        )
        return counter, selected_label, selected_anchor

    fallback_counter = now
    _LOGGER.debug(
        "Anchor Selected: Type=%s Value=%s Counter=%s",
        "unix_time",
        now,
        fallback_counter,
    )
    return fallback_counter, "unix_time", now


def _build_timebase_candidates(
    identity: DeviceIdentity,
    *,
    now_unix: int,
) -> list[_TimebaseCandidate]:
    """Return candidate timebases derived from identity anchors."""

    candidates: list[_TimebaseCandidate] = []
    absolute_candidate = _TimebaseCandidate(
        TimebaseLabel.ABSOLUTE,
        reference_time=_mask_u32(now_unix),
        anchor_epoch=None,
    )

    def _build_anchor_candidate(
        label: str, anchor_epoch: int | None
    ) -> _TimebaseCandidate | None:
        anchor_value = _coerce_int(anchor_epoch)
        if anchor_value is None:
            return None

        anchor_age = now_unix - anchor_value
        if anchor_age < -FUTURE_ANCHOR_MAX_DRIFT:
            _LOGGER.debug(
                "Skipping %s timebase for %s due to excessive future skew (%s)",
                label,
                identity.canonical_id,
                anchor_age,
            )
            return None

        return _TimebaseCandidate(
            label=label,
            reference_time=anchor_age,
            anchor_epoch=anchor_value,
        )

    candidates.append(absolute_candidate)

    secrets_candidate = _build_anchor_candidate(
        TimebaseLabel.REL_SECRETS, identity.secrets_creation_date
    )
    if secrets_candidate is not None:
        candidates.append(secrets_candidate)

    pair_candidate = _build_anchor_candidate(TimebaseLabel.REL_PAIR, identity.pair_date)
    if pair_candidate is not None:
        candidates.append(pair_candidate)

    if identity.time_anchors_debug is not None:
        candidates.extend(
            _iter_debug_timebases(identity.time_anchors_debug, now=now_unix)
        )

    unique: dict[tuple[str, int, int | None], _TimebaseCandidate] = {}
    for candidate in candidates:
        unique[(candidate.label, candidate.reference_time, candidate.anchor_epoch)] = (
            candidate
        )
    return list(unique.values())


@lru_cache(maxsize=512)
def _cached_candidates(identity_key: bytes, timestamp: int) -> tuple[EidCandidate, ...]:
    """Return cached EID candidates for an identity key and window timestamp.

    ``timestamp`` must be the provisioning counter (seconds since pairing), not
    Unix time, to mirror the tracker-side EID PRF input.
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

        # ------------------------------------------------------------------
        # CRITICAL: MOTO TAG / CHIPOLO SUPPORT (Little Endian)
        # Moto Tag hardware derives the P-256 scalar using little-endian
        # byte order. Without these LE variants, the resolver will never
        # match Moto Tag (or similar Chipolo-based) trackers. Keep the
        # `_le_` candidates in lockstep with the big-endian set above.
        # ------------------------------------------------------------------
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


class IdentityKeyDecryptionResult(NamedTuple):
    """Result from attempting to decrypt a device identity key."""

    key: bytes | None
    metadata: dict[str, Any]


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
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(
        init=False, default_factory=dict
    )
    _known_offsets: dict[str, int] = field(default_factory=dict)
    _known_endianness: dict[str, bool] = field(default_factory=dict)
    _decryption_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    _known_timebases: dict[str, _TimebaseLock] = field(default_factory=dict)
    _persisted_locks: dict[str, dict[str, Any]] = field(default_factory=dict)
    _provisioning_warn_at: dict[str, float] = field(default_factory=dict)
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

    async def _refresh_cache(self) -> None:  # noqa: PLR0912, PLR0915 - iterative window search
        """Rebuild the EID lookup table for enabled, non-ignored devices."""

        cache_clear = getattr(_cached_candidates, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
        if not hasattr(self, "_known_timebases"):
            self._known_timebases = {}
        if not hasattr(self, "_lookup_metadata"):
            self._lookup_metadata = {}
        if not hasattr(self, "_decryption_status"):
            self._decryption_status = {}
        if not hasattr(self, "_persisted_locks"):
            self._persisted_locks = {}
        if not hasattr(self, "_provisioning_warn_at"):
            self._provisioning_warn_at = {}
        identities: list[DeviceIdentity] = await self._collect_device_secrets()
        _LOGGER.debug(
            "Resolver received %d identities from coordinator", len(identities)
        )
        lookup: dict[bytes, EIDMatch] = {}
        lookup_metadata: dict[bytes, dict[str, Any]] = {}
        debug_log_state: dict[str, dict[str, Any]] = {}
        now_unix = int(time.time())

        try:
            device_reg = dr.async_get(self.hass)
        except Exception:  # pragma: no cover - defensive stub fallback
            device_reg = None

        if device_reg is not None:
            for identity in identities:
                registry_id = identity.registry_id
                if not registry_id:
                    continue

                if (
                    registry_id in self._known_timebases
                    and registry_id in self._persisted_locks
                ):
                    continue

                entry = device_reg.async_get(registry_id)
                custom_fields = getattr(entry, "custom_fields", None) if entry else None
                if not isinstance(custom_fields, Mapping):
                    continue

                lock_payload = custom_fields.get(LOCK_CUSTOM_FIELD)
                if not isinstance(lock_payload, Mapping):
                    continue

                parsed = _parse_persisted_lock(lock_payload)
                if parsed is None:
                    continue

                persisted_lock, is_reversed = parsed
                self._known_timebases.setdefault(registry_id, persisted_lock)
                self._known_offsets.setdefault(registry_id, persisted_lock.offset)
                self._known_endianness.setdefault(registry_id, is_reversed)
                self._persisted_locks[registry_id] = dict(lock_payload)

        first_identity_id: str | None = None

        for identity in identities:
            if (
                not identity.config_entry_id
                or identity.identity_key is None
                or identity.registry_id is None
            ):
                _LOGGER.debug(
                    "Skipping identity with missing data (entry=%s, registry=%s)",
                    identity.config_entry_id,
                    identity.registry_id,
                )
                continue

            if first_identity_id is None:
                first_identity_id = identity.canonical_id

            config_entry_id = identity.config_entry_id
            registry_id = identity.registry_id
            identity_key_bytes = bytes(identity.identity_key)

            active_lock: _TimebaseLock | None = self._known_timebases.get(registry_id)
            known_offset = (
                active_lock.offset
                if active_lock is not None
                else self._known_offsets.get(registry_id)
            )
            known_endianness = self._known_endianness.get(registry_id, False)
            requested_timebases: set[str] = set()
            recorded_timebases: set[str] = set()
            rel_candidates: list[_TimebaseCandidate] = []
            should_log_debug_dump = identity.canonical_id.endswith("d329") or (
                first_identity_id is not None
                and identity.canonical_id == first_identity_id
            )
            device_debug_state = debug_log_state.setdefault(
                registry_id, {"count": 0, "seen": set()}
            )

            status = self._decryption_status.get(identity.canonical_id, {})
            skip_deep_scan = False
            if status:
                skip_deep_scan = status.get("status") not in {
                    "decrypted",
                    "wrapped_decrypted",
                }

            def _register_with_metadata(
                eid_value: bytes, reversed_flag: bool, metadata_payload: dict[str, Any]
            ) -> None:
                match = EIDMatch(
                    device_id=registry_id,
                    config_entry_id=config_entry_id,
                    canonical_id=identity.canonical_id,
                    time_offset=metadata_payload["time_offset"],
                    is_reversed=reversed_flag,
                )
                existing = lookup.get(eid_value)
                existing_offset = existing.time_offset if existing is not None else None
                timebase_label = metadata_payload.get("timebase")
                should_force = (
                    timebase_label
                    in {
                        TimebaseLabel.REL_PAIR,
                        TimebaseLabel.REL_SECRETS,
                    }
                    and timebase_label not in recorded_timebases
                )

                existing_metadata = lookup_metadata.get(eid_value)
                history: list[dict[str, Any]] = []
                if isinstance(existing_metadata, dict):
                    prior = existing_metadata.get("timebases")
                    if isinstance(prior, list):
                        history = list(prior)
                    else:
                        history = [existing_metadata]

                if metadata_payload not in history:
                    history.append(metadata_payload)

                if (
                    should_force
                    or existing is None
                    or existing_offset is None
                    or abs(match.time_offset) < abs(existing_offset)
                ):
                    lookup[eid_value] = match
                    lookup_metadata[eid_value] = {
                        **metadata_payload,
                        "is_reversed": reversed_flag,
                        "timebases": history,
                    }
                    recorded_timebases.add(str(metadata_payload.get("timebase")))
                elif isinstance(existing_metadata, dict):
                    existing_metadata["timebases"] = history
                    lookup_metadata[eid_value] = existing_metadata

            base_candidates = _build_timebase_candidates(
                identity,
                now_unix=now_unix,
            )

            timebase_candidates = (
                [
                    candidate
                    for candidate in base_candidates
                    if active_lock and candidate.label == active_lock.label
                ]
                if active_lock is not None
                else base_candidates
            )

            requested_timebases.update(
                candidate.label for candidate in timebase_candidates
            )

            for candidate in timebase_candidates:
                if candidate.label == TimebaseLabel.REL_PAIR:
                    rel_candidates.append(candidate)

                lock_for_candidate = (
                    active_lock is not None
                    and active_lock.label == candidate.label
                    and active_lock.rotation_timestamp is not None
                    and candidate.label == TimebaseLabel.ABSOLUTE
                    and abs(active_lock.offset) >= ROTATION_PERIOD
                )

                local_search_offset = known_offset if not lock_for_candidate else 0
                if (
                    local_search_offset is None
                    and active_lock is not None
                    and active_lock.rotation_timestamp is not None
                    and active_lock.label == candidate.label
                ):
                    rotations_since_lock = round(
                        (candidate.reference_time - active_lock.rotation_timestamp)
                        / ROTATION_PERIOD
                    )
                    aligned_rotation = active_lock.rotation_timestamp + (
                        rotations_since_lock * ROTATION_PERIOD
                    )
                    local_search_offset = aligned_rotation - candidate.reference_time

                passes: list[tuple[Iterable[int], bool]]
                anchor_age: int | None = None
                if candidate.anchor_epoch is not None:
                    try:
                        anchor_age = now_unix - int(candidate.anchor_epoch)
                    except (TypeError, ValueError):
                        anchor_age = None

                reference_value = candidate.reference_time
                offset_reference = reference_value
                allow_negative = bool(
                    candidate.label != TimebaseLabel.ABSOLUTE
                    and anchor_age is not None
                    and anchor_age < 0
                )

                if lock_for_candidate and active_lock is not None:
                    passes = [((-1, 0, 1), False)]
                    reference_value = active_lock.rotation_timestamp
                    offset_reference = active_lock.rotation_timestamp
                    local_search_offset = 0
                    allow_negative = False
                elif local_search_offset is not None:
                    passes = [(range(0, 1), True)]
                else:
                    passes = [(NARROW_SCAN_RANGE, False)]
                    if not skip_deep_scan:
                        passes.append(
                            (
                                range(
                                    -REL_DEEP_SCAN_DENSE_RADIUS,
                                    REL_DEEP_SCAN_DENSE_RADIUS + 1,
                                ),
                                False,
                            )
                        )
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
                            passes.append(((tuple(sparse_negative) + tuple(sparse_positive)), False))

                for window_range, include_neighbors in passes:
                    local_window_range = window_range
                    local_include_neighbors = include_neighbors
                    base_target_time = offset_reference

                    counter_label = (
                        f"counter:{candidate.label}"
                        if candidate.label != TimebaseLabel.ABSOLUTE
                        else "counter"
                    )

                    if (
                        not lock_for_candidate
                        and active_lock is not None
                        and active_lock.label == candidate.label
                        and active_lock.rotation_timestamp is not None
                    ):
                        base_target_time = active_lock.rotation_timestamp
                        offset_reference = active_lock.rotation_timestamp - (
                            active_lock.offset or 0
                        )
                        local_search_offset = 0
                        local_window_range = range(0, 3)
                        local_include_neighbors = False

                    elif (
                        local_search_offset is None
                        and active_lock is not None
                        and active_lock.rotation_timestamp is not None
                        and active_lock.label == candidate.label
                    ):
                        rotations_since_lock = round(
                            (candidate.reference_time - active_lock.rotation_timestamp)
                            / ROTATION_PERIOD
                        )
                        aligned_rotation = active_lock.rotation_timestamp + (
                            rotations_since_lock * ROTATION_PERIOD
                        )
                        local_search_offset = (
                            aligned_rotation - candidate.reference_time
                        )

                    target_time = (
                        base_target_time + local_search_offset
                        if local_search_offset is not None
                        else base_target_time
                    )

                    is_reversed = local_search_offset is not None and known_endianness

                    rotation_windows = iter_rotation_windows(
                        target_time,
                        rotation_period=ROTATION_PERIOD,
                        window_range=local_window_range,
                        include_neighbors=local_include_neighbors,
                        allow_negative=allow_negative,
                    )

                    for window_timestamp in rotation_windows:
                        time_offset = window_timestamp - candidate.reference_time

                        masked_timestamp = _mask_u32(window_timestamp)
                        rotation_timestamp = window_timestamp

                        try:
                            candidates = _cached_candidates(
                                identity_key_bytes, masked_timestamp
                            )
                        except Exception as err:  # noqa: BLE001 - defensive guard
                            _LOGGER.debug(
                                "Failed to generate EID candidates (%s) for %s at %s: %s",
                                counter_label,
                                identity.canonical_id,
                                window_timestamp,
                                err,
                            )
                            continue

                        for eid_candidate in candidates:
                            metadata = {
                                "timebase": candidate.label,
                                "anchor_epoch": candidate.anchor_epoch,
                                "rotation_timestamp": rotation_timestamp,
                                "masked_rotation_timestamp": masked_timestamp,
                                "time_offset": time_offset,
                                "timestamp_basis": counter_label,
                                "variant": eid_candidate.name,
                            }

                            if (
                                should_log_debug_dump
                                and device_debug_state["count"] < DEBUG_LOG_LIMIT
                            ):
                                message_key = (
                                    candidate.label,
                                    eid_candidate.name,
                                    masked_timestamp,
                                )
                                if message_key not in device_debug_state["seen"]:
                                    device_debug_state["seen"].add(message_key)
                                    device_debug_state["count"] += 1
                                    _LOGGER.debug(
                                        "DEBUG DUMP: Device=%s Timebase=%s Variant=%s Basis=%s Anchor=%s Offset=%s TS=%s EID_PREFIX=%s",
                                        identity.canonical_id,
                                        candidate.label,
                                        eid_candidate.name,
                                        counter_label,
                                        candidate.anchor_epoch,
                                        time_offset,
                                        window_timestamp,
                                        _eid_prefix(eid_candidate.eid),
                                    )

                            local_known_offset = known_offset
                            if local_known_offset is None:
                                _register_with_metadata(
                                    eid_candidate.eid, False, metadata
                                )
                                _register_with_metadata(
                                    eid_candidate.eid[::-1], True, metadata
                                )
                            else:
                                eid_bytes = (
                                    eid_candidate.eid[::-1]
                                    if is_reversed
                                    else eid_candidate.eid
                                )
                                metadata = {
                                    **metadata,
                                    "known_offset": local_known_offset,
                                }
                                _register_with_metadata(
                                    eid_bytes, is_reversed, metadata
                                )

            if (
                TimebaseLabel.REL_PAIR in requested_timebases
                and TimebaseLabel.REL_PAIR not in recorded_timebases
            ):
                rel_candidate = next((candidate for candidate in rel_candidates), None)
                if rel_candidate is not None:
                    fallback_rotation = rel_candidate.reference_time - (
                        rel_candidate.reference_time % ROTATION_PERIOD
                    )
                    try:
                        fallback_candidates = _cached_candidates(
                            identity_key_bytes, fallback_rotation
                        )
                    except Exception as err:  # noqa: BLE001 - defensive guard
                        _LOGGER.debug(
                            "Failed to generate fallback REL_PAIR candidates for %s: %s",
                            identity.canonical_id,
                            err,
                        )
                    else:
                        for eid_candidate in fallback_candidates[:1]:
                            fallback_metadata = {
                                "timebase": TimebaseLabel.REL_PAIR,
                                "anchor_epoch": rel_candidate.anchor_epoch,
                                "rotation_timestamp": fallback_rotation,
                                "time_offset": fallback_rotation
                                - rel_candidate.reference_time,
                                "timestamp_basis": "counter:REL_PAIR",
                                "variant": eid_candidate.name,
                            }
                            _register_with_metadata(
                                eid_candidate.eid, False, fallback_metadata
                            )
                            _register_with_metadata(
                                eid_candidate.eid[::-1], True, fallback_metadata
                            )
                            break

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
                result = await self._try_decrypt_identity_key(identity, cache=cache)
                self._decryption_status[identity.canonical_id] = result.metadata
                if result.key is None:
                    _LOGGER.debug(
                        "Decryption returned None for %s - skipping (status=%s)",
                        identity.canonical_id,
                        result.metadata.get("status"),
                    )
                    continue
                normalized.append(replace(identity, identity_key=result.key))
                continue

            normalized.append(identity)

        return normalized

    async def _try_decrypt_identity_key(  # noqa: PLR0912 - branching for decryption attempts
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
                {
                    "status": "skipped",
                    "reason": "missing_cache_or_ciphertext",
                },
            )

        metadata: dict[str, Any] = {
            "status": "pending",
            "ciphertext_length": len(identity.encrypted_identity_key),
        }
        try:
            owner_key_info: OwnerKeyInfo = await async_get_owner_key(cache=cache)
        except Exception as err:  # noqa: BLE001 - defensive
            _LOGGER.debug(
                "Failed to retrieve owner key for %s: %s",
                identity.canonical_id,
                err,
            )
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

        key_sources: list[tuple[str, bytes]] = [("owner", owner_key_info.key)]
        try:
            shared_key = await async_get_shared_key(cache=cache)
        except Exception as err:  # noqa: BLE001 - defensive
            _LOGGER.debug(
                "Shared key unavailable for %s: %s", identity.canonical_id, err
            )
        else:
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
                    decrypted = await asyncio.to_thread(
                        decrypt_eik, wrapping_key, candidate_key
                    )
                except InvalidTag as err:
                    _LOGGER.debug(
                        "Identity key decrypt failed for %s with flip=%s (source=%s): %s",
                        identity.canonical_id,
                        flip_mcu,
                        key_source,
                        err,
                    )
                    continue
                except Exception as err:  # noqa: BLE001 - defensive
                    _LOGGER.debug(
                        "Failed to decrypt identity key for %s (owner_key_version=%s, flip=%s, source=%s): %s",
                        identity.canonical_id,
                        identity.owner_key_version,
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
                            "mode": "owner_key",  # compatibility alias
                            "key_source": key_source,
                            "flip_mcu": flip_mcu,
                        },
                    )

                _LOGGER.debug(
                    "Decryption returned non-bytes result for %s (flip=%s, source=%s)",
                    identity.canonical_id,
                    flip_mcu,
                    key_source,
                )

        if wrapped_failure is not None:
            return wrapped_failure

        return IdentityKeyDecryptionResult(
            None,
            {**metadata, "status": "failed", "mode": "owner_key"},
        )

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
            aad_candidates.append(
                ("registry_id", identity.registry_id.encode("utf-8", "ignore"))
            )
        if identity.canonical_id and identity.canonical_id != identity.registry_id:
            aad_candidates.append(
                (
                    "canonical_id",
                    identity.canonical_id.encode("utf-8", "ignore"),
                )
            )

        cipher_variants = [
            (False, encrypted_identity_key),
            (True, flip_bits(encrypted_identity_key, True)),
        ]

        for key_source, key_bytes in key_sources:
            try:
                aesgcm = AESGCM(key_bytes)
            except Exception as err:  # noqa: BLE001 - defensive
                _LOGGER.debug(
                    "Unable to initialize AESGCM for %s (source=%s): %s",
                    identity.canonical_id,
                    key_source,
                    err,
                )
                continue

            for flip_mcu, ciphertext_blob in cipher_variants:
                nonce = ciphertext_blob[:nonce_length]
                ciphertext = ciphertext_blob[nonce_length:-tag_length]
                tag = ciphertext_blob[-tag_length:]
                payload = ciphertext + tag

                for aad_label, aad in aad_candidates:
                    try:
                        plaintext = aesgcm.decrypt(nonce, payload, aad)
                    except InvalidTag as err:
                        _LOGGER.debug(
                            "AES-GCM unwrap failed for %s (source=%s, flip=%s, aad=%s): %s",
                            identity.canonical_id,
                            key_source,
                            flip_mcu,
                            aad_label,
                            err,
                        )
                        continue
                    except Exception as err:  # noqa: BLE001 - defensive
                        _LOGGER.debug(
                            "AES-GCM unwrap error for %s (source=%s, flip=%s, aad=%s): %s",
                            identity.canonical_id,
                            key_source,
                            flip_mcu,
                            aad_label,
                            err,
                        )
                        continue

                    if len(plaintext) != EIK_LENGTH:
                        _LOGGER.debug(
                            "AES-GCM unwrap produced unexpected length for %s (source=%s, flip=%s, aad=%s, length=%s)",
                            identity.canonical_id,
                            key_source,
                            flip_mcu,
                            aad_label,
                            len(plaintext),
                        )
                        continue

                    _LOGGER.debug(
                        "AES-GCM unwrap succeeded for %s (source=%s, flip=%s, aad=%s)",
                        identity.canonical_id,
                        key_source,
                        flip_mcu,
                        aad_label,
                    )
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

        FHNA Find My Device Network frames encode the frame type at octet 7.
        Legacy (``0x40``) frames carry a 20-byte EID at octets 8–27; modern
        (``0x41``) frames carry a 32-byte EID at octets 8–39. Optional hashed
        flags follow the EID and are ignored for lookup. Raw 20-byte and
        32-byte payloads are accepted as-is, and a backward compatible slice
        attempts to parse payloads that start with ``0x40``/``0x41`` when the
        length matches the legacy one-byte header layout.

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

        eid_length = len(eid_bytes)
        lookup_candidates: list[bytes] = []

        if eid_length == EID_LENGTH:
            lookup_candidates.append(eid_bytes)
        elif eid_length == MODERN_EID_LENGTH:
            lookup_candidates.append(eid_bytes)
        else:
            if (
                eid_length >= FHNA_LEGACY_SERVICE_TOTAL
                and eid_bytes[7] == FHNA_FRAME_TYPE_LEGACY
            ):
                lookup_candidates.append(
                    eid_bytes[FHNA_SERVICE_OFFSET:FHNA_LEGACY_SERVICE_TOTAL]
                )
            if (
                eid_length >= FHNA_MODERN_SERVICE_TOTAL
                and eid_bytes[7] == FHNA_FRAME_TYPE_MODERN
            ):
                lookup_candidates.append(
                    eid_bytes[FHNA_SERVICE_OFFSET:FHNA_MODERN_SERVICE_TOTAL]
                )

            if not lookup_candidates and eid_length >= RAW_HEADER_LENGTH:
                frame_type = eid_bytes[0]
                if (
                    frame_type == FHNA_FRAME_TYPE_LEGACY
                    and eid_length >= FHNA_BACKCOMP_LEGACY_TOTAL
                ):
                    lookup_candidates.append(
                        eid_bytes[RAW_HEADER_LENGTH:FHNA_BACKCOMP_LEGACY_TOTAL]
                    )
                elif (
                    frame_type == FHNA_FRAME_TYPE_MODERN
                    and eid_length >= FHNA_BACKCOMP_MODERN_TOTAL
                ):
                    lookup_candidates.append(
                        eid_bytes[RAW_HEADER_LENGTH:FHNA_BACKCOMP_MODERN_TOTAL]
                    )

        if not lookup_candidates:
            _LOGGER.debug(
                "RESOLVER PROBE: Unexpected EID length received (length=%d)", eid_length
            )
            return None

        if not lookup_candidates:
            _LOGGER.debug(
                "RESOLVER PROBE: No candidates produced for length=%d", eid_length
            )
            return None

        eid_prefix = _eid_prefix(lookup_candidates[0])
        _LOGGER.debug(
            "RESOLVER PROBE: Checking Sliced EID prefix %s (Original Len: %d)",
            eid_prefix,
            len(eid_bytes),
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

            _LOGGER.debug(
                "RESOLVER NOT READY: empty cache; scheduling refresh for prefix=%s",
                eid_prefix,
            )
            refresh = getattr(self, "async_refresh", None)
            create_task = getattr(self.hass, "async_create_task", None)
            if callable(refresh) and callable(create_task):
                self._pending_refresh = True
                try:
                    create_task(refresh())
                except Exception:  # pragma: no cover - defensive
                    self._pending_refresh = False
                    _LOGGER.debug(
                        "RESOLVER REFRESH SCHEDULING FAILED (prefix=%s)", eid_prefix
                    )
            return None

        match: EIDMatch | None = None
        metadata: dict[str, Any] | None = None

        for lookup_key in lookup_candidates:
            match = self._lookup.get(lookup_key)
            if match is None:
                match = self._lookup.get(lookup_key[::-1])
            if match is None:
                continue

            lookup_metadata = getattr(self, "_lookup_metadata", None)
            if isinstance(lookup_metadata, dict):
                metadata = lookup_metadata.get(lookup_key) or lookup_metadata.get(
                    lookup_key[::-1]
                )
            break

        if match is None:
            known_timebases = getattr(self, "_known_timebases", {})
            _LOGGER.debug(
                "RESOLVER MISS: prefix=%s cache_size=%d locks_cached=%d",
                eid_prefix,
                len(self._lookup),
                len(known_timebases),
            )
            return None

        previous_offset = self._known_offsets.get(match.device_id)
        previous_endianness = self._known_endianness.get(match.device_id)

        timebase_label = TimebaseLabel.ABSOLUTE
        rotation_timestamp: int | None = None

        if metadata is not None:
            anchor_epoch = metadata.get("anchor_epoch")
            rotation_ts_raw = metadata.get("rotation_timestamp")
            timebase_label = str(metadata.get("timebase", TimebaseLabel.ABSOLUTE))
            offset_override = metadata.get("time_offset", match.time_offset)
            if rotation_ts_raw is None:
                rotation_timestamp = None
            else:
                try:
                    rotation_timestamp = int(rotation_ts_raw)
                except (TypeError, ValueError):
                    rotation_timestamp = None
            try:
                anchor_ts = int(anchor_epoch) if anchor_epoch is not None else None
            except (TypeError, ValueError):
                anchor_ts = None
            try:
                offset_value = int(offset_override)
            except (TypeError, ValueError):
                offset_value = match.time_offset
            if rotation_timestamp is not None:
                self._known_timebases[match.device_id] = _TimebaseLock(
                    label=timebase_label,
                    anchor_epoch=anchor_ts,
                    rotation_timestamp=rotation_timestamp,
                    offset=offset_value,
                )

        if metadata is not None:
            try:
                chosen_offset = int(metadata.get("time_offset", match.time_offset))
            except (TypeError, ValueError):
                chosen_offset = match.time_offset
        else:
            chosen_offset = match.time_offset

        if previous_offset != chosen_offset or previous_endianness != match.is_reversed:
            _LOGGER.info(
                "Locked on to device %s! Applying Time Offset: %ss, Reverse: %s (Timebase=%s)",
                match.device_id,
                chosen_offset,
                match.is_reversed,
                timebase_label,
            )

        variant = metadata.get("variant") if metadata is not None else None
        anchor_epoch = anchor_ts if metadata is not None else None

        active_lock = self._known_timebases.get(match.device_id)
        if (
            timebase_label == TimebaseLabel.ABSOLUTE
            and rotation_timestamp is not None
            and abs(chosen_offset) >= ROTATION_PERIOD
        ):
            locked_timebase = _TimebaseLock(
                label=timebase_label,
                anchor_epoch=anchor_epoch,
                rotation_timestamp=rotation_timestamp,
                offset=chosen_offset,
                variant=variant,
            )
            if locked_timebase != active_lock:
                self._known_timebases[match.device_id] = locked_timebase
                lock_state = "updated" if active_lock is not None else "created"
                _LOGGER.info(
                    "[LOCK-ON] %s absolute timebase for %s (offset=%s, variant=%s, rotation=%s)",
                    lock_state,
                    match.device_id,
                    chosen_offset,
                    variant,
                    rotation_timestamp,
                )

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

        self._known_offsets[match.device_id] = chosen_offset
        self._known_endianness[match.device_id] = match.is_reversed
        self._schedule_lock_persistence(match, metadata)
        _LOGGER.debug("Resolved EID to device %s", match.device_id)
        return match

    def reset_device_offset(self, device_id: str) -> None:
        """Clear cached time offset and endianness for a device."""

        self._known_offsets.pop(device_id, None)
        self._known_endianness.pop(device_id, None)

    def _schedule_lock_persistence(
        self, match: EIDMatch, metadata: dict[str, Any] | None
    ) -> None:
        """Persist lock metadata for restart resilience."""

        if metadata is None:
            return

        rotation_raw = metadata.get("rotation_timestamp")
        timebase_label = metadata.get("timebase")
        anchor_raw = metadata.get("anchor_epoch")
        if not hasattr(self, "_persisted_locks"):
            self._persisted_locks = {}
        if rotation_raw is None:
            return
        try:
            rotation_timestamp = int(rotation_raw)
        except (TypeError, ValueError):
            return

        try:
            time_offset = int(metadata.get("time_offset", 0))
        except (TypeError, ValueError):
            time_offset = 0

        try:
            anchor_epoch = int(anchor_raw) if anchor_raw is not None else None
        except (TypeError, ValueError):
            anchor_epoch = None

        try:
            variant = (
                str(metadata.get("variant"))
                if metadata.get("variant") is not None
                else None
            )
        except Exception:
            variant = None

        lock_payload = {
            "label": timebase_label,
            "anchor_epoch": anchor_epoch,
            "rotation_timestamp": rotation_timestamp,
            "time_offset": time_offset,
            "is_reversed": match.is_reversed,
            "variant": variant,
        }

        if self._persisted_locks.get(match.device_id) == lock_payload:
            return

        async def _async_store() -> None:
            device_reg = dr.async_get(self.hass)
            if device_reg is None:
                return
            entry = device_reg.async_get(match.device_id)
            if entry is None:
                return

            custom_fields = dict(getattr(entry, "custom_fields", {}) or {})
            custom_fields[LOCK_CUSTOM_FIELD] = lock_payload
            device_reg.async_update_device(match.device_id, custom_fields=custom_fields)
            self._persisted_locks[match.device_id] = lock_payload

        create_task = getattr(self.hass, "async_create_task", asyncio.create_task)
        try:
            create_task(_async_store())
        except Exception:  # pragma: no cover - defensive
            asyncio.create_task(_async_store())

    async def async_trigger_immediate_refresh(self) -> None:
        """Force an immediate re-calculation of EID candidates.

        Callers should invoke this when critical crypto material (Identity Key or
        Secrets Creation Date) changes due to an incoming device update so the
        resolver does not wait for the next scheduled refresh.
        """

        if self._refresh_lock.locked():
            _LOGGER.debug(
                "Immediate refresh requested but resolver is busy; skipping duplicate trigger.",
            )
            return

        _LOGGER.info(
            "EVENT-DRIVEN: Critical Key Update detected. Triggering immediate EID recalculation.",
        )

        await self._refresh_cache()

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
        self._lookup_metadata.clear()
        self._known_timebases.clear()


@runtime_checkable
class _IdentityProvider(Protocol):
    """Interface for coordinator-like objects that expose device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]:
        """Return eligible device identities for EID resolution."""

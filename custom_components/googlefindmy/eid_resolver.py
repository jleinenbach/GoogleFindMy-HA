# custom_components/googlefindmy/eid_resolver.py
"""Ephemeral identifier resolver for local BLE scans (Eddystone-EID / AES-128).

This resolver maps rotating beacon identifiers (EIDs) observed via BLE to
devices known to the integration.

Core change (2025-12):
- EID generation is based on **absolute Unix time in seconds (UTC)** and the
  public Eddystone-EID computation (AES-128, no elliptic curves).
- Previous curve-based variants (secp160r1 / P-256) are removed.

Eddystone-EID reference algorithm:
- Temporary Key: AES-128(identity_key, 0x00*11 || 0xFF || 0x00*2 || TS_top16)
- EID block:      AES-128(temp_key,    0x00*11 || K    || TS_masked32)
- EID output: first 8 bytes (MSB) of AES output
See: https://raw.githubusercontent.com/google/eddystone/master/eddystone-eid/eid-computation.md
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .coordinator import DeviceIdentity, GoogleFindMyCoordinator
from .FMDNCrypto.eid_generator import (
    EID_LENGTH,
    EIK_LENGTH,
    ROTATION_PERIOD,
    EidCandidate,
    generate_eid_candidates,
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

# Common frame hints seen in BLE scan paths (kept permissive).
RAW_HEADER_LENGTH = 1
FMDN_FRAME_TYPE = 0x40
FHNA_SERVICE_OFFSET = 8  # legacy service-data offset used by some scanners


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


@runtime_checkable
class _IdentityProvider(Protocol):
    """Protocol implemented by coordinators that can provide device identities."""

    def get_active_device_identities(self) -> list[DeviceIdentity]: ...


def _eid_prefix(eid: bytes) -> str:
    """Small, stable log prefix for EID values."""
    if not eid:
        return "<empty>"
    return eid[: min(4, len(eid))].hex()


def _mask_u32(value: int) -> int:
    """Mask to uint32."""
    return int(value) & 0xFFFFFFFF


@dataclass(slots=True)
class GoogleFindMyEIDResolver:
    """Resolver that precalculates rotating EIDs for known trackers."""

    hass: HomeAssistant
    _lookup: dict[bytes, EIDMatch] = field(init=False, default_factory=dict)
    _lookup_metadata: dict[bytes, dict[str, Any]] = field(init=False, default_factory=dict)
    _decryption_status: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)

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
            self._pending_refresh = False
            await self._refresh_cache()
            while self._pending_refresh:
                self._pending_refresh = False
                await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        """Rebuild the EID lookup table for all active devices."""
        identities = await self._collect_device_secrets()

        now_unix = int(time.time())
        rotation_start = now_unix - (now_unix % ROTATION_PERIOD)

        # Generate windows for current + neighbors to tolerate clock drift.
        windows = (
            rotation_start - ROTATION_PERIOD,
            rotation_start,
            rotation_start + ROTATION_PERIOD,
        )

        lookup: dict[bytes, EIDMatch] = {}
        lookup_metadata: dict[bytes, dict[str, Any]] = {}

        def _register(
            eid_value: bytes,
            match: EIDMatch,
            meta: dict[str, Any],
        ) -> None:
            if len(eid_value) != EID_LENGTH:
                return
            # Prefer the closest window (smallest absolute offset).
            existing = lookup.get(eid_value)
            if existing is not None and abs(existing.time_offset) <= abs(match.time_offset):
                return
            lookup[eid_value] = match
            lookup_metadata[eid_value] = meta

        for identity in identities:
            if (
                not identity.config_entry_id
                or identity.identity_key is None
                or identity.registry_id is None
            ):
                continue

            key_bytes = bytes(identity.identity_key)

            for window_ts in windows:
                offset = int(window_ts - now_unix)

                for cand in generate_eid_candidates(key_bytes, window_ts):
                    meta = {
                        "timebase": "unix",
                        "rotation_timestamp": window_ts,
                        "masked_rotation_timestamp": _mask_u32(window_ts),
                        "time_offset": offset,
                        "variant": cand.name,
                    }
                    match = EIDMatch(
                        device_id=identity.registry_id,
                        config_entry_id=identity.config_entry_id,
                        canonical_id=identity.canonical_id,
                        time_offset=offset,
                        is_reversed=False,
                    )
                    _register(cand.eid, match, meta)
                    _register(
                        cand.eid[::-1],
                        EIDMatch(
                            device_id=identity.registry_id,
                            config_entry_id=identity.config_entry_id,
                            canonical_id=identity.canonical_id,
                            time_offset=offset,
                            is_reversed=True,
                        ),
                        {**meta, "reversed": True},
                    )

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

    async def _try_decrypt_identity_key(
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

        # Some MCU firmwares bit-flip the blob; try both.
        suggested_mcu = is_mcu_tracker(
            device_type=identity.device_type,
            fast_pair_model_id=identity.fast_pair_model_id,
        )
        candidates = [suggested_mcu, not suggested_mcu]

        # 60-byte envelope (12 nonce + 32 ct + 16 tag) observed for some devices.
        gcm_result = self._unwrap_aes_gcm_identity_key(
            identity=identity,
            encrypted_identity_key=encrypted_identity_key,
            key_sources=key_sources,
            metadata=metadata,
        )
        if gcm_result is not None:
            return gcm_result

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

    def resolve_eid(self, eid_bytes: bytes) -> EIDMatch | None:
        """Resolve a scanned payload to a Home Assistant device registry ID.

        Accepted input shapes (heuristic):
        - Raw EID (8 bytes)
        - Raw frame: 0x40/0x41 + 8-byte EID (length >= 9)
        - Service-data payloads where EID begins at offset 8
        - Fallback: scan for any 8-byte window that matches the cache
        """
        if not isinstance(eid_bytes, (bytes, bytearray)):
            return None

        raw = bytes(eid_bytes)
        candidates: list[bytes] = []

        if len(raw) == EID_LENGTH:
            candidates.append(raw)

        # Back-compat "frame" path: first byte is a frame type.
        if len(raw) >= RAW_HEADER_LENGTH + EID_LENGTH and raw[0] in (0x40, 0x41):
            candidates.append(raw[1 : 1 + EID_LENGTH])

        # Some scan paths deliver the service data chunk (EID at offset 8).
        if len(raw) >= FHNA_SERVICE_OFFSET + EID_LENGTH:
            candidates.append(raw[FHNA_SERVICE_OFFSET : FHNA_SERVICE_OFFSET + EID_LENGTH])

        # If nothing matched so far, fall back to a sliding window probe.
        if not candidates and len(raw) > EID_LENGTH:
            for i in range(0, len(raw) - EID_LENGTH + 1):
                candidates.append(raw[i : i + EID_LENGTH])

        if not candidates:
            return None

        eid_prefix = _eid_prefix(candidates[0])

        if not self._lookup:
            # Cache not ready: schedule a refresh (best-effort).
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
            return None

        for c in candidates:
            match = self._lookup.get(c)
            if match is not None:
                _LOGGER.info(
                    "HIT: device=%s canonical=%s reversed=%s offset=%s eid_prefix=%s",
                    match.device_id,
                    match.canonical_id,
                    match.is_reversed,
                    match.time_offset,
                    eid_prefix,
                )
                return match

            rev = c[::-1]
            match = self._lookup.get(rev)
            if match is not None:
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

"""Cache operations mixin for GoogleFindMyCoordinator.

Methods moved here:
- get_device_location_data: Get cached location data for a device
- prime_device_location_cache: Prime cache with initial data
- seed_device_last_seen: Seed last_seen timestamp for a device
- _track_device_interval: Track device polling intervals
- _persist_anchor_metadata: Persist anchor metadata for EID resolution
- update_device_cache: Update device location cache
- _propagate_location_to_shared_devices: Propagate location to shared devices
- _is_significant_update: Check if update is significant
- _merge_with_existing_cache_row: Merge new data with existing cache
- _haversine_distance: Calculate distance between two coordinates
- _apply_weighted_location_fusion: Apply weighted location fusion
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

from ..const import (
    _EID_REFRESH_DEBOUNCE_S,
    DATA_EID_RESOLVER,
    DEFAULT_MAX_PLAUSIBLE_SPEED_MPS,
    DEFAULT_SPEED_GATE_ENABLED,
    DOMAIN,
    OPT_SPEED_GATE_ENABLED,
)
from ._mixin_typing import _MixinBase
from .helpers.cache import (
    merge_cache_row as _merge_cache_row_impl,
)
from .helpers.cache import (
    normalize_location_fields as _normalize_location_fields_impl,
)
from .helpers.cache import (
    sanitize_decoder_row as _sanitize_decoder_row,
)
from .helpers.cache import (
    should_clear_metadata_only_flag as _should_clear_metadata_only_flag_impl,
)
from .helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK_M,
    MIN_PHYSICAL_ACCURACY_M,
    is_reliable_fix,
    select_display_row,
)
from .helpers.geo import (
    coerce_float as _coerce_float_impl,
)
from .helpers.geo import (
    haversine_distance as _haversine_distance_impl,
)
from .helpers.subentry import normalize_epoch_seconds as _normalize_epoch_seconds

_LOGGER = logging.getLogger(__name__)

# Metadata keys to preserve across cache updates
_METADATA_KEYS = (
    "pair_date",
    "pairDate",
    "deviceRegistration",
    "secrets_creation_date",
    "secretsCreationDate",
    "encrypted_user_secrets_creation_date",
    "encryptedUserSecretsCreationDate",
    "time_anchors_debug",
    "identity_key",
    "identityKey",
    "eik",
    "encrypted_identity_key",
    "encryptedIdentityKey",
    "identity_key_candidates",
    "identityKeyCandidates",
    "owner_key_version",
    "device_type",
    "fast_pair_model_id",
    "fastPairModelId",
    "manufacturer",
    "model",
    "encrypted_account_key",
    "encryptedAccountKey",
    "public_key_address",
    "encryptedSha256AccountKeyPublicAddress",
)

# CamelCase to snake_case key normalization for metadata fields
_CAMEL_TO_SNAKE: dict[str, str] = {
    "identityKey": "identity_key",
    "pairDate": "pair_date",
    "secretsCreationDate": "secrets_creation_date",
    "encryptedUserSecretsCreationDate": "encrypted_user_secrets_creation_date",
    "encryptedIdentityKey": "encrypted_identity_key",
    "identityKeyCandidates": "identity_key_candidates",
    "fastPairModelId": "fast_pair_model_id",
    "encryptedAccountKey": "encrypted_account_key",
    "encryptedSha256AccountKeyPublicAddress": "public_key_address",
}

# Epoch timestamp for year 2000 (2000-01-01 00:00:00 UTC)
# Used to reject timestamps that are clearly invalid (before Y2K)
_Y2K_EPOCH_SECONDS = 946684800.0


def _normalize_metadata_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize camelCase metadata keys to snake_case.

    This ensures consistent key naming in the cache regardless of
    whether the source payload uses camelCase or snake_case.

    Args:
        data: Dictionary potentially containing camelCase keys.

    Returns:
        New dictionary with normalized key names.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        normalized_key = _CAMEL_TO_SNAKE.get(key, key)
        # Don't overwrite if snake_case version already set
        if normalized_key not in result:
            result[normalized_key] = value
    return result


class CacheOperations(_MixinBase):
    """Cache operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage the device location cache,
    including cache updates, location fusion, and metadata persistence.
    """

    def get_device_location_data(self, device_id: str) -> dict[str, Any] | None:
        """Return the cached location data for a single device (copy)."""
        raw = self._device_location_data.get(device_id)
        if not isinstance(raw, dict):
            return None
        return dict(raw)

    def get_display_location_data(self, device_id: str) -> dict[str, Any] | None:
        """Return the row that is actually published for a device (copy).

        The current fix when it carries a usable accuracy, otherwise the last
        accuracy-bearing fix (coordinator last-good), otherwise ``None``. This is
        the single source feeding both Plus Code consumers (the standalone sensor
        and the device_tracker ``plus_code`` attribute), so they are last-known
        by construction and never publish an accuracy-less coordinate the tracker
        hides (Codex #202 / PR #1179).
        """
        row = select_display_row(
            self.get_device_location_data(device_id),
            getattr(self, "_device_last_good_location", {}).get(device_id),
        )
        return dict(row) if row is not None else None

    def _record_last_good_location(
        self, device_id: str, row: Mapping[str, Any]
    ) -> None:
        """Store ``row`` as the device's last *reliable* fix (last-good).

        Only a reliable (non-estimated) fix advances the last-good, so a
        sanitized accuracy-less fix (``_is_significant_update`` replaces its
        missing/error-code accuracy with the 200 m fallback and sets
        ``accuracy_estimated=True``) never poisons the fallback the Plus Code
        display accessors read (#1179). A plain ``has_usable_accuracy`` check
        could not tell that sanitized row apart from a real fix; ``is_reliable_fix``
        excludes the estimated provenance. The ``elif`` is a bootstrap: when
        nothing reliable has been seen yet, the first (estimated/accuracy-less)
        fix is kept, mirroring the tracker's ``_last_good_accuracy_data``
        bootstrap. An *estimated* bootstrap is still published (it carries the
        200 m fallback, so ``select_display_row`` shows it); a *genuinely*
        accuracy-less bootstrap (e.g. a legacy/partial restore that never went
        through ``_is_significant_update``) is retained only so age / status /
        ``has_last_known`` stay available -- ``select_display_row`` applies the
        same ``has_usable_accuracy`` gate to it as to any other fallback and
        never publishes it as a coordinate (Codex PR #1181). The cache is lazily
        initialized (mirroring the ``_device_update_history`` idiom below) so
        coordinators built via ``__new__`` need no extra wiring.
        """
        cache = getattr(self, "_device_last_good_location", None)
        if cache is None:
            cache = {}
            self._device_last_good_location = cache
        if is_reliable_fix(row):
            cache[device_id] = dict(row)
        elif device_id not in cache:
            cache[device_id] = dict(row)

    def prime_device_location_cache(self, device_id: str, data: dict[str, Any]) -> None:
        """Prime the internal location cache with externally-provided data.

        This is intended for test fixtures or bootstrap scenarios where the
        coordinator should start with pre-populated location information
        before its first poll cycle completes.
        """
        existing = self._device_location_data.get(device_id)
        if existing:
            merged = dict(existing)
            merged.update(data)
            self._device_location_data[device_id] = merged
        else:
            self._device_location_data[device_id] = dict(data)
        # Restore-seed the coordinator last-good from the primed row so the Plus
        # Code display accessors return a value immediately after a restart,
        # before the first poll. The device_tracker already calls this on restore
        # (device_tracker.py) with the same row its private last-good restores
        # from, keeping the two last-good caches in sync by construction.
        self._record_last_good_location(device_id, data)

    def seed_device_last_seen(self, device_id: str, timestamp: float) -> None:
        """Seed a device's last-seen timestamp for cache initialization."""
        self._present_last_seen[device_id] = timestamp

    def _track_device_interval(self, device_id: str, last_seen: float | None) -> None:
        """Track last_seen history to predict future poll targets."""
        if last_seen is None:
            return

        history_store = getattr(self, "_device_update_history", None)
        if history_store is None:
            history_store = {}
            self._device_update_history = history_store

        history = history_store.setdefault(device_id, deque(maxlen=4))

        if not history or last_seen > history[-1]:
            history.append(last_seen)

    def _persist_anchor_metadata(
        self,
        device_id: str,
        payload: dict[str, Any],
        *,
        clear_metadata_only: bool = False,
    ) -> None:
        """Persist anchor/identity metadata for EID resolution debugging.

        This method extracts metadata fields from a location payload and
        merges them into the device's cache entry without overwriting
        location fields. The metadata includes:

        - pair_date / pairDate / deviceRegistration.pairDate
        - secrets_creation_date / secretsCreationDate
        - encrypted_user_secrets_creation_date
        - time_anchors_debug

        These fields help the EID resolver reason about rotation windows.

        Args:
            device_id: Canonical device identifier.
            payload: Raw location payload containing potential metadata.
            clear_metadata_only: When True and a new location coordinate is
                available, clear the `metadata_only` flag from the cache entry
                so subsequent snapshot builds treat the data as fresh.
        """
        if not payload:
            return

        # Use module-level constant for metadata keys
        metadata_keys = set(_METADATA_KEYS)

        existing = self._device_location_data.get(device_id)
        if not isinstance(existing, dict):
            existing = {}

        updated = dict(existing)

        # Extract metadata from payload
        for key in metadata_keys:
            value = payload.get(key)
            if value is not None:
                updated[key] = value

        # Handle nested deviceRegistration
        device_reg = payload.get("deviceRegistration")
        if isinstance(device_reg, dict):
            pair_date = device_reg.get("pairDate")
            if pair_date is not None and updated.get("pair_date") is None:
                updated["pair_date"] = pair_date

        # Clear metadata_only flag when we have real coordinates
        if clear_metadata_only and _should_clear_metadata_only_flag_impl(
            updated,
            payload.get("metadata_only"),
        ):
            updated.pop("metadata_only", None)

        # Only write back if we actually have metadata to persist
        has_metadata = any(updated.get(k) is not None for k in metadata_keys)
        if has_metadata or updated != existing:
            self._device_location_data[device_id] = updated

        # Trigger EID resolver refresh when identity_key is present.
        if "identity_key" in payload or "identityKey" in payload:
            self._schedule_inline_eid_resolver_refresh(device_id)

    def _schedule_inline_eid_resolver_refresh(self, device_id: str) -> None:
        """Debounced inline EID-resolver refresh trigger (AP-B2).

        This is the separate inline trigger site (distinct from the central
        ``_schedule_eid_resolver_refresh`` helper in identity.py). During an FCM
        burst, ``_persist_anchor_metadata`` runs once per anchored payload, so
        without coalescing each identity-bearing payload would spawn its own
        ``async_create_task(resolver.async_refresh())``. We hold one pending
        timer handle for this site: the first trigger arms a ``loop.call_later``
        timer, further triggers within ``_EID_REFRESH_DEBOUNCE_S`` are dropped,
        and the handle is cleared *before* the task is created so a trigger after
        the window still produces a fresh task (no swallowed window-change). The
        rebuild-vs-skip decision remains with the AP-B1 skip guard.
        """
        hass_obj = getattr(self, "hass", None)
        if hass_obj is None:
            return
        domain_bucket = hass_obj.data.get(DOMAIN) if hasattr(hass_obj, "data") else None
        if not isinstance(domain_bucket, dict):
            return
        eid_resolver = domain_bucket.get(DATA_EID_RESOLVER)
        if eid_resolver is None:
            return
        refresh_coro = getattr(eid_resolver, "async_refresh", None)
        if not callable(refresh_coro):
            return

        create_task = getattr(hass_obj, "async_create_task", None)
        if not callable(create_task):
            return

        def _spawn_refresh_task() -> None:
            """Clear the debounce handle, then create the single refresh task."""
            # Clear first so a trigger after the window arms a fresh timer.
            self._eid_inline_refresh_debounce_handle = None
            _LOGGER.debug(
                "Triggering EID Resolver refresh for %s (identity_key in anchor_payload)",
                device_id,
            )
            create_task(refresh_coro())

        loop = getattr(hass_obj, "loop", None)
        call_later = getattr(loop, "call_later", None)
        if not callable(call_later):
            # No event loop available (e.g. minimal stubs): fall back to the
            # legacy immediate behaviour so a refresh is never lost.
            _LOGGER.debug(
                "Triggering EID Resolver refresh for %s (identity_key in anchor_payload)",
                device_id,
            )
            create_task(refresh_coro())
            return

        # A timer is already pending for this window: coalesce this trigger.
        if getattr(self, "_eid_inline_refresh_debounce_handle", None) is not None:
            return

        self._eid_inline_refresh_debounce_handle = call_later(
            _EID_REFRESH_DEBOUNCE_S, _spawn_refresh_task
        )

    def update_device_cache(
        self,
        device_id: str,
        location_data: dict[str, Any],
        *,
        source: str | None = None,
    ) -> None:
        """Public, encapsulated update of the internal location cache for one device.

        Used by the FCM receiver (push path) and by internal manual-commit call sites.
        Expects validated fields (decrypt layer performs fail-fast checks).

        Internal rules:
        - Applies type-aware **poll** cooldowns based on an internal `_report_hint` (if present).
        - Strips `_report_hint` from the cached payload to avoid exposing internal fields.
        - Applies weighted fusion and semantic-anchor protection to stabilize coordinates
          while still updating timestamps and metadata.
        """
        # Thread safety: marshal to HA loop if needed
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(self.update_device_cache, device_id, location_data)
            return

        if not isinstance(location_data, dict):
            _LOGGER.debug(
                "Ignored cache update for %s: payload is not a dict", device_id
            )
            return

        # Shallow copy to avoid caller-side mutation
        slot = dict(location_data)

        # Normalize fields
        slot = _normalize_location_fields_impl(slot)
        slot = _normalize_metadata_keys(slot)

        previous_cached = self._device_location_data.get(device_id)
        if not isinstance(previous_cached, Mapping):
            previous_cached = None
        comparison_cached = previous_cached

        # Preserve metadata from existing cache
        if isinstance(previous_cached, Mapping):
            for metadata_key in _METADATA_KEYS:
                cached_value = previous_cached.get(metadata_key)
                incoming_value = slot.get(metadata_key)
                if cached_value is None or incoming_value is not None:
                    continue
                if isinstance(cached_value, dict):
                    slot[metadata_key] = dict(cached_value)
                elif isinstance(cached_value, list):
                    slot[metadata_key] = list(cached_value)
                else:
                    slot[metadata_key] = cached_value

        # Handle metadata_only flag
        incoming_metadata_only = location_data.get("metadata_only")
        has_location_payload = (
            slot.get("latitude") is not None or slot.get("longitude") is not None
        )
        if slot.get("metadata_only") and incoming_metadata_only is False:
            slot.pop("metadata_only", None)
        elif (
            slot.get("metadata_only")
            and has_location_payload
            and incoming_metadata_only is not True
        ):
            slot.pop("metadata_only", None)

        clear_metadata_only = (
            has_location_payload and incoming_metadata_only is not True
        )
        self._persist_anchor_metadata(
            device_id, slot, clear_metadata_only=clear_metadata_only
        )

        # Record semantic label if available
        record_semantic = getattr(self, "_record_semantic_label", None)
        if callable(record_semantic):
            record_semantic(slot, device_id=device_id)

        # Check for replay (same timestamp)
        cached_loc = comparison_cached
        is_replay = False
        if isinstance(cached_loc, Mapping):
            new_ts = _normalize_epoch_seconds(slot.get("last_seen"))
            old_ts = _normalize_epoch_seconds(cached_loc.get("last_seen"))
            if new_ts is not None and old_ts is not None and new_ts == old_ts:
                is_replay = True

        slot["is_replayed"] = is_replay

        # Apply semantic location mapping
        apply_mapping = getattr(self, "_apply_semantic_mapping", None)
        if callable(apply_mapping):
            apply_mapping(slot)

        slot.pop("is_replayed", None)

        report_hint = slot.get("_report_hint")
        fusion_preapplied = bool(slot.pop("_fusion_preapplied", False))

        # Apply weighted fusion if not already done
        if not fusion_preapplied and not self._apply_weighted_location_fusion(
            device_id, slot
        ):
            _LOGGER.debug(
                "Dropping cache update for %s: weighted fusion rejected payload",
                device_id,
            )
            return

        fused_applied = slot.pop("_fused_applied", False)

        status = slot.get("status")

        # Track fused update statistics
        if fused_applied and status == "Fused (Weighted)":
            self.increment_stat("fused_updates")

        # Report-type cooldown removed: location_poll_interval (default 300s)
        # already provides sufficient protection against excessive polling.
        # The previous 600s cooldown for crowdsourced reports caused a feedback
        # loop where replays kept extending the cooldown indefinitely.

        # Track crowd-sourced updates when hint is present
        if report_hint:
            self.increment_stat("crowd_sourced_updates")

        slot.pop("_report_hint", None)
        slot.pop("is_replayed", None)

        # Sanitize decoder row
        slot = _sanitize_decoder_row(slot)

        # Track device interval
        raw_last_seen = _normalize_epoch_seconds(slot.get("last_seen"))
        self._track_device_interval(device_id, raw_last_seen)

        # Significance check
        if not self._is_significant_update(device_id, slot):
            _LOGGER.debug(
                "Dropping cache update for %s: update failed significance checks",
                device_id,
            )
            return

        resolver_refresh_needed = False

        # Handle identity key changes
        cached_identity_key = None
        if isinstance(cached_loc, Mapping):
            cached_identity_key = self._normalize_identity_key(
                cached_loc.get("identity_key")
            )

        incoming_identity_key = self._normalize_identity_key(slot.get("identity_key"))
        if incoming_identity_key is not None:
            slot["identity_key"] = incoming_identity_key

        incoming_eik = self._normalize_identity_key(slot.get("encrypted_identity_key"))
        if incoming_eik is not None:
            slot["encrypted_identity_key"] = incoming_eik

        incoming_owner_key_version = slot.get("owner_key_version")

        identity_changed = (
            incoming_identity_key is not None
            and incoming_identity_key != cached_identity_key
        )

        cached_eik = None
        cached_owner_key_version = None
        if isinstance(cached_loc, Mapping):
            cached_eik = self._normalize_identity_key(
                cached_loc.get("encrypted_identity_key")
            )
            cached_owner_key_version = cached_loc.get("owner_key_version")

        encrypted_changed = False
        if incoming_eik is not None or incoming_owner_key_version is not None:
            encrypted_changed = (
                incoming_eik != cached_eik
                or incoming_owner_key_version != cached_owner_key_version
            )

        if identity_changed or encrypted_changed:
            resolver_refresh_needed = True
            _LOGGER.info(
                "Identity key update detected for %s (ownerKeyVersion=%s); "
                "scheduling EID resolver refresh.",
                device_id,
                incoming_owner_key_version,
            )

        # Ensure last_updated is present
        slot.setdefault("last_updated", time.time())

        # Merge with existing cache
        slot = self._merge_with_existing_cache_row(device_id, slot)

        # Keep name cache up-to-date
        name_cache_fn = getattr(self, "_ensure_device_name_cache", None)
        if callable(name_cache_fn):
            name_cache = name_cache_fn()
            name = slot.get("name")
            if isinstance(name, str) and name:
                name_cache[device_id] = name

        self._device_location_data[device_id] = slot
        # Coordinator last-good: advance only for reliable (non-estimated) fixes
        # so a sanitized accuracy-less update never overwrites the last reliable
        # position the Plus Code display accessors fall back to (non-poison).
        self._record_last_good_location(device_id, slot)

        # FIX #155: Only count background_updates for non-poll sources
        # to avoid double-counting with polled_updates.
        if source != "poll":
            self.increment_stat("background_updates")

        # Register identity key and propagate to shared devices
        effective_identity_key = incoming_identity_key or cached_identity_key
        if effective_identity_key is not None:
            register_fn = getattr(self, "_register_identity_key", None)
            if callable(register_fn):
                register_fn(device_id, effective_identity_key)
            self._propagate_location_to_shared_devices(device_id, slot)

        # Trigger resolver refresh if identity changed
        if resolver_refresh_needed:
            reset_fn = getattr(self, "_reset_resolver_offset", None)
            if callable(reset_fn):
                reset_fn(device_id)
            schedule_fn = getattr(self, "_schedule_eid_resolver_refresh", None)
            if callable(schedule_fn):
                schedule_fn()

    def _propagate_location_to_shared_devices(
        self,
        source_device_id: str,
        location: dict[str, Any],
    ) -> None:
        """Propagate location updates to devices sharing the same tracker.

        When multiple accounts track the same physical device, they share
        an identity_key. This method finds all devices with the same
        identity_key and propagates location updates between them.

        Args:
            source_device_id: The device that received the update.
            location: The location data to propagate.
        """
        if not location:
            return

        # Get the identity key for the source device
        source_identity = self._normalize_identity_key(
            location.get("identity_key")
            or location.get("identityKey")
            or location.get("eik")
        )

        if source_identity is None:
            # Try to get from cache
            cached = self._device_location_data.get(source_device_id)
            if isinstance(cached, dict):
                source_identity = self._normalize_identity_key(
                    cached.get("identity_key")
                    or cached.get("identityKey")
                    or cached.get("eik")
                )

        if source_identity is None:
            return

        # Find devices sharing this identity key
        shared_devices = self._identity_key_to_devices.get(source_identity)
        if not shared_devices or len(shared_devices) <= 1:
            return

        # Propagate to other devices
        incoming_ts = _normalize_epoch_seconds(location.get("last_seen"))
        source_label = location.get("source_label", "unknown")

        for target_id in shared_devices:
            if target_id == source_device_id:
                continue

            target_cache = self._device_location_data.get(target_id)
            if not isinstance(target_cache, dict):
                target_cache = {}

            target_ts = _normalize_epoch_seconds(target_cache.get("last_seen"))

            # Only propagate if source is fresher
            if incoming_ts is not None and target_ts is not None:
                if incoming_ts <= target_ts:
                    continue

            # Create propagated location
            propagated = dict(location)
            propagated["_propagated_from"] = source_device_id

            # Merge with target's metadata
            merged = _merge_cache_row_impl(target_cache, propagated)
            merged["last_updated"] = time.time()

            self._device_location_data[target_id] = merged
            # Coordinator last-good for the shared target, same non-poison gate.
            self._record_last_good_location(target_id, merged)

            _LOGGER.debug(
                "Propagated location from %s to %s (shared tracker, source=%s)",
                source_device_id,
                target_id,
                source_label,
            )

    def _is_significant_update(
        self,
        device_id: str,
        new_data: dict[str, Any],
    ) -> bool:
        """Validate temporal ordering and data quality before committing cache updates.

        This gate rejects:
        1. Malformed payloads (not a dict)
        2. Timestamps that are too old (pre-Y2K) or too far in the future
        3. Timestamps that regress from the cached value

        Accuracy sanitization (not rejection):
        - Values < 0.001m (error code 0.0) are REMOVED from dict, not rejected
        - Valid sub-meter accuracy (e.g., 0.5m) is preserved
        - The report continues processing without precision data

        Args:
            device_id: Canonical device identifier.
            new_data: The latest location payload to evaluate.

        Returns:
            ``True`` when the caller should accept the payload.
        """
        # Use centralized constant from helpers/geo.py
        # MIN_PHYSICAL_ACCURACY_M = 0.001m (only error code 0.0 is filtered)

        # Maximum accepted future drift in seconds (2 hours)
        MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S = 7200

        if not isinstance(new_data, dict):
            _LOGGER.debug("Rejecting update for %s: payload is not a dict", device_id)
            return False

        # Sanitize error code accuracy values (0.0 in Android Location API).
        # The API uses 0.0 as "no accuracy available", not "perfect precision".
        # We ACCEPT the update and REPLACE invalid accuracy with a conservative fallback.
        # This ensures Home Assistant always gets a valid numeric gps_accuracy attribute.
        # Valid sub-meter values like 0.5m or 0.01m are preserved!
        # ``accuracy_estimated`` is a producer-side flag: True whenever the
        # accuracy below was replaced by the conservative fallback radius, False
        # when a real measurement survived. Only the producer can tell the two
        # apart, because once the value is the 200m fallback it is byte-for-byte
        # indistinguishable from a real 200m fix downstream (map_view, recorder).
        new_acc = new_data.get("accuracy")
        if new_acc is None:
            # Missing accuracy: set conservative fallback for HA state machine
            new_data["accuracy"] = DEFAULT_ACCURACY_FALLBACK_M
            new_data["accuracy_estimated"] = True
            self.increment_stat("accuracy_sanitized_count")
            _LOGGER.debug(
                "Setting fallback accuracy for %s: key was missing, using %sm",
                device_id,
                DEFAULT_ACCURACY_FALLBACK_M,
            )
        else:
            try:
                acc_f = float(new_acc)
                if not math.isfinite(acc_f) or acc_f < MIN_PHYSICAL_ACCURACY_M:
                    # Sanitize: replace error code with conservative fallback
                    new_data["accuracy"] = DEFAULT_ACCURACY_FALLBACK_M
                    new_data["accuracy_estimated"] = True
                    self.increment_stat("accuracy_sanitized_count")
                    _LOGGER.debug(
                        "Sanitizing update for %s: error code accuracy (%s) -> %sm",
                        device_id,
                        new_acc,
                        DEFAULT_ACCURACY_FALLBACK_M,
                    )
                    # Continue processing - the update is valid, with fallback precision
                # Numerically valid accuracy. Mark it as a non-estimated fix
                # ONLY when the producer did not already flag it estimated. A
                # preserved 200m fallback is numerically valid yet carries
                # accuracy_estimated=True from upstream; clobbering it to False
                # here would draw a solid "real measurement" circle for a
                # fallback radius. A carried True therefore wins; otherwise the
                # surviving real measurement is recorded as not estimated.
                elif new_data.get("accuracy_estimated") is not True:
                    new_data["accuracy_estimated"] = False
            except (TypeError, ValueError):
                # Non-numeric accuracy: replace with fallback
                new_data["accuracy"] = DEFAULT_ACCURACY_FALLBACK_M
                new_data["accuracy_estimated"] = True
                self.increment_stat("accuracy_sanitized_count")
                _LOGGER.debug(
                    "Sanitizing update for %s: non-numeric accuracy (%r) -> %sm",
                    device_id,
                    new_acc,
                    DEFAULT_ACCURACY_FALLBACK_M,
                )

        n_seen_norm = _normalize_epoch_seconds(new_data.get("last_seen"))
        if n_seen_norm is not None:
            if n_seen_norm < _Y2K_EPOCH_SECONDS:
                self.increment_stat("invalid_ts_drop_count")
                self.increment_stat("drop_reason_invalid_ts")
                # Corrupt (pre-Y2K) timestamp: alarming sub-bucket. Increment
                # only (cumulative), unlike the canonicless absolute assign.
                self.increment_stat("invalid_ts_drop_warn")
                _LOGGER.debug(
                    "Rejecting update for %s: timestamp too old (%s)",
                    device_id,
                    n_seen_norm,
                )
                return False
            if n_seen_norm > time.time() + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S:
                self.increment_stat("future_ts_drop_count")
                _LOGGER.debug(
                    "Rejecting update for %s: timestamp too far in future (%s)",
                    device_id,
                    n_seen_norm,
                )
                return False

        existing = self._device_location_data.get(device_id)
        if not existing:
            return True

        e_seen_norm = _normalize_epoch_seconds(existing.get("last_seen"))
        if (
            n_seen_norm is not None
            and e_seen_norm is not None
            and n_seen_norm < e_seen_norm
        ):
            self.increment_stat("invalid_ts_drop_count")
            self.increment_stat("drop_reason_invalid_ts")
            # Regressed/out-of-order timestamp: benign sub-bucket. Increment
            # only (cumulative), unlike the canonicless absolute assign.
            self.increment_stat("invalid_ts_drop_benign")
            _LOGGER.debug(
                "Rejecting update for %s: timestamp regressed (%s < %s)",
                device_id,
                n_seen_norm,
                e_seen_norm,
            )
            return False

        return True

    def _merge_with_existing_cache_row(
        self,
        device_id: str,
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge incoming location data with existing cache entry.

        This method preserves important fields from the existing cache
        while updating with new data. It handles:
        - Coordinate preservation when incoming is semantic-only
        - Metadata field preservation
        - Timestamp monotonicity

        Args:
            device_id: Device identifier.
            incoming: New location data.

        Returns:
            Merged cache entry.
        """
        existing = self._device_location_data.get(device_id)
        if not isinstance(existing, dict):
            return dict(incoming)

        # Use the helper for core merge logic
        merged = _merge_cache_row_impl(existing, incoming)

        return merged

    def _haversine_distance(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """Calculate the great-circle distance between two points (meters)."""
        return _haversine_distance_impl(lat1, lon1, lat2, lon2)

    def _speed_gate_enabled(self) -> bool:
        """Return whether the kinematic speed gate is active (Discussion #177)."""
        entry = getattr(self, "config_entry", None)
        if entry is None or getattr(entry, "options", None) is None:
            return DEFAULT_SPEED_GATE_ENABLED
        return bool(
            entry.options.get(OPT_SPEED_GATE_ENABLED, DEFAULT_SPEED_GATE_ENABLED)
        )

    def _apply_weighted_location_fusion(
        self,
        device_id: str,
        new_data: dict[str, Any],
    ) -> bool:
        """Fuse overlapping locations while honoring semantic anchors.

        The fusion pipeline keeps semantic locations authoritative and blends
        overlapping sensor fixes to minimize jitter:
        1. Cold starts accept any payload because there is no baseline.
        2. Trusted (semantic) updates always win immediately.
        3. When the cached location is trusted, overlapping sensor fixes snap
           back to the anchor instead of drifting.
        4. Standard sensor fixes are fused with inverse-square weighting when
           their accuracy circles overlap; clear jumps simply pass through.

        Returns True when the caller should continue processing the payload.
        """
        existing = self._device_location_data.get(device_id)
        if not existing or existing.get("latitude") is None:
            return True

        new_lat = _coerce_float_impl(new_data.get("latitude"))
        new_lon = _coerce_float_impl(new_data.get("longitude"))
        if new_lat is None or new_lon is None:
            return True

        existing_lat = _coerce_float_impl(existing.get("latitude"))
        existing_lon = _coerce_float_impl(existing.get("longitude"))
        if existing_lat is None or existing_lon is None:
            return True

        existing_acc_raw = _coerce_float_impl(existing.get("accuracy"))
        new_acc_raw = _coerce_float_impl(new_data.get("accuracy"))

        def _safe_accuracy(value: float | None) -> float:
            """Convert raw accuracy to a safe value for fusion calculations.

            The Android Location API uses 0.0 as an error code meaning "no accuracy".
            We treat values < MIN_VALID_ACCURACY (0.001m) as this error code.

            Modern dual-frequency GNSS (L1+L5) can achieve sub-meter accuracy under
            ideal conditions, so valid values like 0.5m or 0.01m are preserved.

            The fallback (PRIVACY_ACCURACY_FALLBACK = 200m) is based on Bluetooth
            tracker physics (max Bluetooth range + GPS error margin). This ensures:
              - Error codes get lower weight in fusion than real GPS (200²/20² = 100x)
              - The fallback is still useful for finding a tracker (unlike 2km)

            SELF-HEALING: If the cache contains a corrupted value (0.0),
            treating it as 200m allows new valid data to properly override it.
            """
            if value is None or not math.isfinite(value):
                return DEFAULT_ACCURACY_FALLBACK_M
            # < MIN_VALID_ACCURACY (0.001m) is the error code 0.0
            # This also handles self-healing of corrupted cache entries
            if value < MIN_PHYSICAL_ACCURACY_M:
                return DEFAULT_ACCURACY_FALLBACK_M
            return value

        existing_acc = _safe_accuracy(existing_acc_raw)
        new_acc = _safe_accuracy(new_acc_raw)

        try:
            dist = _haversine_distance_impl(
                existing_lat, existing_lon, new_lat, new_lon
            )
        except Exception:
            return True

        radius_sum = existing_acc + new_acc

        # Incoming trusted update always wins
        if new_data.get("location_type") == "trusted":
            return True

        # Existing trusted anchor: snap back if overlapping
        if existing.get("location_type") == "trusted":
            if dist <= radius_sum:
                new_data["latitude"] = existing_lat
                new_data["longitude"] = existing_lon
                if existing_acc_raw is not None:
                    new_data["accuracy"] = existing_acc_raw
                if existing.get("altitude") is not None:
                    new_data["altitude"] = existing["altitude"]
                new_data["location_type"] = "trusted"
                new_data["status"] = "Stationary (at Anchor)"
            return True

        # FIX #155: When BOTH sides carry the fallback accuracy, fusion
        # degenerates to a meaningless 50/50 average that injects jitter.
        # Accept the newer data as-is instead of blending.
        # Placed after trusted-anchor checks so anchored devices stay pinned.
        if (
            existing_acc == DEFAULT_ACCURACY_FALLBACK_M
            and new_acc == DEFAULT_ACCURACY_FALLBACK_M
        ):
            return True

        # Clear jump - no overlap. Before accepting, apply the kinematic
        # plausibility gate (Discussion #177): a single FMDN crowd report can
        # land far away with huge uncertainty ("teleport"). Reject a far jump
        # whose implied speed is physically impossible. Falls through to accept
        # when speed is not computable (missing/degenerate timestamps) so no
        # regression occurs. Own-report GPS fixes bypass the gate entirely: they
        # are cryptographically trusted device positions that do not teleport,
        # so gating them would only risk false-blocking genuine fast travel.
        # Placement rationale (F3): the gate sits AFTER the trusted-anchor and
        # #155 double-fallback branches on purpose - those return earlier because
        # a semantic anchor has no kinematics and two 200m fallback circles give
        # radii too unreliable to gate on (would risk false-blocks, F4-a).
        # dt limit (F5): after a long offline gap dt is huge, implied_speed tiny,
        # so the gate never fires - intended (slow travel over long time is
        # plausible); a post-offline teleport passing ungated is the inherent
        # limit of a forward speed gate (Q2 round-trip gate would catch it).
        if dist > radius_sum:
            incoming_is_own = bool(new_data.get("is_own_report"))
            # Only gate a fix that carries a REAL measured accuracy. A crowd
            # report is "accuracy-less" not only when the key is missing
            # (new_acc_raw is None) but also when it carries Android's
            # no-accuracy sentinel (0.0) or any sub-physical/non-finite value
            # (< MIN_PHYSICAL_ACCURACY_M) - the same error-code set that
            # _safe_accuracy() maps to the 200m fallback. Such a fix is not a
            # clean teleport discriminant and must fall through so the
            # downstream sanitization in _is_significant_update can flag it
            # estimated (accuracy_estimated=True). Hard-dropping it here would
            # regress the #1181 last-good protection; that path is already
            # guarded by is_reliable_fix, not by this gate.
            new_acc_measured = (
                new_acc_raw is not None
                and math.isfinite(new_acc_raw)
                and new_acc_raw >= MIN_PHYSICAL_ACCURACY_M
            )
            if self._speed_gate_enabled() and not incoming_is_own and new_acc_measured:
                existing_ts = _normalize_epoch_seconds(existing.get("last_seen"))
                new_ts = _normalize_epoch_seconds(new_data.get("last_seen"))
                if existing_ts is not None and new_ts is not None:
                    delta_t = new_ts - existing_ts
                    if delta_t > 0:
                        implied_speed = dist / delta_t
                        if implied_speed > DEFAULT_MAX_PLAUSIBLE_SPEED_MPS:
                            self.increment_stat("speed_gate_rejects")
                            _LOGGER.debug(
                                "Speed gate rejected crowd fix %s: %.0fm in "
                                "%.0fs = %.1fm/s > %.1fm/s cap",
                                device_id,
                                dist,
                                delta_t,
                                implied_speed,
                                DEFAULT_MAX_PLAUSIBLE_SPEED_MPS,
                            )
                            return False
            return True

        # Overlapping accuracy circles: fuse with inverse-square weighting
        # Use MIN_PHYSICAL_ACCURACY_M (0.001m) as floor to preserve sub-meter weight
        # and prevent division by zero. Valid sub-meter accuracy gets proper weight.
        w_old = 1 / (max(MIN_PHYSICAL_ACCURACY_M, existing_acc) ** 2)
        w_new = 1 / (max(MIN_PHYSICAL_ACCURACY_M, new_acc) ** 2)
        total_w = w_old + w_new
        if total_w == 0:
            return True

        lat_fused = (existing_lat * w_old + new_lat * w_new) / total_w
        lon_fused = (existing_lon * w_old + new_lon * w_new) / total_w

        new_data["latitude"] = lat_fused
        new_data["longitude"] = lon_fused

        # Calculate fused accuracy using inverse variance weighting.
        #
        # When fusing two measurements with variances σ₁² and σ₂², the optimal
        # combined variance is: σ_fused² = 1 / (1/σ₁² + 1/σ₂²) = 1 / total_w
        #
        # Since accuracy ≈ standard deviation, the fused accuracy is:
        #   accuracy_fused = sqrt(1 / total_w)
        #
        # This is statistically correct: combining two independent measurements
        # should yield a MORE precise result than either alone.
        #
        # Safety bounds:
        # - MIN_FUSED_ACCURACY_M: Physical limit (consumer GPS can't do better)
        # - limit_best: Never claim worse than the best input (conservative)

        MIN_FUSED_ACCURACY_M = 5.0  # Consumer GPS floor

        # NOTE: This predicate intentionally differs from the canonical
        # ``coordinator.helpers.geo.is_valid_accuracy`` and must NOT be
        # consolidated with it. geo uses ``>= MIN_VALID_ACCURACY`` (0.001m),
        # whereas fusion accepts any strictly positive accuracy (``> 0``).
        # For a sub-millimeter input in the open interval (0, 0.001) the two
        # disagree: this predicate keeps it as a valid measurement (driving the
        # both-valid inverse-variance branch), while geo would reject it and
        # collapse fusion to the single valid side. That changes the fused
        # accuracy (characterization: tests/test_cache_fusion_validity.py pins
        # ~11.978m here versus 12.0m under geo). The divergence is deliberate:
        # fusion treats a sub-mm fix as a real, high-weight measurement rather
        # than the 0.0 error code geo guards against.
        def _is_valid_accuracy(acc: float | None) -> bool:
            return (
                acc is not None
                and isinstance(acc, (int, float))
                and math.isfinite(acc)
                and acc > 0
            )

        valid_existing = _is_valid_accuracy(existing_acc_raw)
        valid_new = _is_valid_accuracy(new_acc_raw)

        best_accuracy: float | None
        if valid_existing and valid_new:
            # Both valid: use inverse variance formula
            # accuracy_fused = sqrt(1 / total_w) where total_w = 1/acc₁² + 1/acc₂²
            fused_accuracy = math.sqrt(1.0 / total_w)

            # Safety bounds:
            # - Never claim better than physics allows (MIN_FUSED_ACCURACY_M)
            # - Never claim worse than the best individual measurement
            # Note: assert helps mypy understand values are not None after validation
            assert existing_acc_raw is not None and new_acc_raw is not None
            limit_best = min(existing_acc_raw, new_acc_raw)
            best_accuracy = max(fused_accuracy, limit_best, MIN_FUSED_ACCURACY_M)
        elif valid_new:
            best_accuracy = new_acc_raw
        elif valid_existing:
            best_accuracy = existing_acc_raw
        else:
            # Neither is valid - use conservative fallback
            # This ensures Home Assistant always gets a valid gps_accuracy attribute
            best_accuracy = DEFAULT_ACCURACY_FALLBACK_M

        # ALWAYS write back a valid accuracy - never leave it as None or missing
        # Home Assistant requires a numeric gps_accuracy for the state machine
        new_data["accuracy"] = best_accuracy
        # Mirror the producer flag for the fused result: False only when at least
        # one input was a REAL measurement. An input counts as real when it is
        # numerically valid AND not itself flagged estimated. A numerically valid
        # value is not automatically a real measurement: a preserved 200m fallback
        # is valid (>0) yet carries accuracy_estimated=True, so it must not flip the
        # fused result to "real". The existing flag is read from the cached row and
        # the new flag from new_data. Deriving from input validity (not from
        # value-equality with the fallback) still avoids mislabeling a genuinely
        # fused measurement that happens to equal the 200m fallback value. This
        # assignment remains a guard: the significance gate (_is_significant_update)
        # is the authoritative writer downstream.
        real_existing = valid_existing and not existing.get("accuracy_estimated")
        real_new = valid_new and not new_data.get("accuracy_estimated")
        new_data["accuracy_estimated"] = not (real_existing or real_new)

        new_data["status"] = "Fused (Weighted)"
        new_data["_fused_applied"] = True
        return True

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
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .helpers.cache import (
    LOCATION_FIELDS,
    merge_cache_row as _merge_cache_row_impl,
    normalize_location_fields as _normalize_location_fields_impl,
    preserve_metadata_fields as _preserve_metadata_fields_impl,
    sanitize_decoder_row as _sanitize_decoder_row,
    should_allow_location_update as _should_allow_location_update_impl,
    should_clear_metadata_only_flag as _should_clear_metadata_only_flag_impl,
)
from .helpers.geo import (
    coerce_float as _coerce_float_impl,
    haversine_distance as _haversine_distance_impl,
    safe_accuracy,
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

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class CacheOperations:
    """Cache operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage the device location cache,
    including cache updates, location fusion, and metadata persistence.
    """

    def get_device_location_data(
        self: GoogleFindMyCoordinator, device_id: str
    ) -> dict[str, Any] | None:
        """Return the cached location data for a single device (copy)."""
        raw = self._device_location_data.get(device_id)
        if not isinstance(raw, dict):
            return None
        return dict(raw)

    def prime_device_location_cache(
        self: GoogleFindMyCoordinator, device_id: str, data: dict[str, Any]
    ) -> None:
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

    def seed_device_last_seen(
        self: GoogleFindMyCoordinator, device_id: str, timestamp: float
    ) -> None:
        """Seed a device's last-seen timestamp for cache initialization."""
        self._present_last_seen[device_id] = timestamp

    def _track_device_interval(
        self: GoogleFindMyCoordinator, device_id: str, interval_s: float
    ) -> None:
        """Record an observed polling interval for a device.

        Intervals are appended to a rolling window and used to compute
        predictive polling targets in _get_predicted_poll_time.
        """
        window = self._device_interval_history.setdefault(device_id, [])
        window.append(interval_s)
        max_samples = 10
        if len(window) > max_samples:
            window[:] = window[-max_samples:]

    def _persist_anchor_metadata(
        self: GoogleFindMyCoordinator,
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

    def update_device_cache(
        self: GoogleFindMyCoordinator,
        device_id: str,
        location: dict[str, Any],
        *,
        source: str | None = None,
    ) -> None:
        """Update the internal location cache for a device.

        This method applies the cache update logic, including:
        - Timestamp validation (reject stale updates)
        - Location fusion (weighted merging)
        - Shared device propagation

        Args:
            device_id: Canonical device identifier.
            location: New location data to merge.
            source: Optional source identifier for logging.
        """
        if not location:
            return

        wall_now = time.time()

        # Normalize incoming location
        location = dict(location)
        location = _normalize_location_fields_impl(location)

        # Normalize camelCase metadata keys to snake_case
        location = _normalize_metadata_keys(location)

        # Get existing cache entry
        existing = self._device_location_data.get(device_id)
        if not isinstance(existing, dict):
            existing = {}

        # Check if fusion was already applied (from _async_start_poll_cycle)
        fusion_preapplied = location.pop("_fusion_preapplied", False)

        # Strip internal hints before caching
        location.pop("_report_hint", None)

        # Validate timestamps
        incoming_ts = _normalize_epoch_seconds(location.get("last_seen"))
        existing_ts = _normalize_epoch_seconds(existing.get("last_seen"))

        # Get source ranks
        incoming_rank = location.get("source_rank")
        existing_rank = existing.get("source_rank")

        # Determine if we should allow the location update
        allow_update = _should_allow_location_update_impl(
            existing_ts, incoming_ts, existing_rank, incoming_rank
        )

        # If allow_update is None, we need to check distance for significance
        if allow_update is None and not fusion_preapplied:
            allow_update = self._is_significant_update(device_id, location)

        if allow_update is False and not fusion_preapplied:
            # Reject stale location but preserve metadata
            _LOGGER.debug(
                "Rejecting stale location for %s: existing=%s, incoming=%s",
                device_id,
                existing_ts,
                incoming_ts,
            )
            # Still merge metadata fields
            for key, value in location.items():
                if key not in LOCATION_FIELDS and value is not None:
                    existing[key] = value
            self._device_location_data[device_id] = existing
            return

        # Apply weighted fusion if not already done
        if not fusion_preapplied:
            if not self._apply_weighted_location_fusion(device_id, location):
                # Fusion rejected the update
                return

        # Merge with existing cache
        merged = self._merge_with_existing_cache_row(device_id, location)

        # Set last_updated timestamp
        merged["last_updated"] = wall_now

        # Preserve metadata from existing entry
        merged = _preserve_metadata_fields_impl(existing, merged, _METADATA_KEYS)

        # Store the merged result
        self._device_location_data[device_id] = merged

        # Track interval for predictive polling
        if incoming_ts is not None and existing_ts is not None:
            interval = incoming_ts - existing_ts
            if 0 < interval < 86400:  # Sanity check: less than 24h
                self._track_device_interval(device_id, interval)

        # Propagate to shared devices
        self._propagate_location_to_shared_devices(device_id, merged)

        _LOGGER.debug(
            "Cache updated for %s (source=%s): ts=%s",
            device_id,
            source or "unknown",
            incoming_ts,
        )

    def _propagate_location_to_shared_devices(
        self: GoogleFindMyCoordinator,
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

            _LOGGER.debug(
                "Propagated location from %s to %s (shared tracker, source=%s)",
                source_device_id,
                target_id,
                source_label,
            )

    def _is_significant_update(
        self: GoogleFindMyCoordinator,
        device_id: str,
        location: dict[str, Any],
    ) -> bool:
        """Check if a location update represents a significant change.

        Significant changes include:
        - Distance moved > threshold (default 50m)
        - First location for the device
        - Accuracy improvement > 50%

        Args:
            device_id: Device identifier.
            location: New location data.

        Returns:
            True if the update is significant and should be applied.
        """
        existing = self._device_location_data.get(device_id)
        if not isinstance(existing, dict):
            return True  # First location is always significant

        # Get coordinates
        new_lat = _coerce_float_impl(location.get("latitude"))
        new_lon = _coerce_float_impl(location.get("longitude"))
        old_lat = _coerce_float_impl(existing.get("latitude"))
        old_lon = _coerce_float_impl(existing.get("longitude"))

        # If we don't have valid coordinates, allow the update
        if new_lat is None or new_lon is None:
            return True
        if old_lat is None or old_lon is None:
            return True

        # Calculate distance
        try:
            distance = _haversine_distance_impl(old_lat, old_lon, new_lat, new_lon)
        except Exception:
            return True  # On error, allow update

        # Check distance threshold (50m default)
        threshold = 50.0
        if distance > threshold:
            return True

        # Check accuracy improvement
        new_acc = safe_accuracy(location.get("accuracy"))
        old_acc = safe_accuracy(existing.get("accuracy"))

        if new_acc is not None and old_acc is not None:
            # Significant if accuracy improved by > 50%
            if new_acc < old_acc * 0.5:
                return True

        return False

    def _merge_with_existing_cache_row(
        self: GoogleFindMyCoordinator,
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
        self: GoogleFindMyCoordinator,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """Calculate the great-circle distance between two points (meters)."""
        return _haversine_distance_impl(lat1, lon1, lat2, lon2)

    def _apply_weighted_location_fusion(
        self: GoogleFindMyCoordinator,
        device_id: str,
        location: dict[str, Any],
    ) -> bool:
        """Apply weighted location fusion based on source authority.

        This method compares the incoming location with cached data and
        decides whether to accept, reject, or blend the update based on:
        - Source rank (owner > crowdsourced > aggregated > semantic)
        - Timestamp freshness
        - Accuracy values

        Args:
            device_id: Device identifier.
            location: Location data to evaluate.

        Returns:
            True if the location should be applied, False to reject.
        """
        existing = self._device_location_data.get(device_id)
        if not isinstance(existing, dict):
            return True  # No existing data, accept

        # Get timestamps
        incoming_ts = _normalize_epoch_seconds(location.get("last_seen"))
        existing_ts = _normalize_epoch_seconds(existing.get("last_seen"))

        # Get source ranks (higher = more authoritative)
        incoming_rank = location.get("source_rank", 0)
        existing_rank = existing.get("source_rank", 0)

        # If incoming has higher rank, accept
        if incoming_rank > existing_rank:
            return True

        # If existing has higher rank, only accept if incoming is significantly fresher
        if existing_rank > incoming_rank:
            if incoming_ts is None or existing_ts is None:
                return False
            # Require at least 5 minutes fresher to override higher-rank source
            if incoming_ts - existing_ts < 300:
                _LOGGER.debug(
                    "Rejecting %s update for %s: lower rank (%s vs %s) and not fresh enough",
                    location.get("source_label", "unknown"),
                    device_id,
                    incoming_rank,
                    existing_rank,
                )
                return False

        # Same rank: prefer fresher data
        if incoming_ts is not None and existing_ts is not None:
            if incoming_ts < existing_ts:
                _LOGGER.debug(
                    "Rejecting stale %s update for %s: %s < %s",
                    location.get("source_label", "unknown"),
                    device_id,
                    incoming_ts,
                    existing_ts,
                )
                return False

        return True

"""Locate operations for GoogleFindMyCoordinator.

This module contains location-related methods extracted from main.py.

Methods moved here:
- _normalize_coords: Validate and normalize latitude/longitude
- can_play_sound: Check if Play Sound is enabled for device
- _get_device_lock: Get or create device-specific lock
- can_request_location: Check if manual locate is allowed
- async_locate_device: Locate a device using the native async API
- async_play_sound: Play sound on a device
- async_stop_sound: Stop sound on a device
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from typing import Any

from aiohttp import ClientConnectionError, ClientError
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from ..const import DEFAULT_MIN_POLL_INTERVAL
from ..NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyLookupTransientError,
    StaleOwnerKeyError,
)
from ..NovaApi.nova_request import (
    NovaAuthError,
    NovaHTTPError,
    NovaLogicError,
    NovaProtobufDecodeError,
    NovaRateLimitError,
)
from ..SpotApi.spot_request import SpotAuthPermanentError
from ._mixin_typing import _MixinBase
from ._reauth_reason import ReauthReasonCode
from .helpers.cache import (
    SOUND_UUID_MAX_AGE_S,
    carry_reused_accuracy,
    is_sound_uuid_expired,
)
from .helpers.geo import MIN_PHYSICAL_ACCURACY_M

_LOGGER = logging.getLogger(__name__)

# Cooldown guardrails for owner purge window
_COOLDOWN_OWNER_MIN_S = 60.0
_COOLDOWN_OWNER_MAX_S = 600.0


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between min and max."""
    return max(min_val, min(max_val, value))


class LocateOperations(_MixinBase):
    """Locate operations mixin for GoogleFindMyCoordinator.

    This class contains methods that handle device location requests,
    including coordinate validation and location management.
    """

    # Attribute declaration for mypy (actual value set in GoogleFindMyCoordinator.__init__)
    _is_polling: bool

    def _normalize_coords(
        self,
        payload: dict[str, Any],
        *,
        device_label: str | None = None,
        warn_on_invalid: bool = True,
    ) -> bool:
        """Validate and normalize latitude/longitude (and optionally accuracy).

        - Accepts numeric-like strings and converts them to floats.
        - Rejects NaN/Inf and out-of-range values.
        - Writes normalized floats back into `payload` when valid.
        - Normalizes `accuracy` to a finite float if present (best-effort).

        Returns:
            True if latitude/longitude are present and valid after normalization.
            False if coordinates are missing or invalid.

        Side effects:
            - Increments `invalid_coords` on invalid input.
            - Logs warnings for invalid data (unless warn_on_invalid=False).
        """
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if lat is None or lon is None:
            # Missing coordinates is not an error per se (semantic-only is valid).
            return False

        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring invalid (non-numeric) coordinates%s: lat=%r, lon=%r",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        if not (
            math.isfinite(lat_f)
            and math.isfinite(lon_f)
            and -90.0 <= lat_f <= 90.0
            and -180.0 <= lon_f <= 180.0
        ):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring out-of-range/invalid coordinates%s: lat=%s, lon=%s",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        # Write back normalized floats
        payload["latitude"] = lat_f
        payload["longitude"] = lon_f

        # Best-effort normalize accuracy (if present).
        # The Android Location API uses 0.0 as an error code ("no accuracy").
        # Modern dual-frequency GNSS can achieve sub-meter accuracy, so we only
        # filter the error code (< 0.001m) and negative values.
        acc = payload.get("accuracy")
        if acc is not None:
            try:
                acc_f = float(acc)
                if math.isfinite(acc_f) and acc_f >= MIN_PHYSICAL_ACCURACY_M:
                    payload["accuracy"] = acc_f
                else:
                    # Error code (0.0), negative, NaN, Inf - remove it
                    payload.pop("accuracy", None)
            except (TypeError, ValueError):
                # Accuracy malformed; remove it
                payload.pop("accuracy", None)

        return True

    def can_play_sound(self, device_id: str) -> bool:
        """Return True if 'Play Sound' should be enabled for the device.

        **No network in availability path.**
        Strategy:
        - If capability is known from the lightweight device list -> use it (fast, cached).
        - If push readiness is explicitly False -> disable.
        - Otherwise -> optimistic True (known devices) to keep the UI usable.
          The actual action enforces reality and will start a cooldown on failure.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if playing a sound is likely possible.
        """
        # 1) Use cached capability when available (fast path, no network).
        caps = self._device_caps.get(device_id)
        if caps and isinstance(caps.get("can_ring"), bool):
            res = bool(caps["can_ring"])
            _LOGGER.debug(
                "can_play_sound(%s) -> %s (from capability can_ring)", device_id, res
            )
            return res

        # 2) Short-circuit if push transport is not ready.
        ready = self._api_push_ready()
        if ready is False:
            # Respect explicit cooldowns triggered after recent failures, but do not
            # hide the action solely because push transport appears disconnected.
            if time.monotonic() < self._push_cooldown_until:
                _LOGGER.debug(
                    "can_play_sound(%s) -> False (push cooldown active)", device_id
                )
                return False
            _LOGGER.debug(
                "can_play_sound(%s): push not ready, keeping entity available",
                device_id,
            )

        # 3) Optimistic final decision based on whether we know the device.
        name_cache = self._ensure_device_name_cache()
        is_known = device_id in name_cache or device_id in self._device_location_data
        if is_known:
            _LOGGER.debug(
                "can_play_sound(%s) -> True (optimistic; known device, push_ready=%s)",
                device_id,
                ready,
            )
            return True

        _LOGGER.debug(
            "can_play_sound(%s) -> True (optimistic final fallback)", device_id
        )
        return True

    # ---------------------------- Public control / Locate gating ------------
    def _get_device_lock(self, device_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific device.

        This prevents race conditions when multiple concurrent locate requests
        target the same device (e.g., rapid UI clicks or parallel service calls).
        """
        if device_id not in self._device_action_locks:
            self._device_action_locks[device_id] = asyncio.Lock()
        return self._device_action_locks[device_id]

    def can_request_location(self, device_id: str) -> bool:
        """Return True if a manual 'Locate now' request is currently allowed.

        Gate conditions:
          - device not ignored,
          - no sequential polling in progress,
          - no in-flight locate for the device,
          - per-device cooldown (lower-bounded by DEFAULT_MIN_POLL_INTERVAL) not active.
        Push readiness is checked lazily when submitting the request so the UI
        can stay responsive while the transport recovers.
        """
        # Block manual locate for ignored devices.
        if self.is_ignored(device_id):
            return False
        if self._is_polling:
            return False
        if device_id in self._locate_inflight:
            return False
        # Respect both manual-locate and poll cooldowns for the device
        now_mono = time.monotonic()
        until_manual = self._locate_cooldown_until.get(device_id, 0.0)
        if until_manual and now_mono < until_manual:
            return False
        until_poll = self._device_poll_cooldown_until.get(device_id, 0.0)
        if until_poll and now_mono < until_poll:
            return False
        return True

    # ---------------------------- Passthrough API ---------------------------
    async def async_locate_device(self, device_id: str) -> dict[str, Any]:
        """Locate a device using the native async API (no executor).

        UX & gating:
          - Reject immediately if `can_request_location()` is False.
          - Mark request as in-flight and (optimistically) start a cooldown that
            equals `DEFAULT_MIN_POLL_INTERVAL`. This disables repeated clicks.
          - On success: reset the polling baseline and set a **per-device cooldown**
            (owner-report purge window) by clamping a dynamic guess.
          - Always notify listeners via `async_set_updated_data(self.data)`.

        POPETS'25-informed behaviour:
          - If the returned payload carries an internal `_report_hint` of
            "in_all_areas" (~10 min throttle) or "high_traffic" (~5 min throttle),
            we additionally apply a type-aware cooldown (at least server minimum
            and at least one user poll interval). This stacks with the owner cooldown.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary containing the location data (empty dict on gating).

        Corrections:
            - Persist the received location data into the coordinator cache.
            - Mirror the Google Home spam filter used by the polling path.
            - Preserve previous coordinates for semantic-only locations.
            - Validate coordinates/accuracy and apply significance gating.
            - Push a fresh snapshot via `push_updated([device_id])`.
        """
        # Import helpers lazily to avoid circular imports
        from .helpers.cache import sanitize_decoder_row as _sanitize_decoder_row
        from .helpers.subentry import (
            normalize_epoch_seconds as _normalize_epoch_seconds,
        )

        name = self.get_device_display_name(device_id) or device_id

        # Acquire per-device lock to prevent race conditions on concurrent requests
        lock = self._get_device_lock(device_id)
        async with lock:
            if not self.can_request_location(device_id):
                _LOGGER.warning(
                    "Manual locate for %s is currently disabled (in-flight, cooldown, or polling).",
                    name,
                )
                return {}

            if not self._api_push_ready():
                # Only hard-block during the post-failure cooldown window;
                # otherwise allow the attempt (mirrors can_play_sound logic).
                if time.monotonic() < self._push_cooldown_until:
                    _LOGGER.warning(
                        "Manual locate for %s blocked (push transport recovering).",
                        name,
                    )
                    return {}
                _LOGGER.debug(
                    "Push transport not confirmed ready for %s; attempting locate anyway.",
                    name,
                )

            # Enter in-flight and set a lower-bound cooldown window
            self._locate_inflight.add(device_id)
            self._locate_cooldown_until[device_id] = time.monotonic() + float(
                DEFAULT_MIN_POLL_INTERVAL
            )
            self.async_set_updated_data(self.data)

            google_home_filter = self._get_google_home_filter()

            try:
                location_data = await self.api.async_get_device_location(
                    device_id, name
                )

                # Success path: clear any auth error state
                self._set_auth_state(failed=False)

                if not location_data:
                    return {}

                # Manual locate is upward-only for the reauth budget and never
                # consumes the poll-only decrypt-proof hint; drop it here (after the
                # empty guard, so location_data is a non-empty dict) so it cannot
                # leak into the cached payload or the returned dict (mirrors the
                # _report_hint stripping discipline).
                location_data.pop("_decrypt_proven", None)

                self._record_semantic_label(location_data, device_id=device_id)

                cached_loc = self._device_location_data.get(device_id)
                is_replay = False
                if isinstance(cached_loc, Mapping):
                    new_ts = _normalize_epoch_seconds(location_data.get("last_seen"))
                    old_ts = _normalize_epoch_seconds(cached_loc.get("last_seen"))
                    if new_ts is not None and old_ts is not None and new_ts == old_ts:
                        is_replay = True

                location_data["is_replayed"] = is_replay
                mapping_applied = self._apply_semantic_mapping(location_data)

                # --- Parity with polling path: Google Home semantic spam filter --------
                # Consume coordinate substitution from the filter when needed.
                semantic_name = location_data.get("semantic_name")
                if (
                    not mapping_applied
                    and not is_replay
                    and semantic_name
                    and google_home_filter is not None
                ):
                    try:
                        (should_filter, replacement_attrs) = (
                            google_home_filter.should_filter_detection(
                                device_id, semantic_name
                            )
                        )
                    except Exception as gf_err:
                        _LOGGER.debug(
                            "Google Home filter error for %s: %s", name, gf_err
                        )
                    else:
                        if should_filter:
                            _LOGGER.debug(
                                "Filtering out Google Home spam detection for %s (manual locate)",
                                name,
                            )
                            # Successful but filtered: reset baseline, clear cooldown, and refresh UI.
                            self._last_poll_mono = time.monotonic()
                            self._locate_cooldown_until.pop(device_id, None)
                            self.push_updated([device_id])
                            return {}
                        if replacement_attrs:
                            prev_location = self._device_location_data.get(device_id)
                            keep_previous_precise = (
                                self._should_preserve_precise_home_coordinates(
                                    prev_location, replacement_attrs
                                )
                            )

                            location_data = dict(location_data)
                            if keep_previous_precise and prev_location is not None:
                                _LOGGER.debug(
                                    "Google Home filter: %s detected at '%s' (manual locate), preserving previous precise coordinates",
                                    name,
                                    semantic_name,
                                )
                                location_data["latitude"] = prev_location["latitude"]
                                location_data["longitude"] = prev_location["longitude"]
                                # Carry the producer flag with the reused accuracy so
                                # a preserved estimated fallback is not reclassified
                                # as a real measurement downstream.
                                carry_reused_accuracy(location_data, prev_location)
                            else:
                                if (
                                    "latitude" in replacement_attrs
                                    and "longitude" in replacement_attrs
                                ):
                                    location_data["latitude"] = replacement_attrs.get(
                                        "latitude"
                                    )
                                    location_data["longitude"] = replacement_attrs.get(
                                        "longitude"
                                    )
                                if (
                                    "radius" in replacement_attrs
                                    and replacement_attrs.get("radius") is not None
                                ):
                                    location_data["accuracy"] = replacement_attrs.get(
                                        "radius"
                                    )
                            # Clear semantic name so HA Core's zone engine determines the final state.
                            location_data["semantic_name"] = None
                location_data.pop("is_replayed", None)
                # ----------------------------------------------------------------------

                # Preserve previous coordinates if only semantic location is provided.
                if (
                    location_data.get("latitude") is None
                    or location_data.get("longitude") is None
                ) and location_data.get("semantic_name"):
                    prev = self._device_location_data.get(device_id, {})
                    if prev:
                        location_data.setdefault("latitude", prev.get("latitude"))
                        location_data.setdefault("longitude", prev.get("longitude"))
                        # Carry the producer flag with the reused accuracy under
                        # setdefault semantics so a preserved estimated fallback
                        # keeps its flag without clobbering a value already present.
                        carry_reused_accuracy(location_data, prev, setdefault=True)
                        location_data["status"] = (
                            "Semantic location; preserving previous coordinates"
                        )

                # Validate/normalize coordinates (and accuracy if present).
                if not self._normalize_coords(location_data, device_label=name):
                    if not location_data.get("semantic_name"):
                        _LOGGER.debug(
                            "No location data (coordinates or semantic name) available for %s in manual locate.",
                            name,
                        )
                    return {}

                # Prepare a copy for gating/cooldown application
                slot = dict(location_data)
                slot.setdefault("last_updated", time.time())

                # Report-type cooldown removed: location_poll_interval already
                # regulates polling frequency; the 600s cooldown caused stale updates.
                report_hint = slot.get("_report_hint")

                # Track crowd-sourced updates when hint is present
                if report_hint:
                    self.increment_stat("crowd_sourced_updates")

                slot.pop("_report_hint", None)

                # Sanitize invariants + derive labels before significance gating
                slot = _sanitize_decoder_row(slot)

                if not self._apply_weighted_location_fusion(device_id, slot):
                    return {}

                slot["_fusion_preapplied"] = True

                # Increment crowdsourced stats for manual locate as well (if applicable)
                if slot.get("source_label") == "crowdsourced":
                    self.increment_stat("crowd_sourced_updates")

                # Commit to cache (update_device_cache ensures last_updated and stats)
                self.update_device_cache(device_id, slot)

                # Successful manual locate:
                # - reset poll baseline,
                # - set a per-device poll cooldown (owner purge window) using a dynamic guess
                #   clamped into guardrails,
                # - set the same cooldown for manual locate button to avoid spamming.
                self._last_poll_mono = time.monotonic()
                dynamic_guess = max(
                    float(DEFAULT_MIN_POLL_INTERVAL), float(self.location_poll_interval)
                )
                owner_cooldown = _clamp(
                    dynamic_guess, _COOLDOWN_OWNER_MIN_S, _COOLDOWN_OWNER_MAX_S
                )
                now_mono = time.monotonic()
                # Extend (not overwrite) any type-aware cooldown applied above
                existing_deadline = self._device_poll_cooldown_until.get(device_id, 0.0)
                owner_deadline = now_mono + owner_cooldown
                self._device_poll_cooldown_until[device_id] = max(
                    existing_deadline, owner_deadline
                )
                self._locate_cooldown_until[device_id] = max(
                    self._locate_cooldown_until.get(device_id, 0.0), owner_deadline
                )

                # Touch presence for the device (a fresh interaction implies it exists)
                self._present_last_seen[device_id] = now_mono

                self.push_updated([device_id])
                return location_data or {}
            except SpotAuthPermanentError as auth_err:
                self._set_auth_state(
                    failed=True,
                    reason=f"Auth failed during manual locate: {auth_err}",
                )
                # FIX 3: direct reauth site (no exception bubbles to a coordinator
                # catch), so record the classified reason directly.
                self.record_reauth_reason(
                    ReauthReasonCode.OWNER_KEY_PERMANENT,
                    origin="locate.py:521",
                )
                entry = getattr(self, "config_entry", None)
                reauth_started = False
                if entry is not None:
                    try:
                        # ``ConfigEntry.async_start_reauth`` is a synchronous
                        # ``@callback`` returning ``None`` (it schedules the flow
                        # itself). Awaiting it would raise ``TypeError`` on the
                        # ``None`` result, which the broad ``except`` below would
                        # swallow, leaving ``reauth_started`` False and emitting
                        # the wrong user message. Call it without ``await``.
                        entry.async_start_reauth(self.hass)
                        reauth_started = True
                    except Exception as reauth_err:  # pragma: no cover - defensive
                        _LOGGER.debug(
                            "Failed to start reauth flow after manual locate auth error: %s",
                            reauth_err,
                        )
                message = (
                    "Authentication for Google Find My Device expired; "
                    "re-authentication has been started."
                    if reauth_started
                    else "Authentication for Google Find My Device expired; please re-authenticate."
                )
                raise HomeAssistantError(message) from auth_err
            except ConfigEntryAuthFailed as auth_exc:
                # Mark error and request a refresh; no need to re-raise here for manual action.
                self._set_auth_state(
                    failed=True, reason=f"Auth failed during manual locate: {auth_exc}"
                )
                try:
                    await self.async_request_refresh()
                except Exception:
                    pass
                return {}
            except NovaAuthError as auth_err:
                # Expected: Authentication/permission issue from Nova API
                _LOGGER.warning(
                    "Manual locate for %s failed (authentication): HTTP %s - %s",
                    name,
                    getattr(auth_err, "status", "?"),
                    auth_err,
                )
                self._set_auth_state(failed=True, reason=f"Nova auth error: {auth_err}")
                self.note_error(auth_err, where="async_locate_device", device=name)
                return {}
            except NovaRateLimitError as rate_err:
                # Expected: Rate limiting from Nova API (429 Too Many Requests)
                _LOGGER.warning(
                    "Manual locate for %s rate-limited by Google: %s",
                    name,
                    rate_err,
                )
                self.note_error(rate_err, where="async_locate_device", device=name)
                return {}
            except NovaHTTPError as http_err:
                # Expected: Server errors from Nova API (5xx)
                _LOGGER.warning(
                    "Manual locate for %s failed (server error): HTTP %s - %s",
                    name,
                    getattr(http_err, "status", "?"),
                    http_err,
                )
                self.note_error(http_err, where="async_locate_device", device=name)
                return {}
            except NovaLogicError as logic_err:
                # Expected: Logic error from Protobuf response (e.g., invalid device ID)
                _LOGGER.warning(
                    "Manual locate for %s failed (API logic error): Code %s - %s",
                    name,
                    getattr(logic_err, "code", "?"),
                    getattr(logic_err, "message", str(logic_err)),
                )
                self.note_error(logic_err, where="async_locate_device", device=name)
                return {}
            except NovaProtobufDecodeError as decode_err:
                # Expected: Malformed Protobuf response
                _LOGGER.warning(
                    "Manual locate for %s failed (decode error): %s",
                    name,
                    decode_err,
                )
                self.note_error(decode_err, where="async_locate_device", device=name)
                return {}
            except StaleOwnerKeyError as stale_err:
                # Per-tracker outdated owner key: an account-wide reauth would not
                # fix a single outdated tracker. Record it through the shared entry
                # point (per-tracker log, no account counter) and return an empty
                # result rather than a generic HomeAssistantError. Must precede the
                # DecryptionError handler below -- it is a subclass.
                self.note_decrypt_failure(stale=True, error=stale_err, device=name)
                self.note_error(stale_err, where="async_locate_device", device=name)
                return {}
            except OwnerKeyLookupTransientError as transient_err:
                # Transient owner-key lookup miss (partial server response,
                # network/gRPC failure). NOT a credential defect: do NOT feed the
                # account-wide reauth counter and do NOT escalate. Treat it as a
                # plain skip (empty result), like a "no pending report" cycle, so a
                # transient single-device failure never drives a spurious reauth.
                # Must precede the DecryptionError handler; it is NOT a subclass, so
                # this explicit catch keeps it out of the escalation path.
                _LOGGER.debug(
                    "Manual locate for %s skipped (transient owner-key lookup): %s",
                    name,
                    transient_err,
                )
                self.note_error(transient_err, where="async_locate_device", device=name)
                return {}
            except DecryptionError as dec_err:
                # Account-wide stale/missing shared key. Feed the SAME escalation
                # counter as the poll and push paths (single source of truth) so a
                # user clicking "Locate" contributes to -- and can trigger -- the
                # reauth flow instead of silently swallowing into a generic error.
                # Below threshold: graceful empty result, like the poll path keeps
                # polling. At threshold: start reauth (HA cannot refresh the shared
                # key itself; the user must supply a fresh secrets.json).
                #
                # This path only advances the shared counter, never resets it: the
                # authoritative reset is the poll loop's whole-cycle decrypt-clean
                # gate. A manual locate cannot prove the key is healthy (an empty
                # result means "no pending report", which decrypts nothing), so
                # resetting here could mask a genuinely stale key. The next clean
                # poll cycle reconciles the counter. Locate is an additional upward
                # evidence source, not a reset surface.
                should_reauth = self.note_decrypt_failure(
                    stale=False, error=dec_err, device=name
                )
                self.note_error(dec_err, where="async_locate_device", device=name)
                if not should_reauth:
                    return {}
                self._set_auth_state(
                    failed=True,
                    reason=(
                        "Location decryption keeps failing during manual locate; "
                        "the shared key is stale and a fresh secrets.json "
                        "(re-authentication) is required"
                    ),
                )
                # FIX 3: direct reauth site (a stale shared key needs fresh
                # credentials); record the classified reason directly.
                self.record_reauth_reason(
                    ReauthReasonCode.AAS_INVALID,
                    origin="locate.py:656",
                )
                entry = getattr(self, "config_entry", None)
                reauth_started = False
                if entry is not None:
                    try:
                        # async_start_reauth is a synchronous @callback returning
                        # None (it schedules the flow); awaiting it would raise on
                        # the None result. Call it without await, mirroring the
                        # SpotAuthPermanentError handler above.
                        entry.async_start_reauth(self.hass)
                        reauth_started = True
                    except Exception as reauth_err:  # pragma: no cover - defensive
                        _LOGGER.debug(
                            "Failed to start reauth flow after manual locate "
                            "decryption failure: %s",
                            reauth_err,
                        )
                message = (
                    "Google Find My Device can no longer decrypt locations "
                    "(the shared key is stale); re-authentication has been started."
                    if reauth_started
                    else "Google Find My Device can no longer decrypt locations "
                    "(the shared key is stale); please re-authenticate."
                )
                raise HomeAssistantError(message) from dec_err
            except Exception as err:
                short_err = self._short_error_message(err)
                _LOGGER.error("Manual locate for %s failed: %s", name, short_err)
                self.note_error(err, where="async_locate_device", device=name)
                raise HomeAssistantError(
                    f"Manual locate for '{name}' failed due to an unexpected error. "
                    "Check logs for details."
                ) from err
            finally:
                self._locate_inflight.discard(device_id)
                # Push an update so buttons/entities can refresh availability
                self.async_set_updated_data(self.data)

    def _cached_sound_uuid_is_stale(self, device_id: str) -> bool:
        """Return True if the device's cached Play Sound UUID has aged out.

        Mirrors the load-path expiry (#108) so the store-path overwrite guard
        and the Stop read-path agree with the reload filter on what "stale"
        means. A key with no tracked timestamp (e.g. tests that bypass
        ``__init__``, or pre-tracking entries) is treated as fresh here: only a
        present, genuinely aged timestamp marks the key stale, so a known-good
        cancel key is never dropped on incomplete state.
        """
        timestamps = getattr(self, "_sound_request_timestamps", None)
        if timestamps is None:
            return False
        existing_ts = timestamps.get(device_id)
        if existing_ts is None:
            return False
        return is_sound_uuid_expired(existing_ts, time.time(), SOUND_UUID_MAX_AGE_S)

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device using the native async API (no executor).

        Guard with can_play_sound(); on failure, start a short cooldown to avoid repeated errors.

        **IMPORTANT**: This method tracks the request UUID so that Stop Sound can properly
        cancel the specific Play Sound request. Without UUID tracking, sounds may continue
        ringing indefinitely even after pressing Stop.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
        if not self.can_play_sound(device_id):
            _LOGGER.debug(
                "Suppressing play_sound call for %s: capability/push not ready",
                device_id,
            )
            return False
        try:
            ok, request_uuid = await self.api.async_play_sound(device_id)
            # Decide whether to (over)write the cached Stop cancel key.
            # api.async_play_sound returns a non-None UUID in exactly the two
            # cases where a ring may be active and Stop needs the key: (1) the
            # server accepted the command (HTTP 200, ok=True) or (2) post-dispatch
            # ambiguity — a failure at/after the request reached the wire (server
            # disconnect, read timeout, or a transient 5xx that may have been
            # generated before Nova accepted the command; ok=False but the play
            # may have started). It returns None for every failure that provably
            # never rang (pre-dispatch/connection-setup failure OR an explicit
            # rejection such as 401/403). Storing rule: overwrite only on an
            # accepted command (ok=True) or when no key is cached yet. A merely
            # ambiguous result (ok=False with a fresh UUID) must NOT clobber a
            # known-good key for an earlier, possibly still-ringing play —
            # otherwise the default Stop would cancel the wrong request. With no
            # cached key — or one that has aged past SOUND_UUID_MAX_AGE_S, which
            # the reload filter would discard — the ambiguous UUID is still
            # stored, since it may be the only handle on a current ring. See
            # IRR-CA-CANCEL-KEY-ON-SUCCESS-ONLY.
            existing_uuid = self._sound_request_uuids.get(device_id)
            existing_is_stale = (
                existing_uuid is not None
                and self._cached_sound_uuid_is_stale(device_id)
            )
            if request_uuid is not None and (
                ok or existing_uuid is None or existing_is_stale
            ):
                self._sound_request_uuids[device_id] = request_uuid
                # Use getattr for test compatibility (tests may bypass __init__)
                timestamps = getattr(self, "_sound_request_timestamps", None)
                if timestamps is not None:
                    timestamps[device_id] = time.time()
                _LOGGER.debug(
                    "Stored Play Sound UUID for %s: %s", device_id, request_uuid
                )
                await self._async_save_sound_uuids()
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(
                failed=True, reason=f"Auth failed during play_sound: {auth_exc}"
            )
            try:
                await self.async_request_refresh()
            except Exception:
                pass
            return False
        except (TimeoutError, ClientConnectionError, ClientError) as conn_err:
            _LOGGER.warning(
                "Connection failed during play_sound for %s: %s",
                device_id,
                conn_err,
            )
            self.note_error(conn_err, where="async_play_sound", device=device_id)
            self._note_push_transport_problem()
            return False
        except Exception as err:
            _LOGGER.error(
                "Unexpected error during play_sound for %s: %s",
                device_id,
                err,
                exc_info=True,
            )
            self.note_error(err, where="async_play_sound", device=device_id)
            self._note_push_transport_problem()
            return False

    async def async_stop_sound(
        self,
        device_id: str,
        request_uuid: str | None = None,
    ) -> bool:
        """Stop sound on a device using the native async API (no executor).

        **IMPORTANT**: This method retrieves the UUID from the previous Play Sound request
        and uses it to cancel that specific request. Without the UUID, Google's API may
        not properly cancel the sound and the device will continue ringing.

        Args:
            device_id: The canonical ID of the device.
            request_uuid: Optional request UUID that identifies the prior play request.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
        # Less strict than can_play_sound(): stopping is harmless but still requires push readiness.
        if not self._api_push_ready():
            _LOGGER.debug(
                "Suppressing stop_sound call for %s: push not ready", device_id
            )
            return False
        request_uuid_to_use = request_uuid
        if request_uuid_to_use is None:
            cached_uuid = self._sound_request_uuids.get(device_id)
            # An aged-out key is no better than no key: the ring it referenced
            # auto-stopped long ago (the reload filter would discard it), so
            # targeting it could miss a current ring. Treat it as absent.
            if cached_uuid is not None and self._cached_sound_uuid_is_stale(device_id):
                _LOGGER.debug(
                    "Ignoring expired cached Play Sound UUID for %s", device_id
                )
                cached_uuid = None
            request_uuid_to_use = cached_uuid
            if request_uuid_to_use is not None:
                _LOGGER.debug(
                    "Using cached Play Sound UUID for %s: %s",
                    device_id,
                    request_uuid_to_use,
                )
            else:
                _LOGGER.warning(
                    "Missing Play Sound UUID for %s; attempting stop without it",
                    device_id,
                )

        try:
            ok = await self.api.async_stop_sound(device_id, request_uuid_to_use)
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            if ok:
                removed_request_uuid = self._sound_request_uuids.pop(device_id, None)
                # Use getattr for test compatibility (tests may bypass __init__)
                timestamps = getattr(self, "_sound_request_timestamps", None)
                if timestamps is not None:
                    timestamps.pop(device_id, None)
                if removed_request_uuid is not None:
                    await self._async_save_sound_uuids()
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(
                failed=True, reason=f"Auth failed during stop_sound: {auth_exc}"
            )
            try:
                await self.async_request_refresh()
            except Exception:
                pass
            return False
        except (TimeoutError, ClientConnectionError, ClientError) as conn_err:
            _LOGGER.warning(
                "Connection failed during stop_sound for %s: %s",
                device_id,
                conn_err,
            )
            self.note_error(conn_err, where="async_stop_sound", device=device_id)
            self._note_push_transport_problem()
            return False
        except Exception as err:
            _LOGGER.error(
                "Unexpected error during stop_sound for %s: %s",
                device_id,
                err,
                exc_info=True,
            )
            self.note_error(err, where="async_stop_sound", device=device_id)
            self._note_push_transport_problem()
            return False

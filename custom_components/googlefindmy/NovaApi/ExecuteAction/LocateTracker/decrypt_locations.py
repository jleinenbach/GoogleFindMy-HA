# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/decrypt_locations.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import math
import time
from itertools import zip_longest
from typing import Any, Dict, List, Optional

from google.protobuf.message import DecodeError

from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import decrypt
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import (
    decrypt_aes_gcm,
    decrypt_eik,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypted_location import (
    WrappedLocation,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
    DeviceRegistration,
)
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    parse_device_update_protobuf,
)
from custom_components.googlefindmy.SpotApi.CreateBleDevice.config import (
    mcu_fast_pair_model_id,
)
from custom_components.googlefindmy.SpotApi.CreateBleDevice.util import flip_bits
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
    async_get_eid_info,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    async_get_owner_key,
)

_LOGGER = logging.getLogger(__name__)

# Soft limit to avoid pathological payloads; large batches are unusual and heavy.
_MAX_REPORTS: int = 500

# Strict length of Ephemeral Identity Key (bytes). Paper and ecosystem practice expect 32 bytes.
_EIK_LEN: int = 32


# ---- Exceptions (specific, compatible via RuntimeError) -----------------------
class DecryptionError(RuntimeError):
    """Raised when decryption fails for reasons other than stale owner key."""


class StaleOwnerKeyError(DecryptionError):
    """Raised when the tracker was encrypted with an older owner key version."""


def _status_name_safe(code: Any) -> str:
    """Safely get the string representation of an enum, with a robust fallback."""
    try:
        return Common_pb2.Status.Name(int(code))
    except Exception:
        try:
            return str(int(code))
        except Exception:
            return str(code)


def create_google_maps_link(latitude: float, longitude: float) -> Optional[str]:
    """Return a Google Maps link for valid coordinates, otherwise None.

    Contract:
    - Returns a valid URL string, or None if coordinates are invalid.
    - Avoids mixing error strings with URLs at call sites.

    Note: Keep this for developer diagnostics (debug level only elsewhere).
    """
    try:
        lat_f = float(latitude)
        lon_f = float(longitude)
    except (TypeError, ValueError):
        _LOGGER.debug("Invalid coordinate types for Maps link; skipping link generation")
        return None

    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        _LOGGER.debug("Out-of-bounds coordinates for Maps link; skipping link generation")
        return None

    return f"https://www.google.com/maps/search/?api=1&query={lat_f},{lon_f}"


def is_mcu_tracker(device_registration: DeviceRegistration) -> bool:
    """Return True if device appears to be our custom MCU tracker."""
    return device_registration.fastPairModelId == mcu_fast_pair_model_id


async def async_retrieve_identity_key(device_registration: DeviceRegistration) -> bytes:
    """Retrieve the device Ephemeral Identity Key (EIK) asynchronously.

    Flow (async-first, HA-friendly):
    - Apply MCU bit-flip quirk to the encrypted EIK blob.
    - Obtain owner key (async).
    - Decrypt EIK (CPU-bound → offload to thread).
    - Strictly validate length to avoid silent misuse downstream.

    Raises:
        StaleOwnerKeyError: if tracker is encrypted with an older owner key.
        DecryptionError: for generic decryption failures.
        SpotApiEmptyResponseError: propagated if EID info trailers-only response indicates auth/session issue.
    """
    is_mcu = is_mcu_tracker(device_registration)
    encrypted_user_secrets = device_registration.encryptedUserSecrets

    encrypted_identity_key = flip_bits(
        encrypted_user_secrets.encryptedIdentityKey,
        is_mcu,
    )

    owner_key = await async_get_owner_key()

    try:
        # CPU-heavy → do not block the event loop
        eik_bytes = await asyncio.to_thread(
            decrypt_eik, owner_key, encrypted_identity_key
        )
        # Strict sanity: EIK must be exactly 32 bytes
        if not isinstance(eik_bytes, (bytes, bytearray)) or len(eik_bytes) != _EIK_LEN:
            raise DecryptionError(
                f"Ephemeral identity key invalid (expected {_EIK_LEN} bytes)."
            )
        return bytes(eik_bytes)
    except Exception as e:
        current_owner_key_version = None
        try:
            e2ee_data = await async_get_eid_info()
            current_owner_key_version = (
                e2ee_data.encryptedOwnerKeyAndMetadata.ownerKeyVersion
            )
            _LOGGER.debug(
                "E2EE metadata: current ownerKeyVersion=%s", current_owner_key_version
            )
        except SpotApiEmptyResponseError:
            _LOGGER.error(
                "Failed to decrypt identity key due to empty trailers-only EID info response "
                "(authentication/session). Please re-authenticate and retry."
            )
            raise
        except Exception as meta_exc:  # best-effort diagnostics
            _LOGGER.warning(
                "Failed to retrieve E2EE metadata for diagnostics: %s", meta_exc
            )

        old_ver = getattr(encrypted_user_secrets, "ownerKeyVersion", None)
        if (
            current_owner_key_version is not None
            and old_ver is not None
            and old_ver < current_owner_key_version
        ):
            _LOGGER.error(
                "Owner key version mismatch: tracker=%s, current=%s. "
                "This typically occurs after resetting E2EE data. "
                "The tracker cannot be decrypted anymore; remove it in the Find My Device app.",
                old_ver,
                current_owner_key_version,
            )
            raise StaleOwnerKeyError(
                "Tracker was encrypted with a stale owner key version."
            ) from e

        _LOGGER.error(
            "Failed to decrypt identity key (owner key version %s vs. current %s). "
            "If you recently reset E2EE data, re-authenticate or recreate keys. "
            "If the issue persists, clear the integration secrets to force a fresh key derivation.",
            old_ver,
            current_owner_key_version,
        )
        raise DecryptionError("Identity key decryption failed.") from e


def _parse_epoch_seconds(value: Any, now_s: float) -> float | None:
    """Robustly parse a Unix epoch timestamp (float) from various inputs.

    Handles int, float, str, bytes, and protobuf Time objects.
    Sanitizes strings, checks for finiteness, and applies a plausibility window.

    Returns:
        The timestamp as a float, or None if invalid or implausible.
    """
    v: float
    # Protobuf Timestamp: seconds (+ optional nanos)
    if hasattr(value, "seconds"):
        try:
            secs = float(getattr(value, "seconds"))
            nanos = float(getattr(value, "nanos", 0.0))
            v = secs + nanos / 1e9
        except (TypeError, ValueError):
            return None
    else:
        raw = value
        # Bytes -> UTF-8
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", "strict")
            except Exception:
                return None
        # Sanitize strings (Whitespace, BOM, Non-breaking space)
        if isinstance(raw, str):
            raw = raw.strip().replace("\ufeff", "").replace("\u00a0", "")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None

    # Unit heuristic (ms/µs)
    if v > 1e15:  # microseconds
        v /= 1e6
    elif v > 1e12:  # milliseconds
        v /= 1e3

    # Finite and plausibility check
    if not math.isfinite(v):
        return None
    # Plausibility: >= 2000-01-01 and <= now + 10 minutes
    if not (946684800.0 <= v <= (now_s + 600.0)):
        return None
    return v


async def _offload_decrypt_aes(
    identity_key: bytes, encrypted_location: bytes
) -> bytes:
    """Offload AES-GCM decryption; derive key hash cheaply on event loop."""
    identity_key_hash = hashlib.sha256(identity_key).digest()  # cheap hash → OK on loop
    return await asyncio.to_thread(
        decrypt_aes_gcm, identity_key_hash, encrypted_location
    )


async def _offload_decrypt_foreign(
    identity_key: bytes,
    encrypted_location: bytes,
    public_key_random: bytes,
    time_offset: int,
) -> bytes:
    """Offload ECC-based decryption for foreign reports."""
    return await asyncio.to_thread(
        decrypt, identity_key, encrypted_location, public_key_random, time_offset
    )


# ----------------------------- Validation helpers -----------------------------
def _is_valid_latlon(lat: float, lon: float) -> bool:
    """Validate latitude/longitude are finite and within geographic bounds.

    POPETS'25 notes integer-scaled coordinates (±90/±180 after scaling by 1e7).
    We validate after scaling here and fail fast on out-of-range/NaN/Inf.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return False
    return True


def _infer_report_hint(status_value: Any) -> Optional[str]:
    """Infer a throttling hint from the protobuf Status.

    Strategy:
    1) Prefer **explicit enum comparisons** (robust across locales).
    2) Fall back to **name substring checks** if enums are unavailable
       in the environment/build (defensive coding for older protobufs).

    Hints:
        - "high_traffic"  → aggregated server-side reports typically throttled more aggressively.
        - "in_all_areas"  → crowdsourced reports available broadly; back off for longer.
        - None            → unknown/irrelevant; coordinator applies no type-specific cooldown.
    """
    # --- Explicit enum mapping (robust path) -----------------------
    try:
        if int(status_value) == getattr(Common_pb2.Status, "CROWDSOURCED"):
            return "in_all_areas"
    except Exception:
        pass
    try:
        if int(status_value) == getattr(Common_pb2.Status, "AGGREGATED"):
            return "high_traffic"
    except Exception:
        pass

    # --- Conservative fallback based on enum name -------------------
    try:
        name = Common_pb2.Status.Name(int(status_value)).lower()
    except Exception:
        return None

    if "high" in name and "traffic" in name:
        return "high_traffic"
    if "in" in name and "all" in name and "areas" in name:
        return "in_all_areas"
    return None


# ----------------------------- Main decryptor ---------------------------------
async def async_decrypt_location_response_locations(
    device_update_protobuf: DeviceUpdate_pb2.DeviceUpdate,
) -> List[Dict[str, Any]]:
    """Decrypt and normalize location reports into HA-friendly dicts (async).

    Guarantees:
    - Event loop remains responsive: CPU-heavy crypto is offloaded via asyncio.to_thread().
    - Fail-fast: malformed coordinates are dropped at the decryption boundary,
      preventing bad data from leaking into higher layers (HA Platinum quality).
    - Robust against partial/invalid reports (log and continue).
    - No prints or process termination; errors bubble or are logged with context.

    POPETS'25 reference (Böttger et al., 2025):
      - Integer-scaled coordinates and validation: §4
      - "High Traffic" vs. "In All Areas" throttling semantics: §4–5
    """
    # Defensive guards on required metadata
    try:
        device_registration: DeviceRegistration = (
            device_update_protobuf.deviceMetadata.information.deviceRegistration
        )
    except Exception as exc:
        _LOGGER.error("Device registration metadata missing or invalid: %s", exc)
        raise

    identity_key = await async_retrieve_identity_key(device_registration)

    try:
        locations_proto = (
            device_update_protobuf.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
        )
    except Exception as exc:
        _LOGGER.error("Location information missing or invalid: %s", exc)
        raise

    is_mcu = is_mcu_tracker(device_registration)

    # Assemble reports (preserve semantics; own report is appended if present)
    recent_location = locations_proto.recentLocation
    recent_location_time = locations_proto.recentLocationTimestamp
    network_locations: List[Any] = list(locations_proto.networkLocations)
    network_locations_time: List[Any] = list(locations_proto.networkLocationTimestamps)
    if locations_proto.HasField("recentLocation"):
        network_locations.append(recent_location)
        network_locations_time.append(recent_location_time)

    now_wall = time.time()

    # Optional hard cap (defense-in-depth against pathological inputs)
    if len(network_locations) > _MAX_REPORTS:
        _LOGGER.warning(
            "Truncating reports: %s → %s", len(network_locations), _MAX_REPORTS
        )
        network_locations = network_locations[:_MAX_REPORTS]
        network_locations_time = network_locations_time[:_MAX_REPORTS]

    wrapped: List[WrappedLocation] = []
    if len(network_locations) != len(network_locations_time):
        _LOGGER.debug(
            "Mismatched report arrays: locations=%s timestamps=%s (dropping unmatched entries)",
            len(network_locations),
            len(network_locations_time),
        )
    for loc, time_ts in zip_longest(
        network_locations, network_locations_time, fillvalue=None
    ):
        if loc is None or time_ts is None:
            continue
        try:
            ts = _parse_epoch_seconds(time_ts, now_wall)
            if ts is None:
                _LOGGER.debug(
                    "Dropping one location report due to invalid or missing timestamp."
                )
                continue

            if loc.status == Common_pb2.Status.SEMANTIC:
                wrapped.append(
                    WrappedLocation(
                        decrypted_location=b"",
                        time=ts,
                        accuracy=0,  # Internal placeholder, not for display
                        status=loc.status,
                        is_own_report=False,  # SEMANTIC is not an Owner-Report
                        name=loc.semanticLocation.locationName,
                    )
                )
                continue

            enc = loc.geoLocation.encryptedReport
            encrypted_location: bytes = enc.encryptedLocation
            public_key_random: bytes = enc.publicKeyRandom

            if public_key_random == b"":  # Own report
                decrypted_location = await _offload_decrypt_aes(
                    identity_key, encrypted_location
                )
            else:
                time_offset = 0 if is_mcu else loc.geoLocation.deviceTimeOffset
                decrypted_location = await _offload_decrypt_foreign(
                    identity_key,
                    encrypted_location,
                    public_key_random,
                    time_offset,
                )

            wrapped.append(
                WrappedLocation(
                    decrypted_location=decrypted_location,
                    time=ts,
                    accuracy=loc.geoLocation.accuracy,
                    status=loc.status,
                    is_own_report=enc.isOwnReport,
                    name="",
                )
            )
        except Exception as one_exc:
            # Continue with other reports (per-item resilience; avoid warn spam)
            _LOGGER.debug("Failed to process one location report: %s", one_exc)

    if not wrapped:
        _LOGGER.debug("[DecryptLocations] No locations found.")
        return []

    # Convert to structured payloads for HA entities (with fail-fast validation)
    structured: List[Dict[str, Any]] = []
    for loc in wrapped:
        try:
            report_hint = _infer_report_hint(loc.status)  # may be None (conservative)

            if loc.status == Common_pb2.Status.SEMANTIC:
                payload: Dict[str, Any] = {
                    "latitude": None,
                    "longitude": None,
                    "altitude": None,
                    "accuracy": None,  # No coordinates means no meaningful accuracy
                    "last_seen": loc.time,
                    "status": _status_name_safe(loc.status),
                    "status_code": int(loc.status),
                    "is_own_report": False,
                    "semantic_name": loc.name,
                }
                # Internal hint helps the coordinator schedule throttling-aware cooldowns.
                if report_hint:
                    payload["_report_hint"] = report_hint
            else:
                proto_loc = DeviceUpdate_pb2.Location()
                try:
                    # Protobuf parsing is relatively cheap → inline
                    proto_loc.ParseFromString(loc.decrypted_location)
                except DecodeError as de:
                    _LOGGER.debug(
                        "Failed to parse Location protobuf; dropping one report: %s", de
                    )
                    continue

                # --- Fail-fast coordinate validation (POPETS'25 §4) -----------------
                # The protocol uses integer-scaled lat/lon (1e7). We validate *after* scaling.
                latitude = proto_loc.latitude / 1e7
                longitude = proto_loc.longitude / 1e7
                if not _is_valid_latlon(latitude, longitude):
                    # Keep the message non-sensitive: do not print raw coordinates.
                    _LOGGER.debug(
                        "Dropping invalid/out-of-bounds coordinates from one report"
                    )
                    continue
                # ---------------------------------------------------------------------

                altitude = proto_loc.altitude

                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Parsed valid coordinates (altitude present: %s)",
                        altitude is not None,
                    )
                    maps_link = create_google_maps_link(latitude, longitude)
                    if maps_link:
                        _LOGGER.debug("Google Maps Link: %s", maps_link)

                status_name = _status_name_safe(loc.status)
                payload = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude": altitude,
                    "accuracy": loc.accuracy,
                    "last_seen": loc.time,
                    "status": status_name,
                    "status_code": int(loc.status),
                    "is_own_report": loc.is_own_report,
                    "semantic_name": None,
                }
                if report_hint:
                    payload["_report_hint"] = report_hint

            # Log with timezone-awareness if HA util is available (debug only)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    from homeassistant.util import (
                        dt as dt_util,
                    )  # lazy import (keeps __main__ dev check usable)

                    ts_local = dt_util.as_local(
                        datetime.datetime.fromtimestamp(
                            loc.time, tz=datetime.timezone.utc
                        )
                    )
                    _LOGGER.debug(
                        "Time (local): %s | Status: %s | Own: %s",
                        ts_local,
                        loc.status,
                        loc.is_own_report,
                    )
                except Exception:
                    _LOGGER.debug(
                        "Time (epoch): %s | Status: %s | Own: %s",
                        loc.time,
                        loc.status,
                        loc.is_own_report,
                    )

            structured.append(payload)
        except Exception as one_exc:
            _LOGGER.debug(
                "Failed to convert one WrappedLocation to structured payload: %s",
                one_exc,
            )

    return structured


def decrypt_location_response_locations(
    device_update_protobuf: DeviceUpdate_pb2.DeviceUpdate,
) -> List[Dict[str, Any]]:
    """Synchronous legacy facade.

    IMPORTANT:
    - MUST NOT be called from inside Home Assistant's running event loop.
    - Prefer: `await async_decrypt_location_response_locations(...)`.

    Implementation:
    - Runs the async implementation via `asyncio.run` only if no loop is running
      in this thread. Otherwise, raises a clear RuntimeError.
    """
    try:
        asyncio.get_running_loop()  # raises RuntimeError if no loop in this thread
    except RuntimeError:
        # No running loop in this thread → safe to use asyncio.run
        return asyncio.run(
            async_decrypt_location_response_locations(device_update_protobuf)
        )
    else:
        # A loop is running in this thread → don't deadlock
        raise RuntimeError(
            "Sync decrypt_location_response_locations() used inside a running event loop. "
            "Use `await async_decrypt_location_response_locations(...)` instead."
        )


if __name__ == "__main__":  # Developer self-check only; not used by Home Assistant
    res = parse_device_update_protobuf("")  # type: ignore[arg-type]
    try:
        decrypt_location_response_locations(res)
    except Exception as exc:
        print(f"Self-check encountered exception (expected outside HA runtime): {exc}")

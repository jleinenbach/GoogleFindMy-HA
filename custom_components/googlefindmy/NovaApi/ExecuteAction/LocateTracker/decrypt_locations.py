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
from typing import Optional, List, Dict, Any, Iterable

from google.protobuf.message import DecodeError

from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import decrypt
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import decrypt_eik, decrypt_aes_gcm
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypted_location import WrappedLocation
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders import Common_pb2
from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import DeviceRegistration
from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf
from custom_components.googlefindmy.SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id
from custom_components.googlefindmy.SpotApi.CreateBleDevice.util import flip_bits
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    async_get_eid_info,
    SpotApiEmptyResponseError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    async_get_owner_key,
)

_LOGGER = logging.getLogger(__name__)

# Soft limit to avoid pathological payloads; large batches are unusual and heavy.
_MAX_REPORTS: int = 500


# ---- Exceptions (specific, compatible via RuntimeError) -----------------------
class DecryptionError(RuntimeError):
    """Raised when decryption fails for reasons other than stale owner key."""


class StaleOwnerKeyError(DecryptionError):
    """Raised when the tracker was encrypted with an older owner key version."""


def create_google_maps_link(latitude: float, longitude: float) -> Optional[str]:
    """Return a Google Maps link for valid coordinates, otherwise None.

    Contract:
    - Returns a valid URL string, or None if coordinates are invalid.
    - Avoids mixing error strings with URLs at call sites.
    """
    try:
        lat_f = float(latitude)
        lon_f = float(longitude)
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid coordinate types for Maps link: lat=%r, lon=%r", latitude, longitude)
        return None

    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        _LOGGER.warning("Invalid coordinate values for Maps link: lat=%s, lon=%s", lat_f, lon_f)
        return None

    return f"https://www.google.com/maps/search/?api=1&query={lat_f},{lon_f}"


def is_mcu_tracker(device_registration: DeviceRegistration) -> bool:
    """Return True if device appears to be our custom MCU tracker."""
    return device_registration.fastPairModelId == mcu_fast_pair_model_id


async def async_retrieve_identity_key(device_registration: DeviceRegistration) -> bytes:
    """Derive the device identity key (async).

    Flow:
    - Apply MCU bit-flip quirk.
    - Obtain owner key (async).
    - Decrypt EIK (CPU-bound → offload to thread).

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
        identity_key = await asyncio.to_thread(decrypt_eik, owner_key, encrypted_identity_key)
        # Basic sanity (defensive): expected sizes are typically 32 bytes; avoid hard fail if library changes.
        if not isinstance(identity_key, (bytes, bytearray)) or len(identity_key) < 16:
            raise DecryptionError("Identity key looks invalid or truncated.")
        return bytes(identity_key)
    except Exception as e:
        current_owner_key_version = None
        try:
            e2ee_data = await async_get_eid_info()
            current_owner_key_version = e2ee_data.encryptedOwnerKeyAndMetadata.ownerKeyVersion
            _LOGGER.debug("E2EE metadata: current ownerKeyVersion=%s", current_owner_key_version)
        except SpotApiEmptyResponseError:
            _LOGGER.error(
                "Failed to decrypt identity key due to empty trailers-only EID info response "
                "(authentication/session). Please re-authenticate and retry."
            )
            raise
        except Exception as meta_exc:  # best-effort diagnostics
            _LOGGER.warning("Failed to retrieve E2EE metadata for diagnostics: %s", meta_exc)

        old_ver = getattr(encrypted_user_secrets, "ownerKeyVersion", None)
        if current_owner_key_version is not None and old_ver is not None and old_ver < current_owner_key_version:
            _LOGGER.error(
                "Owner key version mismatch: tracker=%s, current=%s. "
                "This typically occurs after resetting E2EE data. "
                "The tracker cannot be decrypted anymore; remove it in the Find My Device app.",
                old_ver, current_owner_key_version,
            )
            raise StaleOwnerKeyError("Tracker was encrypted with a stale owner key version.") from e

        _LOGGER.error(
            "Failed to decrypt identity key (owner key version %s vs. current %s). "
            "If you recently reset E2EE data, re-authenticate or recreate keys. "
            "If the issue persists, clear the integration secrets to force a fresh key derivation.",
            old_ver, current_owner_key_version,
        )
        raise DecryptionError("Identity key decryption failed.") from e


def _normalize_ts_seconds(ts_obj: Any) -> int:
    """Extract seconds from a protobuf-like timestamp safely (non-negative int)."""
    try:
        seconds = int(getattr(ts_obj, "seconds", 0) or 0)
    except Exception:
        seconds = 0
    return max(0, seconds)


async def _offload_decrypt_aes(identity_key: bytes, encrypted_location: bytes) -> bytes:
    """Offload AES-GCM decryption; derive key hash cheaply on event loop."""
    identity_key_hash = hashlib.sha256(identity_key).digest()  # cheap hash → OK on loop
    return await asyncio.to_thread(decrypt_aes_gcm, identity_key_hash, encrypted_location)


async def _offload_decrypt_foreign(identity_key: bytes, encrypted_location: bytes, public_key_random: bytes, time_offset: int) -> bytes:
    """Offload ECC-based decryption for foreign reports."""
    return await asyncio.to_thread(decrypt, identity_key, encrypted_location, public_key_random, time_offset)


async def async_decrypt_location_response_locations(
    device_update_protobuf: DeviceUpdate_pb2.DeviceUpdate,
) -> List[Dict[str, Any]]:
    """Decrypt and normalize location reports into HA-friendly dicts (async).

    Guarantees:
    - Event loop remains responsive: CPU-heavy crypto is offloaded via asyncio.to_thread().
    - Robust against partial/invalid reports (warn and continue).
    - No prints or process termination; errors bubble or are logged with context.
    """
    # Defensive guards on required metadata
    try:
        device_registration: DeviceRegistration = device_update_protobuf.deviceMetadata.information.deviceRegistration
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

    # Optional hard cap (defense-in-depth against pathological inputs)
    if len(network_locations) > _MAX_REPORTS:
        _LOGGER.warning("Truncating reports: %s → %s", len(network_locations), _MAX_REPORTS)
        network_locations = network_locations[:_MAX_REPORTS]
        network_locations_time = network_locations_time[:_MAX_REPORTS]

    wrapped: List[WrappedLocation] = []
    for loc, time_ts in zip(network_locations, network_locations_time):
        try:
            ts = _normalize_ts_seconds(time_ts)

            if loc.status == Common_pb2.Status.SEMANTIC:
                wrapped.append(
                    WrappedLocation(
                        decrypted_location=b"",
                        time=ts,
                        accuracy=0,
                        status=loc.status,
                        is_own_report=True,
                        name=loc.semanticLocation.locationName,
                    )
                )
                continue

            enc = loc.geoLocation.encryptedReport
            encrypted_location: bytes = enc.encryptedLocation
            public_key_random: bytes = enc.publicKeyRandom

            if public_key_random == b"":  # Own report
                decrypted_location = await _offload_decrypt_aes(identity_key, encrypted_location)
            else:
                time_offset = 0 if is_mcu else loc.geoLocation.deviceTimeOffset
                decrypted_location = await _offload_decrypt_foreign(
                    identity_key, encrypted_location, public_key_random, time_offset
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
            # Continue with other reports (resilience)
            _LOGGER.warning("Failed to process one location report: %s", one_exc)

    if not wrapped:
        _LOGGER.debug("[DecryptLocations] No locations found.")
        return []

    # Convert to structured payloads for HA entities
    structured: List[Dict[str, Any]] = []
    for loc in wrapped:
        try:
            if loc.status == Common_pb2.Status.SEMANTIC:
                payload: Dict[str, Any] = {
                    "latitude": None,
                    "longitude": None,
                    "altitude": None,
                    "accuracy": loc.accuracy,
                    "last_seen": loc.time,
                    "status": str(loc.status),
                    "is_own_report": loc.is_own_report,
                    "semantic_name": loc.name,
                }
            else:
                proto_loc = DeviceUpdate_pb2.Location()
                try:
                    # Protobuf parsing is relatively cheap → inline
                    proto_loc.ParseFromString(loc.decrypted_location)
                except DecodeError as de:
                    _LOGGER.warning("Failed to parse Location protobuf: %s", de)
                    continue

                latitude = proto_loc.latitude / 1e7
                longitude = proto_loc.longitude / 1e7
                altitude = proto_loc.altitude

                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Latitude: %s | Longitude: %s | Altitude: %s", latitude, longitude, altitude)
                    maps_link = create_google_maps_link(latitude, longitude)
                    if maps_link:
                        _LOGGER.debug("Google Maps Link: %s", maps_link)

                payload = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude": altitude,
                    "accuracy": loc.accuracy,
                    "last_seen": loc.time,
                    "status": str(loc.status),
                    "is_own_report": loc.is_own_report,
                    "semantic_name": None,
                }

            # Log with timezone-awareness if HA util is available (debug only)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    from homeassistant.util import dt as dt_util  # lazy import (keeps __main__ dev check usable)
                    ts_local = dt_util.as_local(datetime.datetime.fromtimestamp(loc.time, tz=datetime.timezone.utc))
                    _LOGGER.debug("Time (local): %s | Status: %s | Own: %s", ts_local, loc.status, loc.is_own_report)
                except Exception:
                    _LOGGER.debug(
                        "Time (epoch): %s | Status: %s | Own: %s",
                        loc.time,
                        loc.status,
                        loc.is_own_report,
                    )

            structured.append(payload)
        except Exception as one_exc:
            _LOGGER.warning("Failed to convert one WrappedLocation to structured payload: %s", one_exc)

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
        return asyncio.run(async_decrypt_location_response_locations(device_update_protobuf))
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

# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/decrypt_locations.py
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

# ruff: noqa: PLR0911, PLR0912, PLR0915
import asyncio
import datetime
import hashlib
import logging
import math
import time
from importlib import import_module
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, cast

from custom_components.googlefindmy import get_proto_decoder
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import decrypt
from custom_components.googlefindmy.FMDNCrypto.mcu_utils import (
    flip_bits,
    is_mcu_tracker,
)
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import (
    EIK_GCM_TOTAL_LEN,
    decrypt_aes_gcm,
    decrypt_eik,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypted_location import (
    WrappedLocation,
)
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    parse_device_update_protobuf,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
    async_get_eid_info,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
    async_get_owner_key,
)
from google.protobuf.message import DecodeError

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache
    from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
        DeviceRegistration as DeviceRegistrationMessage,
    )
    from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
        DeviceUpdate as DeviceUpdateMessage,
    )
else:
    DeviceRegistrationMessage = Any
    DeviceUpdateMessage = Any

DeviceRegistration = DeviceRegistrationMessage
DeviceUpdateProto = DeviceUpdateMessage

_LAT_MIN = -90.0
_LAT_MAX = 90.0
_LON_MIN = -180.0
_LON_MAX = 180.0
_MICROSECONDS_THRESHOLD = 1e15
_MILLISECONDS_THRESHOLD = 1e12
_MIN_VALID_EPOCH_S = 946684800.0  # 2000-01-01

# Acceptable future drift for timestamps to accommodate timezone offsets and
# clock skew while still rejecting obviously invalid data is centralized in
# MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S (const.py) so downstream components stay
# aligned on the same acceptance window.
_LOGGER = logging.getLogger(__name__)
# Soft limit to avoid pathological payloads; large batches are unusual and heavy.
_MAX_REPORTS: int = 500

# Strict length of Ephemeral Identity Key (bytes). Paper and ecosystem practice expect 32 bytes.
_EIK_LEN: int = 32
# Heuristic threshold suggesting encryptedUserSecrets holds structured data rather than a raw key blob.
# Moto Tag payloads can legitimately exceed the smaller legacy cutoff, so tolerate larger blobs before
# raising a diagnostic warning.
_SECRETS_STRUCT_LEN_THRESHOLD: int = 256


def _get_common_pb2() -> Any:
    """Return the Common_pb2 module lazily."""

    return get_proto_decoder("Common_pb2")


def _get_device_update_pb2() -> Any:
    """Return the DeviceUpdate_pb2 module lazily."""

    return get_proto_decoder("DeviceUpdate_pb2")


# ---- Exceptions (specific, compatible via RuntimeError) -----------------------
class DecryptionError(RuntimeError):
    """Raised when decryption fails for reasons other than stale owner key."""


class StaleOwnerKeyError(DecryptionError):
    """Raised when the tracker was encrypted with an older owner key version."""


async def _unwrap_encrypted_identity_key(
    identity_key: bytes, *, cache: TokenCache
) -> bytes | None:
    """Unwrap a 60-byte encrypted identity key into a 32-byte EIK if possible."""

    if len(identity_key) != EIK_GCM_TOTAL_LEN:
        return None

    try:
        owner_key_info = await async_get_owner_key(cache=cache)
    except Exception as exc:  # pragma: no cover - defensive diagnostic path
        _LOGGER.debug("[DECRYPT] Owner key lookup failed while unwrapping EIK: %s", exc)
        return None

    try:
        decrypted_eik = await asyncio.to_thread(
            decrypt_eik, owner_key_info.key, identity_key
        )
    except Exception as exc:  # pragma: no cover - defensive diagnostic path
        _LOGGER.debug("[DECRYPT] EIK unwrap failed in thread: %s", exc)
        return None

    if len(decrypted_eik) != _EIK_LEN:
        _LOGGER.debug(
            "[DECRYPT] Unwrapped EIK length %s does not match expected %s bytes",
            len(decrypted_eik),
            _EIK_LEN,
        )
        return None

    _LOGGER.debug("[DECRYPT] Successfully unwrapped 60-byte EIK to 32 bytes (early path).")
    return bytes(decrypted_eik)


def _status_name_safe(code: Any) -> str:
    """Safely get the string representation of an enum, with a robust fallback."""
    try:
        common_pb2 = _get_common_pb2()
        status_name: str = common_pb2.Status.Name(int(code))
        return status_name
    except Exception:
        try:
            return str(int(code))
        except Exception:
            return str(code)


def create_google_maps_link(latitude: float, longitude: float) -> str | None:
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
        _LOGGER.debug(
            "Invalid coordinate types for Maps link; skipping link generation"
        )
        return None

    if not (_LAT_MIN <= lat_f <= _LAT_MAX and _LON_MIN <= lon_f <= _LON_MAX):
        _LOGGER.debug(
            "Out-of-bounds coordinates for Maps link; skipping link generation"
        )
        return None

    return f"https://www.google.com/maps/search/?api=1&query={lat_f},{lon_f}"


async def async_retrieve_identity_key(
    device_registration: DeviceRegistration,
    *,
    cache: TokenCache,
    _retry: bool = True,
) -> list[bytes]:  # noqa: PLR0912, PLR0915
    """Retrieve the device Ephemeral Identity Key (EIK) asynchronously.

    Flow (async-first, HA-friendly):
    - Try both MCU bit-flip states to derive candidate keys.
    - Obtain owner key (async).
    - Decrypt each candidate EIK (CPU-bound → offload to thread).
    - Strictly validate length to avoid silent misuse downstream.

    Args:
        device_registration: Tracker registration metadata containing the encrypted EIK.
        cache: Entry-scoped TokenCache used for owner key and username resolution.

    Raises:
        StaleOwnerKeyError: if tracker is encrypted with an older owner key.
        DecryptionError: for generic decryption failures.
        SpotApiEmptyResponseError: propagated if EID info trailers-only response indicates auth/session issue.
        RuntimeError: if the TokenCache is missing (multi-account safety guard).
    """
    encrypted_user_secrets = device_registration.encryptedUserSecrets

    raw_encrypted_identity_key = encrypted_user_secrets.encryptedIdentityKey

    if cache is None:
        raise RuntimeError(
            "TokenCache instance is required to retrieve the tracker identity key."
        )

    owner_key_info: OwnerKeyInfo = await async_get_owner_key(cache=cache)
    candidates: list[bytes] = []
    decrypt_errors: list[Exception] = []

    for do_flip in (False, True):
        flipped_blob = flip_bits(raw_encrypted_identity_key, do_flip)
        try:
            # CPU-heavy → do not block the event loop
            eik_bytes = await asyncio.to_thread(
                decrypt_eik, owner_key_info.key, flipped_blob
            )
            if (
                not isinstance(eik_bytes, (bytes, bytearray))
                or len(eik_bytes) != _EIK_LEN
            ):
                raise DecryptionError(
                    f"Ephemeral identity key invalid (expected {_EIK_LEN} bytes)."
                )

            key_bytes = bytes(eik_bytes)
            if key_bytes not in candidates:
                candidates.append(key_bytes)
        except Exception as exc:  # Capture and continue to try the other flip state
            decrypt_errors.append(exc)

    if candidates and not decrypt_errors:
        return candidates

    current_owner_key_version = None
    try:
        e2ee_data = await async_get_eid_info(cache=cache)
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
    last_exc = decrypt_errors[-1] if decrypt_errors else None
    if (
        current_owner_key_version is not None
        and old_ver is not None
        and old_ver < current_owner_key_version
    ):
        if _retry:
            username = None
            cache_key = None
            try:
                username = await cache.get(username_string)
            except Exception as cache_exc:
                _LOGGER.debug(
                    "Failed to resolve username from cache before clearing owner key: %s",
                    cache_exc,
                )

            if isinstance(username, str) and username:
                cache_key = f"owner_key_{username}"

            _LOGGER.info(
                "Owner key version mismatch (tracker=%s, current=%s); %s and retrying once.",
                old_ver,
                current_owner_key_version,
                "clearing cached owner key"
                if cache_key
                else "retrying with fresh owner key",
            )

            if cache_key:
                try:
                    # TokenCache clears entries when the value is set to None
                    await cache.set(cache_key, None)
                except Exception as cache_exc:
                    _LOGGER.debug("Failed to clear cached owner key: %s", cache_exc)

            return await async_retrieve_identity_key(
                device_registration,
                cache=cache,
                _retry=False,
            )

        _LOGGER.error(
            "Owner key version mismatch: tracker=%s, current=%s. "
            "This typically occurs after resetting E2EE data. "
            "The tracker cannot be decrypted anymore; remove it in the Find My Device app.",
            old_ver,
            current_owner_key_version,
        )
        raise StaleOwnerKeyError(
            "Tracker was encrypted with a stale owner key version."
        ) from last_exc

    if candidates:
        return candidates

    _LOGGER.error(
        "Failed to decrypt identity key (owner key version %s vs. current %s). "
        "If you recently reset E2EE data, re-authenticate or recreate keys. "
        "If the issue persists, clear the integration secrets to force a fresh key derivation.",
        old_ver,
        current_owner_key_version,
    )
    raise DecryptionError("Identity key decryption failed.") from last_exc


def retrieve_identity_key(
    device_registration: DeviceRegistration,
    *,
    cache: TokenCache | None = None,
) -> list[bytes]:
    """Legacy synchronous facade removed in favor of the async API."""

    raise RuntimeError(
        "Legacy sync retrieve_identity_key() has been removed. "
        "Use await async_retrieve_identity_key(..., cache=...) instead."
    )


def _parse_epoch_seconds(value: Any, now_s: float) -> float | None:  # noqa: PLR0911
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

    # Unit heuristic (ms/μs)
    if v > _MICROSECONDS_THRESHOLD:  # microseconds
        v /= 1e6
    elif v > _MILLISECONDS_THRESHOLD:  # milliseconds
        v /= 1e3

    # Finite and plausibility check
    if not math.isfinite(v):
        return None
    # Plausibility: >= 2000-01-01 and <= now + realistic drift window
    if v < _MIN_VALID_EPOCH_S:
        return None
    if v > (now_s + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S):
        return None
    return v


def normalize_pair_date_value(raw: Any, *, now_wall: float) -> int | None:
    """Normalize pairDate values that may include millisecond or microsecond units."""

    if raw is None:
        return None

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(raw):
            return None

        value = float(raw)
        if value > _MICROSECONDS_THRESHOLD:
            value /= 1e6
        elif value > _MILLISECONDS_THRESHOLD:
            value /= 1e3

        if value < _MIN_VALID_EPOCH_S:
            return None
        if value > (now_wall + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S):
            return None
        return int(value)

    ts = _parse_epoch_seconds(raw, now_wall)
    if ts is None:
        return None
    return int(ts)


def normalize_creation_timestamp_value(raw: Any, *, now_wall: float) -> int | None:
    """Normalize encryptedUserSecrets.creationDate inputs."""

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(raw):
            return None

        value = float(raw)
        if value > _MICROSECONDS_THRESHOLD:
            value /= 1e6
        elif value > _MILLISECONDS_THRESHOLD:
            value /= 1e3

        if value < _MIN_VALID_EPOCH_S:
            return None
        if value > (now_wall + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S):
            return None
        return int(value)

    ts = _parse_epoch_seconds(raw, now_wall)
    if ts is None:
        return None
    return int(ts)


async def _offload_decrypt_aes(identity_key: bytes, encrypted_location: bytes) -> bytes:
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
def _ensure_bytes(value: object) -> bytes | None:
    """Return a ``bytes`` instance when the value can be losslessly coerced."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return None


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
    if not (_LAT_MIN <= lat_f <= _LAT_MAX and _LON_MIN <= lon_f <= _LON_MAX):
        return False
    return True


def _infer_report_hint(status_value: Any) -> str | None:
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
    common_pb2 = _get_common_pb2()
    try:
        if int(status_value) == getattr(common_pb2.Status, "CROWDSOURCED"):
            return "in_all_areas"
    except Exception:
        pass
    try:
        if int(status_value) == getattr(common_pb2.Status, "AGGREGATED"):
            return "high_traffic"
    except Exception:
        pass

    # --- Conservative fallback based on enum name -------------------
    try:
        name = common_pb2.Status.Name(int(status_value)).lower()
    except Exception:
        return None

    if "high" in name and "traffic" in name:
        return "high_traffic"
    if "in" in name and "all" in name and "areas" in name:
        return "in_all_areas"
    return None


# ----------------------------- Main decryptor ---------------------------------
async def async_decrypt_location_response_locations(  # noqa: PLR0912, PLR0915
    device_update_protobuf: DeviceUpdateProto, *, cache: TokenCache
) -> list[dict[str, Any]]:
    """Decrypt and normalize location reports into HA-friendly dicts (async).

    Guarantees:
    - Event loop remains responsive: CPU-heavy crypto is offloaded via asyncio.to_thread().
    - Fail-fast: malformed coordinates are dropped at the decryption boundary,
      preventing bad data from leaking into higher layers (HA Platinum quality).
    - Robust against partial/invalid reports (log and continue).
    - No prints or process termination; errors bubble or are logged with context.

    Args:
        device_update_protobuf: Raw protobuf payload containing encrypted locations.
        cache: Entry-scoped TokenCache forwarded to key/EID helpers.

    POPETS'25 reference (Böttger et al., 2025):
      - Integer-scaled coordinates and validation: §4
      - "High Traffic" vs. "In All Areas" throttling semantics: §4–5
    """
    common_pb2 = _get_common_pb2()
    device_update_pb2 = _get_device_update_pb2()
    # Defensive guards on required metadata
    try:
        device_registration: DeviceRegistration = (
            device_update_protobuf.deviceMetadata.information.deviceRegistration
        )
    except Exception as exc:
        _LOGGER.error("Device registration metadata missing or invalid: %s", exc)
        raise

    encrypted_user_secrets = device_registration.encryptedUserSecrets
    raw_encrypted_identity_key: bytes = b""
    early_unwrapped_identity_key: bytes | None = None
    try:
        raw_encrypted_identity_key = getattr(
            encrypted_user_secrets, "encryptedIdentityKey", b""
        )
        serialized_length = None
        secrets_blob: bytes | None = None
        try:
            secrets_blob = encrypted_user_secrets.SerializeToString()
            serialized_length = len(secrets_blob)
        except Exception as serialize_exc:  # pragma: no cover - diagnostics only
            _LOGGER.debug(
                "Failed to serialize encryptedUserSecrets for length check: %s",
                serialize_exc,
            )

        if raw_encrypted_identity_key and len(raw_encrypted_identity_key) == EIK_GCM_TOTAL_LEN:
            early_unwrapped_identity_key = await _unwrap_encrypted_identity_key(
                raw_encrypted_identity_key, cache=cache
            )

        _LOGGER.warning(
            "[DIAG-SECRETS] Structure Analysis:\n"
            "  - DeviceReg String: %s\n"
            "  - Secrets Container Type: %s\n"
            "  - Secrets Serialized Length: %s bytes\n"
            "  - EncryptedIdentityKey Length: %s bytes\n"
            "  - EncryptedIdentityKey Type: %s\n"
            "  - EncryptedIdentityKey Hex: %s",
            device_registration,
            type(encrypted_user_secrets),
            serialized_length if serialized_length is not None else "Unknown",
            len(raw_encrypted_identity_key) if raw_encrypted_identity_key else "None",
            type(raw_encrypted_identity_key),
            raw_encrypted_identity_key.hex() if raw_encrypted_identity_key else "None",
        )

        if (
            serialized_length is not None
            and serialized_length > _SECRETS_STRUCT_LEN_THRESHOLD
        ):
            _LOGGER.warning(
                "[DIAG-ALERT] encryptedUserSecrets serialized length is %d bytes (> %d)."
                " This suggests a wrapped/structured payload instead of a raw key.",
                serialized_length,
                _SECRETS_STRUCT_LEN_THRESHOLD,
            )

        if (
            early_unwrapped_identity_key is None
            and raw_encrypted_identity_key
            and len(raw_encrypted_identity_key) != _EIK_LEN
        ):
            _LOGGER.warning(
                "[DIAG-ALERT] Key length is %d (expected %d for raw EIK)."
                " This suggests wrapping/encryption!",
                len(raw_encrypted_identity_key),
                _EIK_LEN,
            )

        if (
            early_unwrapped_identity_key is None
            and raw_encrypted_identity_key
            and len(raw_encrypted_identity_key) > _EIK_LEN
        ):
            _LOGGER.warning(
                "[DIAG-ALERT] Key length %d bytes exceeds expected raw EIK length (%d)."
                " Probable wrapped key container or HKDF-required envelope.",
                len(raw_encrypted_identity_key),
                _EIK_LEN,
            )

        if secrets_blob is not None and raw_encrypted_identity_key:
            if raw_encrypted_identity_key in secrets_blob:
                offset = secrets_blob.find(raw_encrypted_identity_key)
                prefix_start = max(0, offset - 10)
                prefix_bytes = secrets_blob[prefix_start:offset]
                suffix_start = offset + len(raw_encrypted_identity_key)
                suffix_bytes = secrets_blob[suffix_start : suffix_start + 10]
                _LOGGER.warning(
                    "[DIAG-SECRETS-BYTE-SCAN] Cloud key located inside encryptedUserSecrets at offset %d."
                    " Prefix (%d bytes): %s | Suffix (%d bytes): %s",
                    offset,
                    len(prefix_bytes),
                    prefix_bytes.hex(),
                    len(suffix_bytes),
                    suffix_bytes.hex(),
                )
            else:
                _LOGGER.warning(
                    "[DIAG-SECRETS-BYTE-SCAN] Cloud key NOT found inside encryptedUserSecrets blob."
                    " This suggests the blob holds a distinct container or wrapped value."
                )
    except Exception as exc:  # pragma: no cover - diagnostics only
        _LOGGER.warning("[DIAG-ERROR] Failed to inspect secrets: %s", exc)
        raw_encrypted_identity_key = b""

    raw_encrypted_identity_key = bytes(raw_encrypted_identity_key)
    raw_owner_key_version = getattr(encrypted_user_secrets, "ownerKeyVersion", None)

    identity_key_candidates = (
        [early_unwrapped_identity_key]
        if early_unwrapped_identity_key is not None
        else await async_retrieve_identity_key(device_registration, cache=cache)
    )
    identity_key = identity_key_candidates[0] if identity_key_candidates else None
    identity_key_bytes = bytes(identity_key) if identity_key is not None else None
    identity_key_candidate_bytes = [
        bytes(candidate) for candidate in identity_key_candidates
    ]

    # Handle Moto Tag / Chipolo-style 60-byte EIK wrappers by unwrapping to 32 bytes.
    if identity_key_bytes and len(identity_key_bytes) == EIK_GCM_TOTAL_LEN and cache:
        try:
            owner_key_info = await async_get_owner_key(cache=cache)
            decrypted_identity_key = await asyncio.to_thread(
                decrypt_eik, owner_key_info.key, identity_key_bytes
            )
            if len(decrypted_identity_key) == _EIK_LEN:
                _LOGGER.debug("[DECRYPT] Successfully unwrapped 60-byte EIK.")
                identity_key_bytes = bytes(decrypted_identity_key)
                identity_key_candidate_bytes = [identity_key_bytes]
                identity_key = identity_key_bytes
        except Exception as exc:  # pragma: no cover - diagnostics only
            _LOGGER.debug("[DECRYPT] Failed to unwrap: %s", exc)

    if identity_key is None:
        raise DecryptionError("Identity key derivation returned no candidates.")

    try:
        locations_proto = device_update_protobuf.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    except Exception as exc:
        _LOGGER.error("Location information missing or invalid: %s", exc)
        raise

    is_mcu = is_mcu_tracker(device_registration)

    now_wall = time.time()
    metadata: dict[str, Any] = {}
    metadata_update: dict[str, Any] = {}
    device_registration_metadata: dict[str, Any] = {}
    encrypted_user_secrets_metadata: dict[str, Any] = {}
    device_type_information_metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # PERSISTENCE DATA EXTRACTION (Reboot Bug fix)
    # Preserve pairing and secrets metadata as soon as the protobuf is
    # parsed so the coordinator can persist the refreshed crypto material
    # to the device registry. Without this step, HA restarts lose the
    # decrypted identity key and creation date (Moto Tag / Chipolo lock-on
    # fails).
    # ------------------------------------------------------------------
    if device_registration:
        pair_date_raw = getattr(device_registration, "pairDate", None)
        if pair_date_raw is not None:
            metadata_update.setdefault("pair_date", pair_date_raw)
            metadata_update.setdefault("pairDate", pair_date_raw)

    if encrypted_user_secrets:
        creation = getattr(encrypted_user_secrets, "creationDate", None)
        if creation is not None:
            creation_seconds = getattr(creation, "seconds", None)
            if creation_seconds is not None:
                metadata_update.setdefault("secrets_creation_date", creation_seconds)
                metadata_update.setdefault("secretsCreationDate", creation_seconds)
                metadata_update.setdefault("creationDate", creation_seconds)
                metadata_update.setdefault("creation_date", creation_seconds)

    device_type_information = getattr(device_update_protobuf, "deviceTypeInformation", None)

    pair_date_sources = [
        getattr(device_registration, "pairDate", None),
        getattr(device_update_protobuf, "pairDate", None),
        getattr(device_type_information, "pairDate", None),
    ]

    pair_date: int | None = None
    for candidate in pair_date_sources:
        pair_date = normalize_pair_date_value(candidate, now_wall=now_wall)
        if pair_date is not None:
            break

    if pair_date is not None:
        metadata_update["pair_date"] = pair_date
        metadata_update["pairDate"] = pair_date
        device_registration_metadata["pairDate"] = pair_date
        if device_type_information is not None:
            device_type_information_metadata["pairDate"] = pair_date

    creation_date_sources = [
        getattr(encrypted_user_secrets, "creationDate", None),
        getattr(getattr(device_update_protobuf, "encryptedUserSecrets", None), "creationDate", None),
        getattr(
            getattr(device_type_information, "encryptedUserSecrets", None),
            "creationDate",
            None,
        ),
    ]

    secrets_creation_date: int | None = None
    for candidate in creation_date_sources:
        secrets_creation_date = normalize_creation_timestamp_value(
            candidate, now_wall=now_wall
        )
        if secrets_creation_date is not None:
            break

    if secrets_creation_date is not None:
        metadata_update["secrets_creation_date"] = secrets_creation_date
        metadata_update["secretsCreationDate"] = secrets_creation_date
        metadata_update["creationDate"] = secrets_creation_date
        metadata_update["creation_date"] = secrets_creation_date
        encrypted_user_secrets_metadata["creationDate"] = secrets_creation_date
        encrypted_user_secrets_metadata["creation_date"] = secrets_creation_date
        if device_type_information is not None:
            device_type_information_metadata["creationDate"] = secrets_creation_date

    if device_registration_metadata:
        metadata["device_registration"] = device_registration_metadata
        metadata.setdefault("deviceRegistration", device_registration_metadata)

    if encrypted_user_secrets_metadata:
        metadata["encrypted_user_secrets"] = encrypted_user_secrets_metadata
        metadata.setdefault("encryptedUserSecrets", encrypted_user_secrets_metadata)

    if device_type_information_metadata:
        metadata["device_type_information"] = device_type_information_metadata
        metadata.setdefault("deviceTypeInformation", device_type_information_metadata)

    if identity_key_bytes is not None and len(identity_key_bytes) == _EIK_LEN:
        metadata_update["identity_key"] = identity_key_bytes
        metadata_update["identityKey"] = identity_key_bytes
    if identity_key_candidate_bytes:
        metadata.setdefault("identity_key_candidates", identity_key_candidate_bytes)
        metadata.setdefault("identityKeyCandidates", identity_key_candidate_bytes)
    if raw_encrypted_identity_key:
        metadata_update.setdefault("encrypted_identity_key", raw_encrypted_identity_key)
        metadata_update.setdefault("encryptedIdentityKey", raw_encrypted_identity_key)
    if raw_owner_key_version is not None:
        metadata.setdefault("owner_key_version", raw_owner_key_version)
        metadata.setdefault("ownerKeyVersion", raw_owner_key_version)

    if metadata_update:
        metadata.update(metadata_update)

    # Assemble reports (preserve semantics; own report is appended if present)
    recent_location = locations_proto.recentLocation
    recent_location_time = locations_proto.recentLocationTimestamp
    network_locations: list[Any] = list(locations_proto.networkLocations)
    network_locations_time: list[Any] = list(locations_proto.networkLocationTimestamps)

    len_diff = len(network_locations) - len(network_locations_time)
    if len_diff > 0:
        network_locations_time.extend([None] * len_diff)
    elif len_diff < 0:
        network_locations_time = network_locations_time[: len(network_locations)]

    if locations_proto.HasField("recentLocation"):
        network_locations.append(recent_location)
        network_locations_time.append(recent_location_time)

    # Optional hard cap (defense-in-depth against pathological inputs)
    if len(network_locations) > _MAX_REPORTS:
        _LOGGER.warning(
            "Truncating reports: %s → %s", len(network_locations), _MAX_REPORTS
        )
        network_locations = network_locations[:_MAX_REPORTS]
        network_locations_time = network_locations_time[:_MAX_REPORTS]

    wrapped: list[WrappedLocation] = []
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

            if loc.status == common_pb2.Status.SEMANTIC:
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
                decrypted_location_raw = await _offload_decrypt_aes(
                    identity_key, encrypted_location
                )
            else:
                time_offset = 0 if is_mcu else loc.geoLocation.deviceTimeOffset
                decrypted_location_raw = await _offload_decrypt_foreign(
                    identity_key,
                    encrypted_location,
                    public_key_random,
                    time_offset,
                )

            decrypted_location = _ensure_bytes(decrypted_location_raw)
            if decrypted_location is None:
                _LOGGER.debug(
                    "Decrypted location payload is not bytes; dropping one report"
                )
                continue

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
        if metadata:
            metadata_only = dict(metadata)
            metadata_only["metadata_only"] = True
            return [metadata_only]
        return []

    # Convert to structured payloads for HA entities (with fail-fast validation)
    structured: list[dict[str, Any]] = []
    for loc in wrapped:
        try:
            report_hint = _infer_report_hint(loc.status)  # may be None (conservative)

            try:
                setattr(loc, "pair_date", metadata_update.get("pair_date"))
                setattr(
                    loc,
                    "secrets_creation_date",
                    metadata_update.get("secrets_creation_date"),
                )
                setattr(loc, "identity_key", identity_key_bytes)
                _LOGGER.debug("Injected fresh metadata into location object.")
            except Exception as metadata_exc:  # pragma: no cover - diagnostic safety
                _LOGGER.debug("Failed to inject metadata: %s", metadata_exc)

            if loc.status == common_pb2.Status.SEMANTIC:
                payload: dict[str, Any] = {
                    "latitude": None,
                    "longitude": None,
                    "altitude": None,
                    "accuracy": None,  # No coordinates means no meaningful accuracy
                    "last_seen": loc.time,
                    "status": _status_name_safe(loc.status),
                    "status_code": int(loc.status),
                    "is_own_report": False,
                    "semantic_name": loc.name,
                    "encrypted_identity_key": raw_encrypted_identity_key,
                    "owner_key_version": raw_owner_key_version,
                    "identity_key": identity_key_bytes,
                    "identity_key_candidates": identity_key_candidate_bytes,
                }
                # Internal hint helps the coordinator schedule throttling-aware cooldowns.
                if report_hint:
                    payload["_report_hint"] = report_hint
            else:
                proto_loc = device_update_pb2.Location()
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
                    "encrypted_identity_key": raw_encrypted_identity_key,
                    "owner_key_version": raw_owner_key_version,
                    "identity_key": identity_key_bytes,
                    "identity_key_candidates": identity_key_candidate_bytes,
                }
                if report_hint:
                    payload["_report_hint"] = report_hint

            # [FIX: CRITICAL DATA MERGE]
            # Forward the extracted metadata to the coordinator so it can be persisted.
            if "pair_date" in metadata_update:
                payload["pair_date"] = metadata_update["pair_date"]

            if "secrets_creation_date" in metadata_update:
                payload["secrets_creation_date"] = metadata_update[
                    "secrets_creation_date"
                ]

            if metadata:
                payload.update(metadata)

            # Log with timezone-awareness if HA util is available (debug only)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    dt_util = import_module("homeassistant.util.dt")

                    ts_local = dt_util.as_local(
                        datetime.datetime.fromtimestamp(loc.time, tz=datetime.UTC)
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
    device_update_protobuf: DeviceUpdateProto,
    *,
    cache: TokenCache,
) -> list[dict[str, Any]]:
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
            async_decrypt_location_response_locations(
                device_update_protobuf, cache=cache
            )
        )
    else:
        # A loop is running in this thread → don't deadlock
        raise RuntimeError(
            "Sync decrypt_location_response_locations() used inside a running event loop. "
            "Use `await async_decrypt_location_response_locations(...)` instead."
        )


if __name__ == "__main__":  # Developer self-check only; not used by Home Assistant
    res = parse_device_update_protobuf("")
    try:
        decrypt_location_response_locations(res, cache=cast("TokenCache", None))
    except Exception as exc:
        print(f"Self-check encountered exception (expected outside HA runtime): {exc}")

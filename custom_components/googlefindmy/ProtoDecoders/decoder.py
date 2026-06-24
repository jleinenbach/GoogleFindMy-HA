# custom_components/googlefindmy/ProtoDecoders/decoder.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import binascii
import datetime
import logging
import math
import os
import subprocess
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from google.protobuf.message import DecodeError, Message

try:
    from zoneinfo import ZoneInfo  # stdlib, Python 3.9+
except ImportError:
    ZoneInfo = None  # type: ignore

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache

from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaProtobufDecodeError,
)
from custom_components.googlefindmy.ProtoDecoders import (
    Common_pb2,
    DeviceUpdate_pb2,
    LocationReportsUpload_pb2,
)

_LOGGER = logging.getLogger(__name__)

# Process-wide guard so each canonicless device (returned by Google without a
# canonical ID) is announced only once, despite get_devices_with_location running
# multiple times per poll. Keyed by device name (the only stable handle a
# canonicless device has). The naming mirrors main.py's _warned_bad_identifier_devices,
# but the scope differs: that is a per-coordinator instance attribute, while this is a
# module-level set shared across config entries and only cleared by the test reset hook
# (a re-discovered device warns again only after a process restart).
_warned_canonicless_devices: set[str] = set()


def _reset_canonicless_warning_state() -> None:
    """Clear the canonicless-device warning guard (test hook for isolation)."""
    _warned_canonicless_devices.clear()


_text_format_module: Any | None = None


def _get_text_format() -> Any:
    """Lazily import ``google.protobuf.text_format`` outside the event loop."""

    global _text_format_module
    if _text_format_module is None:
        _text_format_module = import_module("google.protobuf.text_format")
    return _text_format_module


class _DecryptLocationsCallable(Protocol):
    """Runtime signature for the decrypt helper imported lazily."""

    def __call__(
        self,
        device_update_protobuf: DeviceUpdate_pb2.DeviceUpdate,
        *,
        cache: TokenCache,
    ) -> list[dict[str, Any]] | None: ...


# --------------------------------------------------------------------------------------
# Pretty printer helpers (dev tooling)
# --------------------------------------------------------------------------------------


def custom_message_formatter(
    message: Message,
    indent: str,
    _as_one_line: bool,
) -> str:
    """Format protobuf messages with bytes fields as hex strings (dev convenience).

    Note:
        This is a developer-facing utility and is intentionally tolerant to schema changes.
        Time fields (named 'Time') are rendered as ISO-8601 UTC (Z). Optionally, when the
        environment variable GOOGLEFINDMY_DEV_TZ is set to a valid IANA zone (e.g.,
        "Europe/Berlin"), a second line with that display timezone is printed.
        This output is for human readability only and must not be used for program logic.
    """
    lines = []
    indent = f"{indent}"
    indent = indent.removeprefix("0")

    # Resolve optional display timezone from env; default to UTC.
    display_tz_name = os.environ.get("GOOGLEFINDMY_DEV_TZ", "UTC")
    display_tz: datetime.tzinfo = datetime.UTC
    if display_tz_name == "UTC" or ZoneInfo is None:
        display_tz_name = "UTC"
    else:
        try:
            display_tz = ZoneInfo(display_tz_name)
        except Exception:
            display_tz = datetime.UTC
            display_tz_name = "UTC"

    for field, value in message.ListFields():
        if field.type == field.TYPE_BYTES:
            hex_value = binascii.hexlify(value).decode("utf-8")
            lines.append(f'{indent}{field.name}: "{hex_value}"')
        elif field.type == field.TYPE_MESSAGE:
            if field.label == field.LABEL_REPEATED:
                for sub_message in value:
                    if field.message_type.name == "Time":
                        # seconds (+ optional nanos) -> float seconds
                        secs = getattr(sub_message, "seconds", 0)
                        nanos = getattr(sub_message, "nanos", 0)
                        unix_time = float(secs) + float(nanos) / 1e9

                        dt_utc = datetime.datetime.fromtimestamp(
                            unix_time, tz=datetime.UTC
                        )
                        utc_str = dt_utc.isoformat().replace("+00:00", "Z")
                        if display_tz_name == "UTC":
                            lines.append(
                                f"{indent}{field.name} {{\n{indent}  utc: {utc_str}\n{indent}}}"
                            )
                        else:
                            dt_disp = dt_utc.astimezone(display_tz)
                            disp_str = dt_disp.isoformat()
                            lines.append(
                                f"{indent}{field.name} {{\n{indent}  utc: {utc_str}\n{indent}  {display_tz_name}: {disp_str}\n{indent}}}"
                            )
                    else:
                        nested_message = custom_message_formatter(
                            sub_message, f"{indent}  ", _as_one_line
                        )
                        lines.append(
                            f"{indent}{field.name} {{\n{nested_message}\n{indent}}}"
                        )
            elif field.message_type.name == "Time":
                # seconds (+ optional nanos) -> float seconds
                secs = getattr(value, "seconds", 0)
                nanos = getattr(value, "nanos", 0)
                unix_time = float(secs) + float(nanos) / 1e9

                dt_utc = datetime.datetime.fromtimestamp(unix_time, tz=datetime.UTC)
                utc_str = dt_utc.isoformat().replace("+00:00", "Z")
                if display_tz_name == "UTC":
                    lines.append(
                        f"{indent}{field.name} {{\n{indent}  utc: {utc_str}\n{indent}}}"
                    )
                else:
                    dt_disp = dt_utc.astimezone(display_tz)
                    disp_str = dt_disp.isoformat()
                    lines.append(
                        f"{indent}{field.name} {{\n{indent}  utc: {utc_str}\n{indent}  {display_tz_name}: {disp_str}\n{indent}}}"
                    )
            else:
                nested_message = custom_message_formatter(
                    value, f"{indent}  ", _as_one_line
                )
                lines.append(f"{indent}{field.name} {{\n{nested_message}\n{indent}}}")
        else:
            lines.append(f"{indent}{field.name}: {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Protobuf parse helpers (stable API)
# --------------------------------------------------------------------------------------


def parse_location_report_upload_protobuf(
    hex_string: str,
) -> LocationReportsUpload_pb2.LocationReportsUpload:
    """Parse LocationReportsUpload from a hex string.

    Raises:
        NovaProtobufDecodeError: If the response cannot be decoded as a valid Protobuf.
    """
    location_reports = LocationReportsUpload_pb2.LocationReportsUpload()
    try:
        location_reports.ParseFromString(bytes.fromhex(hex_string))
    except (DecodeError, ValueError, binascii.Error) as exc:
        _LOGGER.error(
            "Failed to decode Google Protobuf response (LocationReportsUpload): %s",
            exc,
        )
        raise NovaProtobufDecodeError(
            f"LocationReportsUpload decode failed: {exc}"
        ) from exc
    return location_reports


def parse_device_update_protobuf(
    hex_string: str,
) -> DeviceUpdate_pb2.DeviceUpdate:
    """Parse DeviceUpdate from a hex string.

    Raises:
        NovaProtobufDecodeError: If the response cannot be decoded as a valid Protobuf.
    """
    device_update = DeviceUpdate_pb2.DeviceUpdate()
    try:
        device_update.ParseFromString(bytes.fromhex(hex_string))
    except (DecodeError, ValueError, binascii.Error) as exc:
        _LOGGER.error(
            "Failed to decode Google Protobuf response (DeviceUpdate): %s",
            exc,
        )
        raise NovaProtobufDecodeError(
            f"DeviceUpdate decode failed: {exc}"
        ) from exc
    return device_update


def parse_device_list_protobuf(
    hex_string: str,
) -> DeviceUpdate_pb2.DevicesList:
    """Parse DevicesList from a hex string.

    Raises:
        NovaProtobufDecodeError: If the response cannot be decoded as a valid Protobuf.
    """
    device_list = DeviceUpdate_pb2.DevicesList()
    try:
        device_list.ParseFromString(bytes.fromhex(hex_string))
    except (DecodeError, ValueError, binascii.Error) as exc:
        _LOGGER.error(
            "Failed to decode Google Protobuf response (DevicesList): %s",
            exc,
        )
        raise NovaProtobufDecodeError(
            f"DevicesList decode failed: {exc}"
        ) from exc
    return device_list


# --------------------------------------------------------------------------------------
# Canonical ID extraction
# --------------------------------------------------------------------------------------


def get_canonic_ids(
    device_list: DeviceUpdate_pb2.DevicesList,
) -> list[tuple[str, str]]:
    """Return (device_name, canonic_id) for devices in the list.

    Only returns the PRIMARY (first) canonical ID for each device.
    Android devices can have multiple canonical IDs (historical IDs from
    updates/resets), but we only want the current primary identifier.

    Defensive policy:
        * Handle Android and non-Android identifier shapes.
        * Skip non-string/empty IDs to avoid downstream surprises.
        * Use only first valid ID per device to prevent duplicates.
    """
    result: list[tuple[str, str]] = []
    for device in getattr(device_list, "deviceMetadata", []):
        try:
            if device.identifierInformation.type == DeviceUpdate_pb2.IDENTIFIER_ANDROID:
                canonic_ids = (
                    device.identifierInformation.phoneInformation.canonicIds.canonicId
                )
            else:
                canonic_ids = device.identifierInformation.canonicIds.canonicId
        except Exception:
            # Fallback: no canonic IDs available for this device
            canonic_ids = []

        device_name = getattr(device, "userDefinedDeviceName", None) or ""

        for canonic_id in canonic_ids:
            cid = getattr(canonic_id, "id", None)
            if isinstance(cid, str) and cid:
                _LOGGER.debug(
                    "ID extraction: Using primary ID '%s' for device '%s'",
                    cid,
                    device_name,
                )
                result.append((device_name, cid))
                break  # Only use first (primary) canonical ID per device
    return result


# --------------------------------------------------------------------------------------
# Location extraction with contamination shielding
# --------------------------------------------------------------------------------------

# Tunables to keep behavior explicit and easily auditable
_NEAR_TS_TOLERANCE_S: float = 5.0  # semantic merge tolerance (seconds)

_ANCHOR_METADATA_KEYS: tuple[str, ...] = (
    "pair_date",
    "secrets_creation_date",
    "device_registration",
    "device_type_information",
    "encrypted_user_secrets",
    "identity_key",
    "identity_key_candidates",
    "encrypted_identity_key_candidates",
    "encrypted_identity_key",
    "owner_key_version",
    "time_anchors_debug",
    "metadata_only",
)

_DEVICE_STUB_KEYS: tuple[str, ...] = (
    "name",
    "id",
    "device_id",
    "encrypted_identity_key",
    "encrypted_account_key",
    "public_key_address",
    "owner_key_version",
    "device_type",
    "fast_pair_model_id",
    "manufacturer",
    "model",
    "latitude",
    "longitude",
    "altitude",
    "accuracy",
    "last_seen",
    "status",
    "status_code",
    "_report_hint",
    "is_own_report",
    "semantic_name",
    "battery_level",
    *_ANCHOR_METADATA_KEYS,
)


def _build_device_stub(device_name: str, canonic_id: str) -> dict[str, Any]:
    """Return a normalized, predictable stub for a device row.

    The stub ensures consistent keys across call sites and prevents
    accidental overwrites caused by missing keys.
    """
    return {
        "name": device_name,
        "id": canonic_id,
        "device_id": canonic_id,
        "encrypted_identity_key": None,
        "encrypted_account_key": None,
        "public_key_address": None,
        "owner_key_version": None,
        "device_type": None,
        "fast_pair_model_id": None,
        "manufacturer": None,
        "model": None,
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "accuracy": None,
        "last_seen": None,
        "status": None,
        "status_code": None,
        "_report_hint": None,
        "is_own_report": None,
        "semantic_name": None,
        "battery_level": None,
        "pair_date": None,
        "secrets_creation_date": None,
        "device_registration": None,
        "device_type_information": None,
        "encrypted_user_secrets": None,
        "identity_key": None,
        "identity_key_candidates": None,
        "encrypted_identity_key_candidates": None,
        "time_anchors_debug": None,
        "metadata_only": None,
    }


def _normalize_location_dict(loc: dict[str, Any]) -> dict[str, Any]:
    """Coerce numeric fields to floats (when present) and drop invalid values.

    Only mutates a shallow copy. Unknown keys are preserved (e.g., `_report_hint`).

    Validation rules:
        - NaN/Inf values are dropped for all numeric fields.
        - accuracy < 0.001m is dropped: The Android Location API uses 0.0 as an error
          code meaning "no accuracy available". Modern dual-frequency GNSS can achieve
          sub-meter accuracy, so only the error code (0.0) is filtered. The REPORT is
          kept, but accuracy is treated as unmeasured (acc_rank = -inf in ranking).
        - "Null Island" coordinates (0.0, 0.0) are dropped: This location in the
          Atlantic Ocean is a common API default when no real location is available.
    """
    # Minimum valid accuracy threshold (1mm).
    # Only the error code 0.0 (and negative values) are filtered.
    # Modern dual-frequency GNSS can achieve sub-meter accuracy.
    _MIN_VALID_ACCURACY = 0.001

    out = dict(loc)
    for num_key in ("latitude", "longitude", "accuracy", "last_seen", "altitude"):
        val = out.get(num_key)
        if val is None:
            continue
        try:
            f = float(val)
            if not math.isfinite(f):
                out.pop(num_key, None)
            # accuracy < 0.001m is the error code (0.0 = "no accuracy").
            # We KEEP the report but REMOVE the accuracy key so it doesn't
            # corrupt ranking (acc_rank = -inf when accuracy is None).
            # Valid sub-meter values like 0.5m are preserved!
            elif num_key == "accuracy" and f < _MIN_VALID_ACCURACY:
                out.pop(num_key, None)
            else:
                out[num_key] = f
        except (TypeError, ValueError):
            out.pop(num_key, None)

    # "Null Island" filter: Coordinates at (0.0, 0.0) are in the Atlantic Ocean
    # and indicate a default/missing value from the API, not a real location.
    lat = out.get("latitude")
    lon = out.get("longitude")
    if lat is not None and lon is not None:
        # Use small epsilon for floating point comparison (covers 0.0 defaults)
        if abs(lat) < 0.0001 and abs(lon) < 0.0001:
            out.pop("latitude", None)
            out.pop("longitude", None)
            out.pop("accuracy", None)  # Without coordinates, accuracy is meaningless

    return out


def _normalize_timestamp(value: Any) -> int | None:
    """Return epoch seconds from primitive or Timestamp-like values.

    For *anchor* timestamps, treat unset/default values (0 / non-positive) as missing.
    """

    if value is None:
        return None

    # Protobuf Timestamp-like object
    if hasattr(value, "seconds"):
        try:
            seconds = int(getattr(value, "seconds"))
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None

    return seconds if seconds > 0 else None


def _merge_dict_preserve_left(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    """Merge two dicts without overwriting keys from the left dict."""

    merged = dict(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = value
    return merged


def _collect_anchor_metadata(
    location_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Union anchor/metadata keys across *all* candidates.

    This protects metadata-only candidates from being dropped when a different
    candidate wins the location ranking.
    """

    union: dict[str, Any] = {}
    for key in _ANCHOR_METADATA_KEYS:
        union[key] = None

    # Union strategy:
    # - timestamps: keep the first non-null positive value
    # - identity_key: keep the first non-null bytes value
    # - *_candidates: union lists (dedup, stable order)
    # - dict blobs: keep first; for time_anchors_debug merge (preserve-left)
    # - metadata_only: OR
    for cand in location_candidates:
        if not isinstance(cand, dict):
            continue

        # boolean
        if "metadata_only" in cand:
            union["metadata_only"] = bool(union.get("metadata_only")) or bool(
                cand.get("metadata_only")
            )

        for ts_key in ("pair_date", "secrets_creation_date"):
            if ts_key in cand:
                ts_val = _normalize_timestamp(cand.get(ts_key))
                if ts_val is not None and union.get(ts_key) is None:
                    union[ts_key] = ts_val

        # identity key material (convert bytes to hex string for consistency)
        if union.get("identity_key") is None:
            ik_val = cand.get("identity_key")
            if isinstance(ik_val, (bytes, bytearray)) and ik_val:
                union["identity_key"] = bytes(ik_val).hex()
            elif isinstance(ik_val, str) and ik_val:
                union["identity_key"] = ik_val

        # encrypted_identity_key (convert bytes to hex string for consistency)
        if union.get("encrypted_identity_key") is None:
            eik_val = cand.get("encrypted_identity_key")
            if isinstance(eik_val, (bytes, bytearray)) and eik_val:
                union["encrypted_identity_key"] = bytes(eik_val).hex()
            elif isinstance(eik_val, str) and eik_val:
                union["encrypted_identity_key"] = eik_val

        # owner_key_version (int)
        if union.get("owner_key_version") is None:
            okv = cand.get("owner_key_version")
            if isinstance(okv, int) and okv > 0:
                union["owner_key_version"] = okv

        # list unions
        for list_key in (
            "identity_key_candidates",
            "encrypted_identity_key_candidates",
        ):
            lst = cand.get(list_key)
            if not isinstance(lst, list) or not lst:
                continue
            existing = union.get(list_key)
            if not isinstance(existing, list):
                existing = []
            for item in lst:
                if isinstance(item, (bytes, bytearray)) and item:
                    b = bytes(item)
                    if b not in existing:
                        existing.append(b)
                # allow non-bytes items (diagnostics) but still dedup
                elif item not in existing:
                    existing.append(item)
            union[list_key] = existing

        # dict blobs (diagnostics)
        for dict_key in (
            "device_registration",
            "device_type_information",
            "encrypted_user_secrets",
        ):
            if union.get(dict_key) is None:
                d_val = cand.get(dict_key)
                if isinstance(d_val, dict) and d_val:
                    union[dict_key] = d_val

        # debug blob: merge without overwriting left keys
        dbg = cand.get("time_anchors_debug")
        if isinstance(dbg, dict) and dbg:
            existing_dbg = union.get("time_anchors_debug")
            if isinstance(existing_dbg, dict) and existing_dbg:
                union["time_anchors_debug"] = _merge_dict_preserve_left(
                    existing_dbg, dbg
                )
            else:
                union["time_anchors_debug"] = dbg

    return union


def _get_rank_tuple(n: dict[str, Any]) -> tuple[float, int, int, float, str]:
    """Create a sort key tuple prioritizing the freshest timestamp.

    Priority (high to low):
      1. Newer ``last_seen`` timestamp
      2. Source/Status: Owner > Crowdsourced > Aggregated > Unknown
         (BUT: is_own_report only trusted if accuracy is valid)
      3. Presence of coordinates (tie-breaker when timestamps/status match)
      4. Better accuracy (smaller is better)
      5. Deterministic tie-breaker string
    """
    # Pre-compute accuracy validity for status ranking decisions.
    # A report claiming is_own_report=True but with invalid/missing accuracy
    # (after normalization strips accuracy <= 0) is suspicious and should NOT
    # get the "own report" bonus. This prevents the January 2025 phantom bug.
    acc = n.get("accuracy")
    has_valid_accuracy = (
        isinstance(acc, (int, float)) and math.isfinite(float(acc)) and float(acc) > 0
    )

    # 1) Owner-Reports take precedence ONLY if accuracy is trustworthy
    is_own_flag = bool(n.get("is_own_report"))
    # Own report bonus requires valid accuracy (or no coordinates = semantic only)
    has_coords = isinstance(n.get("latitude"), (int, float)) and isinstance(
        n.get("longitude"), (int, float)
    )
    # Trust is_own_report if: (1) accuracy is valid, OR (2) no coordinates (semantic)
    is_own_trusted = is_own_flag and (has_valid_accuracy or not has_coords)

    # 2) Robustly determine status rank (String, Int, or via Hint)
    status_code = n.get("status_code")
    try:
        status_code_int = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code_int = None
    raw_status = n.get("status")
    if isinstance(raw_status, str):
        status_name = raw_status.strip().lower()
    elif isinstance(raw_status, (int, float)):
        try:
            status_name = Common_pb2.Status.Name(int(raw_status)).lower()
        except Exception:
            status_name = str(int(raw_status))
    else:
        status_name = ""

    hint = str(n.get("_report_hint") or "").strip().lower()

    # 3) Derive rank (multiple paths for robustness)
    # Use a sentinel object for robust enum comparisons
    _MISSING = object()
    cs = getattr(Common_pb2.Status, "CROWDSOURCED", _MISSING)
    ag = getattr(Common_pb2.Status, "AGGREGATED", _MISSING)
    if is_own_trusted:
        status_rank = 3
    elif (
        (cs is not _MISSING and status_code_int == cs)
        or "crowdsourced" in status_name
        or "in_all_areas" in status_name
        or hint == "in_all_areas"
    ):
        status_rank = 2
    elif (
        (ag is not _MISSING and status_code_int == ag)
        or "aggregated" in status_name
        or "high_traffic" in status_name
        or hint == "high_traffic"
    ):
        status_rank = 1
    else:
        status_rank = 0  # SEMANTIC/Unknown/Default

    # has_coords already computed above as boolean; convert to int for tuple
    has_coords_rank = 1 if has_coords else 0

    seen = n.get("last_seen")
    seen_rank = (
        float(seen)
        if isinstance(seen, (int, float)) and math.isfinite(float(seen))
        else float("-inf")
    )

    # accuracy <= 0 is physically impossible for GPS; treat as missing/worst rank.
    # Defense in depth: even if _normalize_location_dict didn't filter it.
    # (has_valid_accuracy already computed above; assert helps mypy)
    if has_valid_accuracy:
        assert acc is not None  # validated in has_valid_accuracy
        acc_rank = -float(acc)
    else:
        acc_rank = float("-inf")

    # Deterministic final tiebreaker: canonical content key (string).
    stable_key = "|".join(
        str(x)
        for x in (
            n.get("status_code", ""),
            n.get("status", ""),
            int(bool(n.get("is_own_report"))),
            n.get("last_seen", ""),
            n.get("latitude", ""),
            n.get("longitude", ""),
            n.get("accuracy", ""),
            n.get("semantic_name", ""),
        )
    )
    return (seen_rank, status_rank, has_coords_rank, acc_rank, stable_key)


def _select_best_location(
    cands: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Choose the most useful location from a list of candidates.

    This function normalizes all candidates once, then sorts them based on a
    clear priority hierarchy to find the single most relevant location report.

    Priority (high to low):
        1) Newer `last_seen` timestamp
        2) Status/Source (Owner > Crowdsourced > Aggregated)
        3) Presence of coordinates (tie-breaker when timestamps/status match)
        4) Better accuracy (smaller is better)
        5) Deterministic tie-breaker (canonical stable key)

    Returns:
        A tuple containing:
        - The single best location as a normalized dictionary, or None.
        - The complete list of all normalized candidates, which can be reused
          by downstream functions like semantic merging without re-processing.
    """
    if not cands:
        return None, []

    # Normalize once up front
    normed_cands: list[dict[str, Any]] = [
        _normalize_location_dict(c or {}) for c in cands
    ]

    # Sort using the new rank tuple which prioritizes recency over status
    normed_cands.sort(key=_get_rank_tuple, reverse=True)

    best_candidate = normed_cands[0]

    return dict(best_candidate), normed_cands


def _merge_semantics_if_near_ts(
    best: dict[str, Any],
    normed_cands: list[dict[str, Any]],
    *,
    tolerance_s: float = _NEAR_TS_TOLERANCE_S,
) -> dict[str, Any]:
    """Attach semantic labels and freshest timestamps to the best fix.

    This keeps the most useful coordinate payload while still promoting
    fresher semantic-only reports so downstream consumers perceive the update
    as new. When a semantic report outranks a coordinate fix, the latest
    available coordinates are borrowed back after the merge so spatial data
    remains populated.
    """

    def _extract_ts(raw_ts: Any) -> float:
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            return float("-inf")
        if not math.isfinite(ts) or ts <= 0:
            return float("-inf")
        return ts

    out = dict(best)

    # Extract best candidate's timestamp FIRST - used for identity protection.
    t_best = _extract_ts(out.get("last_seen"))

    # Track the freshest coordinate-bearing candidate so semantic-only entries can
    # still expose stable position data after the merge step.
    #
    # IDENTITY PROTECTION: Initialize to t_best (not -inf) so that lower-ranked
    # candidates with the SAME timestamp cannot "steal" coordinates from the best.
    # A lower-ranked entry must be STRICTLY newer to update coordinates.
    best_coordinate: dict[str, Any] | None = None
    best_coordinate_ts = t_best  # Protects against timestamp collision identity theft

    # Track the semantic label currently attached to the outgoing payload.
    semantic_label: str | None = None
    semantic_ts = float("-inf")
    if out.get("semantic_name"):
        semantic_label = str(out["semantic_name"])
        semantic_ts = _extract_ts(out.get("last_seen"))

    # Historical behaviour: borrow a semantic label very close to the coordinate
    # fix timestamp when none is present yet.
    if semantic_label is None and t_best > float("-inf"):
        best_label: str | None = None
        best_label_ts = float("-inf")
        min_delta = float("inf")
        for n in normed_cands:
            label = n.get("semantic_name")
            if not label:
                continue
            ts = _extract_ts(n.get("last_seen"))
            if ts == float("-inf"):
                continue
            delta = abs(ts - t_best)
            if delta <= tolerance_s and delta < min_delta:
                best_label = str(label)
                best_label_ts = ts
                min_delta = delta
        if best_label is not None:
            semantic_label = best_label
            semantic_ts = best_label_ts

    latest_seen = t_best
    latest_semantic_label = semantic_label
    latest_semantic_ts = semantic_ts

    for n in normed_cands:
        ts = _extract_ts(n.get("last_seen"))
        latest_seen = max(latest_seen, ts)

        # Use strict > to preserve sort order: when timestamps are equal,
        # the first entry (already ranked higher by _get_rank_tuple) wins.
        # Using >= would cause "last-write-wins" and prefer worse entries.
        if (
            isinstance(n.get("latitude"), (int, float))
            and isinstance(n.get("longitude"), (int, float))
            and ts > best_coordinate_ts
        ):
            best_coordinate = n
            best_coordinate_ts = ts

        label = n.get("semantic_name")
        if label:
            if ts > latest_semantic_ts:
                latest_semantic_label = str(label)
                latest_semantic_ts = ts
            elif latest_semantic_label is None and ts == latest_semantic_ts:
                latest_semantic_label = str(label)

    if latest_seen > float("-inf"):
        out["last_seen"] = latest_seen

    if latest_semantic_label:
        out["semantic_name"] = latest_semantic_label

    if best_coordinate is not None:
        for coord_field in ("latitude", "longitude", "accuracy", "altitude"):
            value = best_coordinate.get(coord_field)
            if value is not None:
                out[coord_field] = value

    return out


def get_devices_with_location(
    device_list: DeviceUpdate_pb2.DevicesList,
    *,
    cache: TokenCache | None = None,
) -> list[dict[str, Any]]:
    """Extract one consolidated row per canonic device ID from a device list.

    This function serves as a robust barrier against data contamination by
    ensuring its output is always clean, consistent, and predictable. When a
    real TokenCache instance is provided, encrypted location payloads are
    decrypted; otherwise the function returns sanitized stubs without
    attempting the decrypt workflow.

    Guarantees:
        * **One Row Per ID**: Returns exactly one dictionary per unique canonic ID,
          preventing duplicate entries from overwriting valid data downstream.
        * **Deterministic Selection**: If multiple location reports are embedded
          for a single device, it deterministically selects the single best one.
        * **Consistent Shape**: Returned dictionaries always contain the same set of
          keys (defined in `_DEVICE_STUB_KEYS`), preventing `KeyError` exceptions
          in consumer code.
        * **Data Hygiene**: All numeric fields are coerced to `float`, validated
          for finiteness (no `NaN`/`Inf`), and sanitized before being returned.

    Returns:
        A list of device data dictionaries. Fields may be `None` if no valid
        data was found, but the key structure is always consistent.
    """
    # Lazy import keeps module import-time light and avoids heavy dependencies if unused.
    try:
        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # noqa: E501
            decrypt_location_response_locations as _decrypt_locations,
        )
    except Exception:
        # If the decrypt layer is unavailable, return stubs only.
        decrypt_location_response_locations: _DecryptLocationsCallable | None = None
    else:
        decrypt_location_response_locations = _decrypt_locations

    results: list[dict[str, Any]] = []

    for device in getattr(device_list, "deviceMetadata", []):
        # Resolve canonic IDs for this device (Android vs. generic path)
        # FIX: For phones (IDENTIFIER_ANDROID), use only the FIRST canonical ID.
        # Phones can have multiple canonical IDs in the array (e.g., after re-pairing),
        # but only the first/primary ID should be used to avoid creating duplicate
        # device entries. This matches fcm_receiver_ha._extract_canonic_id_from_response().
        is_android_device = False
        try:
            is_android_device = (
                device.identifierInformation.type == DeviceUpdate_pb2.IDENTIFIER_ANDROID
            )
            if is_android_device:
                all_ids = (
                    device.identifierInformation.phoneInformation.canonicIds.canonicId
                )
                # Use only the first canonical ID for phones to prevent duplicates
                canonic_ids = all_ids[:1] if all_ids else []
            else:
                canonic_ids = device.identifierInformation.canonicIds.canonicId
        except Exception:
            canonic_ids = []

        device_name = getattr(device, "userDefinedDeviceName", None) or ""

        if _LOGGER.isEnabledFor(logging.DEBUG):
            debug_fields = [f.name for f, _ in device.ListFields()]
            _LOGGER.debug("Device '%s' raw fields: %s", device_name, debug_fields)

            if device.HasField("information"):
                info_fields = [f.name for f, _ in device.information.ListFields()]
                _LOGGER.debug("  -> information fields: %s", info_fields)

                if device.information.HasField("locationInformation"):
                    loc_fields = [
                        f.name
                        for f, _ in device.information.locationInformation.ListFields()
                    ]
                    _LOGGER.debug("    -> locationInformation fields: %s", loc_fields)

            device_str = str(device)
            unknown_lines = [
                line
                for line in device_str.splitlines()
                if line.strip() and line.strip()[0].isdigit()
            ]
            if unknown_lines:
                _LOGGER.debug(
                    "Device '%s' has UNKNOWN FIELDS: \n%s",
                    device_name,
                    "\n".join(unknown_lines),
                )

        # Try decryption ONCE per device; share across all its canonic IDs
        location_candidates: list[dict[str, Any]] = []
        encrypted_identity_key = None
        owner_key_version = None
        device_type = None
        fast_pair_model_id: str | None = None
        manufacturer: str | None = None
        model: str | None = None
        pair_date: int | None = None
        secrets_creation_date: int | None = None
        encrypted_account_key: str | None = None
        public_key_address: str | None = None
        if decrypt_location_response_locations is not None and cache is not None:
            try:
                if device.HasField("information") and device.information.HasField(
                    "locationInformation"
                ):
                    locinfo = device.information.locationInformation
                    has_reports = False
                    if locinfo.HasField("reports"):
                        r = locinfo.reports
                        # Either network reports exist, or a 'recentLocation' is set
                        if r.HasField("recentLocationAndNetworkLocations"):
                            rn = r.recentLocationAndNetworkLocations
                            has_reports = (
                                rn.HasField("recentLocation")
                                or len(getattr(rn, "networkLocations", [])) > 0
                            )

                    if has_reports:
                        mock_device_update = DeviceUpdate_pb2.DeviceUpdate()
                        mock_device_update.deviceMetadata.CopyFrom(device)
                        location_candidates = (
                            decrypt_location_response_locations(
                                mock_device_update,
                                cache=cache,
                            )
                            or []
                        )
            except Exception as err:
                # Defensive: decryption issues must not break the whole list, but log
                # the root cause so users can debug key or parsing mismatches.
                _LOGGER.warning(
                    "Failed to decrypt location for device '%s': %s",
                    device_name or "<unknown>",
                    err,
                    exc_info=err,
                )
                location_candidates = []

        if device.HasField("information") and device.information.HasField(
            "deviceRegistration"
        ):
            registration = device.information.deviceRegistration
            encrypted_user_secrets = registration.encryptedUserSecrets

            if encrypted_user_secrets.encryptedIdentityKey:
                encrypted_identity_key = (
                    encrypted_user_secrets.encryptedIdentityKey.hex()
                )

            # NOTE: In Proto3, non-optional scalar fields (int32) do not support
            # HasField(). Accessing them directly returns 0 if unset.
            owner_key_version = encrypted_user_secrets.ownerKeyVersion

            if registration.HasField("deviceTypeInformation"):
                device_type = registration.deviceTypeInformation.deviceType

            raw_fast_pair_model_id = getattr(registration, "fastPairModelId", None)
            if isinstance(raw_fast_pair_model_id, str):
                fast_pair_model_id = raw_fast_pair_model_id or None
            elif isinstance(raw_fast_pair_model_id, (bytes, bytearray)):
                try:
                    fast_pair_model_id = raw_fast_pair_model_id.decode() or None
                except UnicodeDecodeError:
                    fast_pair_model_id = raw_fast_pair_model_id.hex()

            raw_manufacturer = getattr(registration, "manufacturer", None)
            if isinstance(raw_manufacturer, str):
                raw_manufacturer = raw_manufacturer.strip()
                manufacturer = raw_manufacturer or None

            raw_model = getattr(registration, "model", None)
            if isinstance(raw_model, str):
                raw_model = raw_model.strip()
                model = raw_model or None

            # Anchor timestamps used for relative EID timebases
            # - pairDate is a scalar proto3 field: default 0 means "unset"
            raw_pair_date: Any = getattr(registration, "pairDate", None)
            try:
                if isinstance(raw_pair_date, (int, float)) and raw_pair_date > 0:
                    pair_date = int(raw_pair_date)
            except (TypeError, ValueError):
                pair_date = None

            # - creationDate is a Timestamp message: avoid treating an unset/default Timestamp as epoch (0)
            raw_creation_date_seconds: Any = None
            try:
                if encrypted_user_secrets.HasField("creationDate"):
                    creation_date_obj: Any | None = getattr(
                        encrypted_user_secrets, "creationDate", None
                    )
                    raw_creation_date_seconds = getattr(
                        creation_date_obj, "seconds", None
                    )
            except (ValueError, AttributeError):
                creation_date_obj = getattr(
                    encrypted_user_secrets, "creationDate", None
                )
                raw_creation_date_seconds = (
                    getattr(creation_date_obj, "seconds", None)
                    if creation_date_obj is not None
                    else None
                )

            try:
                if (
                    isinstance(raw_creation_date_seconds, (int, float))
                    and raw_creation_date_seconds > 0
                ):
                    secrets_creation_date = int(raw_creation_date_seconds)
            except (TypeError, ValueError):
                secrets_creation_date = None

            raw_encrypted_account_key = getattr(
                encrypted_user_secrets, "encryptedAccountKey", None
            )
            if isinstance(raw_encrypted_account_key, (bytes, bytearray)) and (
                raw_encrypted_account_key
            ):
                encrypted_account_key = bytes(raw_encrypted_account_key).hex()

            raw_public_key_address = getattr(
                encrypted_user_secrets, "encryptedSha256AccountKeyPublicAddress", None
            )
            if isinstance(raw_public_key_address, (bytes, bytearray)) and (
                raw_public_key_address
            ):
                public_key_address = bytes(raw_public_key_address).hex()

        # --- DIAGNOSTIC: FIND HIDDEN KEYS ---
        # Phones (IDENTIFIER_ANDROID) and similar devices may not have keys in the
        # device listing payload, but keys ARE available via the Locate flow (FCM).
        # Only warn for tracker-like devices that unexpectedly lack keys.
        if not encrypted_identity_key:
            ids_str = ", ".join(
                [str(getattr(canonic_id, "id", "")) for canonic_id in canonic_ids]
            )

            # Check if device has reduced fields (no 'information' block)
            # Note: is_android_device is already set at the top of the device loop
            has_information_block = device.HasField("information")

            # Phone devices legitimately don't have keys in list_devices - this is expected.
            # The keys are provided via the Locate flow (FCM response) instead.
            if is_android_device or not has_information_block:
                _LOGGER.debug(
                    "Device '%s' has no key in listing (expected for phones/Android devices); "
                    "keys will be obtained via Locate flow. (IDs: %s)",
                    device_name,
                    ids_str,
                )
            else:
                # For tracker devices that should have keys but don't, log at WARNING
                _LOGGER.warning(
                    "DEBUG STRUCTURE: Missing Key for '%s' (IDs: %s)",
                    device_name,
                    ids_str,
                )

                # Level 1
                fields = [f.name for f, _ in device.ListFields()]
                _LOGGER.warning(" -> Device fields: %s", fields)

                if device.HasField("information"):
                    info = device.information
                    _LOGGER.warning(
                        " -> Info fields: %s", [f.name for f, _ in info.ListFields()]
                    )

                    if info.HasField("deviceRegistration"):
                        reg = info.deviceRegistration
                        _LOGGER.warning(
                            " -> Registration fields: %s",
                            [f.name for f, _ in reg.ListFields()],
                        )

                        if reg.HasField("encryptedUserSecrets"):
                            sec = reg.encryptedUserSecrets
                            _LOGGER.warning(
                                " -> Secrets fields: %s",
                                [f.name for f, _ in sec.ListFields()],
                            )
        # ------------------------------------

        # If decryption yielded results, select the best one and keep normalized list.
        if location_candidates:
            best, normed = _select_best_location(location_candidates)
            if best:
                best = _merge_semantics_if_near_ts(best, normed)
        else:
            best, normed = None, []

        # Collect anchor/metadata keys across all candidates to avoid dropping metadata-only payloads.
        anchor_union = _collect_anchor_metadata(location_candidates)
        if pair_date is not None:
            anchor_union["pair_date"] = pair_date
        if secrets_creation_date is not None:
            anchor_union["secrets_creation_date"] = secrets_creation_date

        # Record provenance hints for later refactoring-robust testing/debugging.
        if pair_date is not None or secrets_creation_date is not None:
            dbg = anchor_union.get("time_anchors_debug")
            if not isinstance(dbg, dict):
                dbg = {}
            if pair_date is not None:
                dbg.setdefault("pair_date_source", "device_registration.proto")
            if secrets_creation_date is not None:
                dbg.setdefault(
                    "secrets_creation_date_source", "encrypted_user_secrets.proto"
                )
            anchor_union["time_anchors_debug"] = dbg

        # Emit **exactly one** row per canonic ID.
        emitted_for_device = 0
        for canonic in canonic_ids:
            cid = getattr(canonic, "id", None)
            if not (isinstance(cid, str) and cid):
                continue

            row = _build_device_stub(device_name, cid)
            row["encrypted_identity_key"] = encrypted_identity_key
            row["owner_key_version"] = owner_key_version
            row["device_type"] = device_type
            row["fast_pair_model_id"] = fast_pair_model_id
            row["manufacturer"] = manufacturer
            row["model"] = model
            row["pair_date"] = pair_date
            row["secrets_creation_date"] = secrets_creation_date
            row["encrypted_account_key"] = encrypted_account_key
            row["public_key_address"] = public_key_address
            # Apply anchor/metadata union after identity fields are set (and after location merge below)

            if best:
                # best already normalized by selection; merge only known keys
                for k in _DEVICE_STUB_KEYS:
                    if k in best and best[k] is not None:
                        row[k] = best[k]
                # Ensure device identity fields are not overwritten by nested payloads
                row["name"] = device_name
                row["id"] = cid
                row["device_id"] = cid

            # Ensure anchor/metadata keys survive location ranking and do not get overwritten by defaults.
            for k in _ANCHOR_METADATA_KEYS:
                v = anchor_union.get(k)
                if v is not None:
                    row[k] = v

            results.append(row)
            emitted_for_device += 1

        if emitted_for_device == 0:
            # Google returned this device without a usable canonical ID, so no row
            # was emitted and it silently vanishes from Home Assistant. Surface it
            # once per process so the user can act, de-duplicated by device name
            # (the only stable handle a canonicless device has).
            dedup_key = device_name or "<unnamed>"
            if dedup_key not in _warned_canonicless_devices:
                _warned_canonicless_devices.add(dedup_key)
                _LOGGER.warning(
                    "Device '%s' was returned without a canonical ID and is not "
                    "shown in Home Assistant. Make it findable again in the Find My "
                    "Device app (or your Google account), then reload the integration.",
                    dedup_key,
                )

    return results


# --------------------------------------------------------------------------------------
# Dev print helpers
# --------------------------------------------------------------------------------------


def print_location_report_upload_protobuf(hex_string: str) -> None:
    msg = parse_location_report_upload_protobuf(hex_string)
    try:
        s = _get_text_format().MessageToString(
            msg, message_formatter=custom_message_formatter
        )
    except TypeError:
        s = _get_text_format().MessageToString(msg)
    print(s)


def print_device_update_protobuf(hex_string: str) -> None:
    msg = parse_device_update_protobuf(hex_string)
    try:
        s = _get_text_format().MessageToString(
            msg, message_formatter=custom_message_formatter
        )
    except TypeError:
        s = _get_text_format().MessageToString(msg)
    print(s)


def print_device_list_protobuf(hex_string: str) -> None:
    msg = parse_device_list_protobuf(hex_string)
    try:
        s = _get_text_format().MessageToString(
            msg, message_formatter=custom_message_formatter
        )
    except TypeError:
        s = _get_text_format().MessageToString(msg)
    print(s)


# --------------------------------------------------------------------------------------
# Developer entry point (protobuf regen + sample dumps)
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    # dev-only import to avoid hard dependency at runtime
    from custom_components.googlefindmy.example_data_provider import (
        get_example_data,
    )

    # Recompile (developer convenience)
    try:
        subprocess.run(
            ["protoc", "--python_out=.", "ProtoDecoders/Common.proto"],
            cwd="../",
            check=True,
        )
        subprocess.run(
            ["protoc", "--python_out=.", "ProtoDecoders/DeviceUpdate.proto"],
            cwd="../",
            check=True,
        )
        subprocess.run(
            ["protoc", "--python_out=.", "ProtoDecoders/LocationReportsUpload.proto"],
            cwd="../",
            check=True,
        )
        subprocess.run(
            ["protoc", "--pyi_out=.", "ProtoDecoders/Common.proto"],
            cwd="../",
            check=True,
        )
        subprocess.run(
            ["protoc", "--pyi_out=.", "ProtoDecoders/DeviceUpdate.proto"],
            cwd="../",
            check=True,
        )
        subprocess.run(
            ["protoc", "--pyi_out=.", "ProtoDecoders/LocationReportsUpload.proto"],
            cwd="../",
            check=True,
        )
    except FileNotFoundError:
        print("protoc not found. Skipping proto regeneration.")
    except subprocess.CalledProcessError as e:
        print(f"protoc failed: {e}")

    print("\n ------------------- \n")

    print("Device List: ")
    print_device_list_protobuf(get_example_data("sample_nbe_list_devices_response"))

    print("Own Report: ")
    print_location_report_upload_protobuf(get_example_data("sample_own_report"))

    print("\n ------------------- \n")

    print("Not Own Report: ")
    print_location_report_upload_protobuf(get_example_data("sample_foreign_report"))

    print("\n ------------------- \n")

    print("Device Update: ")
    print_device_update_protobuf(get_example_data("sample_device_update"))

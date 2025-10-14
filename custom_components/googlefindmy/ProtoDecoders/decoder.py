# custom_components/googlefindmy/ProtoDecoders/decoder.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import binascii
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from google.protobuf import text_format
import datetime
import math
import pytz

from custom_components.googlefindmy.ProtoDecoders import (
    DeviceUpdate_pb2,
    LocationReportsUpload_pb2,
)
from custom_components.googlefindmy.example_data_provider import get_example_data


# --------------------------------------------------------------------------------------
# Pretty printer helpers (dev tooling)
# --------------------------------------------------------------------------------------

def custom_message_formatter(message, indent, as_one_line):
    """Format protobuf messages with bytes fields as hex strings (dev convenience).

    Note:
        This is a developer-facing utility and is intentionally tolerant to schema changes.
        Time fields (named 'Time') are rendered in local time for readability.
    """
    lines = []
    indent = f"{indent}"
    indent = indent.removeprefix("0")

    for field, value in message.ListFields():
        if field.type == field.TYPE_BYTES:
            hex_value = binascii.hexlify(value).decode("utf-8")
            lines.append(f'{indent}{field.name}: "{hex_value}"')
        elif field.type == field.TYPE_MESSAGE:
            if field.label == field.LABEL_REPEATED:
                for sub_message in value:
                    if field.message_type.name == "Time":
                        unix_time = sub_message.seconds
                        local_time = datetime.datetime.fromtimestamp(
                            unix_time, pytz.timezone("Europe/Berlin")
                        )
                        lines.append(
                            f"{indent}{field.name} {{\n{indent}  {local_time}\n{indent}}}"
                        )
                    else:
                        nested_message = custom_message_formatter(
                            sub_message, f"{indent}  ", as_one_line
                        )
                        lines.append(
                            f"{indent}{field.name} {{\n{nested_message}\n{indent}}}"
                        )
            else:
                if field.message_type.name == "Time":
                    unix_time = value.seconds
                    local_time = datetime.datetime.fromtimestamp(
                        unix_time, pytz.timezone("Europe/Berlin")
                    )
                    lines.append(
                        f"{indent}{field.name} {{\n{indent}  {local_time}\n{indent}}}"
                    )
                else:
                    nested_message = custom_message_formatter(
                        value, f"{indent}  ", as_one_line
                    )
                    lines.append(
                        f"{indent}{field.name} {{\n{nested_message}\n{indent}}}"
                    )
        else:
            lines.append(f"{indent}{field.name}: {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Protobuf parse helpers (stable API)
# --------------------------------------------------------------------------------------

def parse_location_report_upload_protobuf(hex_string: str):
    """Parse LocationReportsUpload from a hex string."""
    location_reports = LocationReportsUpload_pb2.LocationReportsUpload()
    location_reports.ParseFromString(bytes.fromhex(hex_string))
    return location_reports


def parse_device_update_protobuf(hex_string: str):
    """Parse DeviceUpdate from a hex string."""
    device_update = DeviceUpdate_pb2.DeviceUpdate()
    device_update.ParseFromString(bytes.fromhex(hex_string))
    return device_update


def parse_device_list_protobuf(hex_string: str):
    """Parse DevicesList from a hex string."""
    device_list = DeviceUpdate_pb2.DevicesList()
    device_list.ParseFromString(bytes.fromhex(hex_string))
    return device_list


# --------------------------------------------------------------------------------------
# Canonical ID extraction
# --------------------------------------------------------------------------------------

def get_canonic_ids(device_list) -> List[Tuple[str, str]]:
    """Return (device_name, canonic_id) for all devices in the list.

    Defensive policy:
        * Handle Android and non-Android identifier shapes.
        * Skip non-string/empty IDs to avoid downstream surprises.
    """
    result: List[Tuple[str, str]] = []
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
                result.append((device_name, cid))
    return result


# --------------------------------------------------------------------------------------
# Location extraction with contamination shielding
# --------------------------------------------------------------------------------------

# Tunables to keep behavior explicit and easily auditable
_NEAR_TS_TOLERANCE_S: float = 5.0  # semantic merge tolerance (seconds)

_DEVICE_STUB_KEYS: Tuple[str, ...] = (
    "name",
    "id",
    "device_id",
    "latitude",
    "longitude",
    "altitude",
    "accuracy",
    "last_seen",
    "status",
    "is_own_report",
    "semantic_name",
    "battery_level",
)

def _build_device_stub(device_name: str, canonic_id: str) -> Dict[str, Any]:
    """Return a normalized, predictable stub for a device row.

    The stub ensures consistent keys across call sites and prevents
    accidental overwrites caused by missing keys.
    """
    return {
        "name": device_name,
        "id": canonic_id,
        "device_id": canonic_id,
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "accuracy": None,
        "last_seen": None,
        "status": None,
        "is_own_report": None,
        "semantic_name": None,
        "battery_level": None,
    }


def _normalize_location_dict(loc: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce numeric fields to floats (when present) and drop NaN/Inf.

    Only mutates a shallow copy. Unknown keys are preserved (e.g., `_report_hint`).
    """
    out = dict(loc)
    for num_key in ("latitude", "longitude", "accuracy", "last_seen", "altitude"):
        val = out.get(num_key)
        if val is None:
            continue
        try:
            f = float(val)
            if not math.isfinite(f):
                out.pop(num_key, None)
            else:
                out[num_key] = f
        except (TypeError, ValueError):
            out.pop(num_key, None)
    return out


def _select_best_location(
    cands: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Choose the most useful location from a list of candidates.

    This function normalizes all candidates once, then sorts them based on a
    clear priority hierarchy to find the single most relevant location report.

    Priority (high → low):
        1) Has numeric coordinates (lat/lon)
        2) Newer `last_seen` timestamp
        3) Better accuracy (smaller radius)
        4) `is_own_report` (used as a soft, final tiebreaker)

    Rationale:
        A fresh, precise crowdsourced fix is typically more helpful than an old,
        coarse own-report. This ordering reflects that practical preference.

    Returns:
        A tuple containing:
        - The single best location as a normalized dictionary, or None.
        - The complete list of all normalized candidates, which can be reused
          by downstream functions like semantic merging without re-processing.
    """
    if not cands:
        return None, []

    # Normalize once up front
    normed_cands: List[Dict[str, Any]] = [_normalize_location_dict(c or {}) for c in cands]

    for n in normed_cands:
        has_coords = isinstance(n.get("latitude"), (int, float)) and isinstance(
            n.get("longitude"), (int, float)
        )
        n["_rank_has_coords"] = 1 if has_coords else 0
        try:
            n["_rank_seen"] = float(n.get("last_seen") or 0.0)
        except (TypeError, ValueError):
            n["_rank_seen"] = 0.0
        try:
            acc = float(n.get("accuracy")) if n.get("accuracy") is not None else float("inf")
            n["_rank_acc"] = -acc  # negative => smaller accuracy ranks higher
        except (TypeError, ValueError):
            n["_rank_acc"] = float("-inf")
        n["_rank_is_own"] = 1 if bool(n.get("is_own_report")) else 0

    # Coords → recency → accuracy → own-report
    normed_cands.sort(
        key=lambda x: (
            x["_rank_has_coords"],
            x["_rank_seen"],
            x["_rank_acc"],
            x["_rank_is_own"],
        ),
        reverse=True,
    )

    # Micro-optimization: remove temp rank keys in-place on the winner, then copy.
    best_candidate = normed_cands[0]
    for k in ("_rank_has_coords", "_rank_seen", "_rank_acc", "_rank_is_own"):
        best_candidate.pop(k, None)

    return dict(best_candidate), normed_cands


def _merge_semantics_if_near_ts(
    best: Dict[str, Any],
    normed_cands: List[Dict[str, Any]],
    *,
    tolerance_s: float = _NEAR_TS_TOLERANCE_S,
) -> Dict[str, Any]:
    """Attach a semantic label from a candidate with the closest timestamp.

    Behavior:
        * If `best` already has a `semantic_name`, nothing changes.
        * Otherwise, it searches for a candidate with a `semantic_name` whose
          timestamp is within the `tolerance_s` window.
        * If multiple matches exist, it deterministically chooses the one with the
          smallest absolute time delta to the `best` location.

    Rationale:
        Sometimes the API provides a highly accurate GPS fix and a separate
        semantic report ("Home") at almost the same time. This function merges
        these two pieces of information, enriching the precise coordinate with
        a human-readable place name.

    Args:
        best: The already-selected best location (assumed normalized).
        normed_cands: The already-normalized list of all available candidates.
        tolerance_s: The maximum time delta in seconds to consider timestamps "near".

    Returns:
        A (shallow) copy of `best` with the `semantic_name` field potentially filled.
    """
    out = dict(best)
    if out.get("semantic_name"):
        return out

    try:
        t_best = float(out.get("last_seen") or 0.0)
    except (TypeError, ValueError):
        t_best = 0.0

    best_label: Optional[str] = None
    min_delta = float("inf")

    if t_best > 0:
        for n in normed_cands:
            label = n.get("semantic_name")
            if not label:
                continue
            try:
                t = float(n.get("last_seen") or 0.0)
            except (TypeError, ValueError):
                t = 0.0
            if t <= 0:
                continue
            delta = abs(t - t_best)
            if delta <= tolerance_s and delta < min_delta:
                best_label = str(label)
                min_delta = delta

    if best_label:
        out["semantic_name"] = best_label
    return out


def get_devices_with_location(device_list) -> List[Dict[str, Any]]:
    """Extract one consolidated row per canonic device ID from a device list.

    This function serves as a robust barrier against data contamination by
    ensuring its output is always clean, consistent, and predictable.

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
            decrypt_location_response_locations,
        )
    except Exception:
        # If the decrypt layer is unavailable, return stubs only.
        decrypt_location_response_locations = None  # type: ignore[assignment]

    results: List[Dict[str, Any]] = []

    for device in getattr(device_list, "deviceMetadata", []):
        # Resolve canonic IDs for this device (Android vs. generic path)
        try:
            if device.identifierInformation.type == DeviceUpdate_pb2.IDENTIFIER_ANDROID:
                canonic_ids = (
                    device.identifierInformation.phoneInformation.canonicIds.canonicId
                )
            else:
                canonic_ids = device.identifierInformation.canonicIds.canonicId
        except Exception:
            canonic_ids = []

        device_name = getattr(device, "userDefinedDeviceName", None) or ""

        # Try decryption ONCE per device; share across all its canonic IDs
        location_candidates: List[Dict[str, Any]] = []
        if decrypt_location_response_locations is not None:
            try:
                if device.HasField("information") and device.information.HasField(
                    "locationInformation"
                ):
                    has_reports = bool(
                        getattr(device.information.locationInformation, "reports", [])
                    )
                    if has_reports:
                        mock_device_update = DeviceUpdate_pb2.DeviceUpdate()
                        mock_device_update.deviceMetadata.CopyFrom(device)
                        location_candidates = (
                            decrypt_location_response_locations(mock_device_update) or []
                        )
            except Exception:
                # Defensive: decryption issues must not break the whole list.
                location_candidates = []

        # If decryption yielded results, select the best one and keep normalized list.
        if location_candidates:
            best, normed = _select_best_location(location_candidates)
            if best:
                best = _merge_semantics_if_near_ts(best, normed)
        else:
            best, normed = None, []

        # Emit **exactly one** row per canonic ID.
        for canonic in canonic_ids:
            cid = getattr(canonic, "id", None)
            if not (isinstance(cid, str) and cid):
                continue

            row = _build_device_stub(device_name, cid)

            if best:
                # best already normalized by selection; merge only known keys
                for k in _DEVICE_STUB_KEYS:
                    if k in best and best[k] is not None:
                        row[k] = best[k]
                # Ensure device identity fields are not overwritten by nested payloads
                row["name"] = device_name
                row["id"] = cid
                row["device_id"] = cid

            results.append(row)

    return results


# --------------------------------------------------------------------------------------
# Dev print helpers
# --------------------------------------------------------------------------------------

def print_location_report_upload_protobuf(hex_string: str):
    print(
        text_format.MessageToString(
            parse_location_report_upload_protobuf(hex_string),
            message_formatter=custom_message_formatter,
        )
    )


def print_device_update_protobuf(hex_string: str):
    print(
        text_format.MessageToString(
            parse_device_update_protobuf(hex_string),
            message_formatter=custom_message_formatter,
        )
    )


def print_device_list_protobuf(hex_string: str):
    print(
        text_format.MessageToString(
            parse_device_list_protobuf(hex_string),
            message_formatter=custom_message_formatter,
        )
    )


# --------------------------------------------------------------------------------------
# Developer entry point (protobuf regen + sample dumps)
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    # Recompile
    subprocess.run(["protoc", "--python_out=.", "ProtoDecoders/Common.proto"], cwd="../")
    subprocess.run(
        ["protoc", "--python_out=.", "ProtoDecoders/DeviceUpdate.proto"], cwd="../"
    )
    subprocess.run(
        ["protoc", "--python_out=.", "ProtoDecoders/LocationReportsUpload.proto"],
        cwd="../",
    )

    subprocess.run(["protoc", "--pyi_out=.", "ProtoDecoders/Common.proto"], cwd="../")
    subprocess.run(
        ["protoc", "--pyi_out=.", "ProtoDecoders/DeviceUpdate.proto"], cwd="../"
    )
    subprocess.run(
        ["protoc", "--pyi_out=.", "ProtoDecoders/LocationReportsUpload.proto"],
        cwd="../",
    )

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

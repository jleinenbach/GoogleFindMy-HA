# custom_components/googlefindmy/ProtoDecoders/decoder.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import binascii
import datetime
import math
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from google.protobuf import text_format

try:
    from zoneinfo import ZoneInfo  # stdlib, Python 3.9+
except ImportError:
    ZoneInfo = None  # type: ignore

from custom_components.googlefindmy.ProtoDecoders import (
    Common_pb2,
    DeviceUpdate_pb2,
    LocationReportsUpload_pb2,
)


# --------------------------------------------------------------------------------------
# Pretty printer helpers (dev tooling)
# --------------------------------------------------------------------------------------


def custom_message_formatter(message, indent, _as_one_line):
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
    if display_tz_name == "UTC" or ZoneInfo is None:
        display_tz = datetime.timezone.utc
        display_tz_name = "UTC"
    else:
        try:
            display_tz = ZoneInfo(display_tz_name)
        except Exception:
            display_tz = datetime.timezone.utc
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
                            unix_time, tz=datetime.timezone.utc
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
            else:
                if field.message_type.name == "Time":
                    # seconds (+ optional nanos) -> float seconds
                    secs = getattr(value, "seconds", 0)
                    nanos = getattr(value, "nanos", 0)
                    unix_time = float(secs) + float(nanos) / 1e9

                    dt_utc = datetime.datetime.fromtimestamp(
                        unix_time, tz=datetime.timezone.utc
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
                        value, f"{indent}  ", _as_one_line
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
    "status_code",
    "_report_hint",
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
        "status_code": None,
        "_report_hint": None,
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


def _get_rank_tuple(n: Dict[str, Any]) -> Tuple[int, int, float, float, str]:
    """Create a sort key tuple with status-based prioritization.
    Priority (high to low):
      1. Source/Status: Owner > Crowdsourced > Aggregated > Unknown
      2. Presence of coordinates
      3. Newer last_seen timestamp
      4. Better accuracy (smaller is better)
      5. Deterministic tie-breaker string
    """
    # 1) Owner-Reports always take precedence
    is_own = 1 if bool(n.get("is_own_report")) else 0

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
    if is_own:
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

    has_coords = (
        1
        if isinstance(n.get("latitude"), (int, float))
        and isinstance(n.get("longitude"), (int, float))
        else 0
    )
    seen = n.get("last_seen")
    seen_rank = (
        float(seen)
        if isinstance(seen, (int, float)) and math.isfinite(float(seen))
        else float("-inf")
    )
    acc = n.get("accuracy")
    acc_rank = (
        -float(acc)
        if isinstance(acc, (int, float)) and math.isfinite(float(acc))
        else float("-inf")
    )
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
    return (status_rank, has_coords, seen_rank, acc_rank, stable_key)


def _select_best_location(
    cands: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Choose the most useful location from a list of candidates.

    This function normalizes all candidates once, then sorts them based on a
    clear priority hierarchy to find the single most relevant location report.

    Priority (high to low):
        1) Status/Source (Owner > Crowdsourced > Aggregated)
        2) Presence of coordinates
        3) Newer `last_seen` timestamp
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
    normed_cands: List[Dict[str, Any]] = [_normalize_location_dict(c or {}) for c in cands]

    # Sort using the new rank tuple which prioritizes status
    normed_cands.sort(key=_get_rank_tuple, reverse=True)

    best_candidate = normed_cands[0]

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
                            decrypt_location_response_locations(mock_device_update)
                            or []
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
    msg = parse_location_report_upload_protobuf(hex_string)
    try:
        s = text_format.MessageToString(msg, message_formatter=custom_message_formatter)
    except TypeError:
        s = text_format.MessageToString(msg)
    print(s)


def print_device_update_protobuf(hex_string: str):
    msg = parse_device_update_protobuf(hex_string)
    try:
        s = text_format.MessageToString(msg, message_formatter=custom_message_formatter)
    except TypeError:
        s = text_format.MessageToString(msg)
    print(s)


def print_device_list_protobuf(hex_string: str):
    msg = parse_device_list_protobuf(hex_string)
    try:
        s = text_format.MessageToString(msg, message_formatter=custom_message_formatter)
    except TypeError:
        s = text_format.MessageToString(msg)
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
            ["protoc", "--pyi_out=.", "ProtoDecoders/Common.proto"], cwd="../", check=True
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

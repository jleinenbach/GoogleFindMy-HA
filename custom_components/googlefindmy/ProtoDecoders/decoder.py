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
from typing import TYPE_CHECKING, Any, Protocol

from google.protobuf import text_format
from google.protobuf.message import Message

try:
    from zoneinfo import ZoneInfo  # stdlib, Python 3.9+
except ImportError:
    ZoneInfo = None  # type: ignore

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache

from custom_components.googlefindmy.ProtoDecoders import (
    Common_pb2,
    DeviceUpdate_pb2,
    LocationReportsUpload_pb2,
)


class _DecryptLocationsCallable(Protocol):
    """Runtime signature for the decrypt helper imported lazily."""

    def __call__(
        self,
        device_update_protobuf: DeviceUpdate_pb2.DeviceUpdate,
        *,
        cache: "TokenCache",
    ) -> list[dict[str, Any]] | None:
        ...


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
            else:
                if field.message_type.name == "Time":
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
                    lines.append(
                        f"{indent}{field.name} {{\n{nested_message}\n{indent}}}"
                    )
        else:
            lines.append(f"{indent}{field.name}: {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Protobuf parse helpers (stable API)
# --------------------------------------------------------------------------------------


def parse_location_report_upload_protobuf(
    hex_string: str,
) -> LocationReportsUpload_pb2.LocationReportsUpload:
    """Parse LocationReportsUpload from a hex string."""
    location_reports = LocationReportsUpload_pb2.LocationReportsUpload()
    location_reports.ParseFromString(bytes.fromhex(hex_string))
    return location_reports


def parse_device_update_protobuf(
    hex_string: str,
) -> DeviceUpdate_pb2.DeviceUpdate:
    """Parse DeviceUpdate from a hex string."""
    device_update = DeviceUpdate_pb2.DeviceUpdate()
    device_update.ParseFromString(bytes.fromhex(hex_string))
    return device_update


def parse_device_list_protobuf(
    hex_string: str,
) -> DeviceUpdate_pb2.DevicesList:
    """Parse DevicesList from a hex string."""
    device_list = DeviceUpdate_pb2.DevicesList()
    device_list.ParseFromString(bytes.fromhex(hex_string))
    return device_list


# --------------------------------------------------------------------------------------
# Canonical ID extraction
# --------------------------------------------------------------------------------------


def get_canonic_ids(
    device_list: DeviceUpdate_pb2.DevicesList,
) -> list[tuple[str, str]]:
    """Return (device_name, canonic_id) for all devices in the list.

    Defensive policy:
        * Handle Android and non-Android identifier shapes.
        * Skip non-string/empty IDs to avoid downstream surprises.
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
                result.append((device_name, cid))
    return result


# --------------------------------------------------------------------------------------
# Location extraction with contamination shielding
# --------------------------------------------------------------------------------------

# Tunables to keep behavior explicit and easily auditable
_NEAR_TS_TOLERANCE_S: float = 5.0  # semantic merge tolerance (seconds)

_DEVICE_STUB_KEYS: tuple[str, ...] = (
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


def _build_device_stub(device_name: str, canonic_id: str) -> dict[str, Any]:
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


def _normalize_location_dict(loc: dict[str, Any]) -> dict[str, Any]:
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


def _get_rank_tuple(n: dict[str, Any]) -> tuple[float, int, int, float, str]:
    """Create a sort key tuple prioritizing the freshest timestamp.

    Priority (high to low):
      1. Newer ``last_seen`` timestamp
      2. Source/Status: Owner > Crowdsourced > Aggregated > Unknown
      3. Presence of coordinates (tie-breaker when timestamps/status match)
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
    return (seen_rank, status_rank, has_coords, acc_rank, stable_key)


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

    # Track the freshest coordinate-bearing candidate so semantic-only entries can
    # still expose stable position data after the merge step.
    best_coordinate: dict[str, Any] | None = None
    best_coordinate_ts = float("-inf")

    # Track the semantic label currently attached to the outgoing payload.
    semantic_label: str | None = None
    semantic_ts = float("-inf")
    if out.get("semantic_name"):
        semantic_label = str(out["semantic_name"])
        semantic_ts = _extract_ts(out.get("last_seen"))

    t_best = _extract_ts(out.get("last_seen"))

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
        if ts > latest_seen:
            latest_seen = ts

        if (
            isinstance(n.get("latitude"), (int, float))
            and isinstance(n.get("longitude"), (int, float))
            and ts >= best_coordinate_ts
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
    cache: "TokenCache" | None = None,
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
        location_candidates: list[dict[str, Any]] = []
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


def print_location_report_upload_protobuf(hex_string: str) -> None:
    msg = parse_location_report_upload_protobuf(hex_string)
    try:
        s = text_format.MessageToString(msg, message_formatter=custom_message_formatter)
    except TypeError:
        s = text_format.MessageToString(msg)
    print(s)


def print_device_update_protobuf(hex_string: str) -> None:
    msg = parse_device_update_protobuf(hex_string)
    try:
        s = text_format.MessageToString(msg, message_formatter=custom_message_formatter)
    except TypeError:
        s = text_format.MessageToString(msg)
    print(s)


def print_device_list_protobuf(hex_string: str) -> None:
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

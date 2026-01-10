# custom_components/googlefindmy/coordinator.py
"""Data coordinator for Google Find My Device (async-first, HA-friendly).

Discovery vs. polling semantics:
- Every coordinator tick fetches the lightweight **full** Google device list.
- Presence and name/capability caches are updated for all devices.
- The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
- The sequential **polling cycle** polls **only devices that are enabled** in Home Assistant's
  Device Registry (devices with `disabled_by is None`) for **this** config entry. Devices explicitly
  ignored via options are filtered out as well. Devices without a Device Registry entry yet are
  included to allow initial discovery.

Google Home semantic locations (note):
- When the Google Home filter identifies a "Google Home-like" semantic location,
  we substitute **Home zone coordinates** (lat/lon[/radius]) instead of forcing
  a zone label. This lets HA Core's zone engine set the state to `home`, which
  aligns with best practices.

Thread-safety and quality goals:
- All state mutations and task creations occur on HA's event loop thread.
- Public methods that may be invoked from background threads marshal execution
  onto the loop using a single hop (no chained hops).
- Owner-driven locates introduce a server-side purge/cooldown window; we respect
  this via **per-device poll cooldowns** with **dynamic guardrails** (min/max bounds),
  without changing any external API or entity fields.

Implementation notes (server behaviour, POPETS'25):
- The network applies *type-specific throttling* to crowdsourced reports:
  "In All Areas" reports are effectively throttled for ~10 minutes,
  "High Traffic" reports for ~5 minutes. We respect this by applying
  per-device cooldowns derived from an internal `_report_hint` (set by the
  decrypt/parse layer) without changing public APIs or entity attributes.
- The well-known "~9h" rate limit discussed in the paper applies to *finder*
  devices contributing reports, not to the owner pulling locations. We **document**
  this here for maintainers but do **not** enforce it client-side.

Authentication handling and HA best practices (Platinum standard):
- The **DataUpdateCoordinator** is the central place to detect auth failures.
- When the API raises `ConfigEntryAuthFailed`, we:
  1) Create (idempotently) a **Repairs issue** using Home Assistant's issue registry
     so the integration is marked with **"Reconfigure"** in the UI (system repairs).
  2) Fire a domain-scoped **event** so users can automate on top of the condition.
  3) Set an internal **flag** (`auth_error_active`) that the diagnostic binary_sensor
     can expose as `on` (see step 5.1-C, binary_sensor.py).
  4) Re-raise `ConfigEntryAuthFailed` from the coordinator so HA triggers the
     **Re-auth flow** defined in `config_flow.py` (platinum-standard behavior).
- As soon as any subsequent API call succeeds, we:
  1) Dismiss the Repairs issue,
  2) Fire a matching **OK event**,
  3) Clear the internal flag and push an update so the sensor flips back to `off`.

This module must not log secrets and must keep user-facing strings out of code; use translations instead.
# custom_components/googlefindmy/coordinator.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import warnings
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from statistics import mean, stdev
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from .helpers.cache import (
    build_base_snapshot_entry as _build_base_snapshot_entry_impl,
)
from .helpers.cache import (
    determine_location_status,
    epoch_to_datetime_utc,
    is_presence_expired,
)
from .helpers.geo import (
    clamp as _clamp,
)
from .helpers.geo import (
    coerce_float as _coerce_float_impl,
)
from .helpers.geo import (
    haversine_distance as _haversine_distance_impl,
)
from .helpers.geo import (
    safe_accuracy,
)
from .helpers.identity import (
    extract_pair_date as _extract_pair_date_impl,
)
from .helpers.identity import (
    extract_secrets_creation_date as _extract_secrets_creation_date_impl,
)
from .helpers.identity import (
    extract_time_anchors_debug as _extract_time_anchors_debug_impl,
)
from .helpers.identity import (
    lookup_prio as _lookup_prio_impl,
)
from .helpers.identity import (
    lookup_prio_with_source as _lookup_prio_with_source_impl,
)
from .helpers.identity import (
    normalize_device_type as _normalize_device_type_impl,
)
from .helpers.identity import (
    normalize_fast_pair_model_id as _normalize_fast_pair_model_id_impl,
)
from .helpers.identity import (
    normalize_identity_timestamp as _normalize_identity_timestamp_impl,
)
from .helpers.identity import (
    store_if_value as _store_if_value_impl,
)
from .helpers.registry import (
    build_legacy_device_registry_kwargs as _build_legacy_kwargs_impl,
)
from .helpers.registry import (
    extract_device_display_name as _extract_display_name_impl,
)
from .helpers.registry import (
    extract_service_subentry_ids as _extract_service_subentry_ids_impl,
)
from .helpers.registry import (
    has_hub_link as _has_hub_link_impl,
)
from .helpers.registry import (
    has_subentry_link as _has_subentry_link_impl,
)
from .helpers.registry import (
    is_hub_device_check as _is_hub_device_check_impl,
)
from .helpers.registry import (
    needs_legacy_kwarg_retry as _needs_legacy_retry_impl,
)
from .helpers.registry import (
    normalize_device_name as _normalize_device_name_impl,
)
from .helpers.registry import (
    parse_device_identifier as _parse_identifier_impl,
)
from .helpers.registry import (
    resolve_tracker_subentry_candidate as _resolve_tracker_subentry_impl,
)
from .helpers.registry import (
    should_defer_service_subentry as _should_defer_service_subentry_impl,
)
from .helpers.stats import (
    ApiStatus,
    DiagnosticsBuffer,
    FcmStatus,
    StatusSnapshot,
    format_recent_errors,
)
from .helpers.stats import (
    get_duration as _get_duration_impl,
)
from .helpers.stats import (
    short_error_message as _short_error_message_impl,
)
from .helpers.subentry import (
    detect_missing_core_subentry_keys as _detect_missing_core_keys_impl,
)
from .helpers.subentry import (
    extract_subentry_group_key as _extract_group_key_impl,
)
from .helpers.subentry import (
    filter_provisional_identifier as _filter_provisional_impl,
)
from .helpers.subentry import (
    format_epoch_utc as _format_epoch_utc_impl,
)
from .helpers.subentry import (
    group_devices_by_subentry as _group_devices_impl,
)
from .helpers.subentry import (
    normalize_epoch_seconds as _normalize_epoch_impl,
)
from .helpers.subentry import (
    parse_last_seen_timestamp as _parse_last_seen_impl,
)
from .helpers.subentry import (
    sanitize_subentry_identifier as _sanitize_subentry_id_impl,
)
from .helpers.update import (
    calculate_presence_ttl as _calculate_presence_ttl_impl,
)
from .helpers.update import (
    filter_and_dedupe_devices as _filter_and_dedupe_impl,
)
from .helpers.update import (
    is_fatal_fcm_auth_error as _is_fatal_fcm_auth_error_impl,
)
from .helpers.update import (
    is_poll_cycle_due as _is_poll_cycle_due_impl,
)
from .helpers.update import (
    normalize_device_list_payload as _normalize_device_list_impl,
)
from .helpers.update import (
    should_defer_empty_list as _should_defer_empty_list_impl,
)

# Operations classes - currently empty mixins for future method extraction
from .identity import IdentityOperations
from .locate import LocateOperations
from .polling import PollingOperations
from .registry import RegistryOperations
from .subentry import SubentryOperations

if TYPE_CHECKING:
    from homeassistant.core import Event

    from .. import ConfigEntrySubentryDefinition, ConfigEntrySubEntryManager
    from ..google_home_filter import GoogleHomeFilter as GoogleHomeFilterProtocol
else:  # pragma: no cover - typing fallback for runtime imports
    Event = Any
    GoogleHomeFilterProtocol = Any

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryAuthFailed,
    UnknownEntry,
    UnknownSubEntry,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import (
    issue_registry as ir,
)  # Repairs: modern "needs action" UI
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.helpers.entity_registry import RegistryEntry as EntityRegistryEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import UpdateFailed

# IMPORTANT: make Common_pb2 import **mandatory** (integration packaging must include it).
# This avoids silent type/name drift and keeps source labels stable.
from ..api import GoogleFindMyAPI
from ..Auth.token_cache import TokenCache
from ..const import (
    CACHE_KEY_CONTRIBUTOR_MODE,
    CACHE_KEY_LAST_MODE_SWITCH,
    # Credential meta for Repairs placeholders
    CONF_GOOGLE_EMAIL,
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    DATA_EID_RESOLVER,
    DEFAULT_CONTRIBUTOR_MODE,
    # Core / options
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DEVICE_LIST_POLL_INTERVAL,
    DOMAIN,
    # Required symbols provided by const.py (5.1-A)
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    INTEGRATION_VERSION,
    ISSUE_AUTH_EXPIRED_KEY,
    LEGACY_SERVICE_IDENTIFIER,
    LOCATION_REQUEST_TIMEOUT_S,
    # Helpers
    MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S,
    OPT_IGNORED_DEVICES,
    OPT_SEMANTIC_LOCATIONS,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    SERVICE_DEVICE_MANUFACTURER,
    # Integration "service device" metadata
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SERVICE_SUBENTRY_TRANSLATION_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    TRACKER_SUBENTRY_TRANSLATION_KEY,
    UPDATE_INTERVAL,
    coerce_ignored_mapping,
    issue_id_for,
    service_device_identifier,
)
from ..ha_typing import DataUpdateCoordinator, callback
from ..KeyBackup.cloud_key_decryptor import decrypt_eik
from ..NovaApi.nova_request import NovaAuthError, NovaAuthPermanentError
from ..SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from ..SpotApi.spot_request import SpotAuthPermanentError

_LOGGER = logging.getLogger(__name__)


_DEFAULT_SUBENTRY_FEATURES: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "device_tracker",
    "sensor",
)

_SERVICE_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(SERVICE_FEATURE_PLATFORMS))
)
_TRACKER_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(TRACKER_FEATURE_PLATFORMS))
)


# --- Lightweight cache protocol for entry-scoped persistence -----------------
class CacheProtocol(Protocol):  # pylint: disable=unnecessary-ellipsis
    """Minimal cache protocol used by the coordinator.

    Implementations are provided by the integration's token/cache layer.
    """

    async def async_get_cached_value(self, key: str) -> Any:
        """Return a cached value for a given key (or None)."""
        ...

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        """Persist a value under a given key (overwriting the previous one)."""
        ...


# --- Module constants (cooldowns & quorum) ---------------------------------
# Accept an empty device list only on the 2nd consecutive result (defers once)
_EMPTY_LIST_QUORUM = 2

# POPETS'25-informed throttling windows for crowdsourced reports
_COOLDOWN_MIN_IN_ALL_AREAS_S = 10 * 60  # 10 minutes
_COOLDOWN_MIN_HIGH_TRAFFIC_S = 5 * 60  # 5 minutes

# Maximum consecutive transient auth failures before triggering reauth.
# Transient failures (401 after token refresh) may self-heal as Google's
# backend propagates the refreshed token. We give it 3 poll cycles before
# asking the user to re-authenticate.
_MAX_TRANSIENT_AUTH_FAILURES = 3

# Guardrails for owner-driven locate cooldown
_COOLDOWN_OWNER_MIN_S = 60  # at least 1 minute
_COOLDOWN_OWNER_MAX_S = 15 * 60  # at most 15 minutes

# Sound UUID expiry after restart (prevents phantom re-triggers)
_SOUND_UUID_MAX_AGE_S = 30 * 60  # 30 minutes

# Minimum position change (meters) to accept location update without timestamp
_LOCATION_SIGNIFICANT_CHANGE_M = 50.0

# FCM error retry threshold before triggering re-auth
_FCM_ERROR_RETRY_THRESHOLD = 3

# Maximum delay before falling back to polling even when push is unavailable.
# [FIX: Reduce 300s -> 15s to allow degraded-mode polling quickly]
_PUSH_NOT_READY_TIMEOUT_S = 15
# Backward-compatible alias for legacy status tests; keep in sync with the
# primary timeout constant above.
_FCM_FALLBACK_POLL_AFTER_S = _PUSH_NOT_READY_TIMEOUT_S

# Altitude adjustments smaller than 1 m are considered noise for significance checks.
_ALTITUDE_SIGNIFICANT_DELTA_M = 1.0

# Predictive polling buffer to avoid requesting data before it is available server-side.
_PREDICTION_BUFFER_S = 45

# Fields consumed by get_active_device_identities from cached payloads/anchors.
_PERSISTED_METADATA_KEYS: tuple[str, ...] = (
    "battery_level",
    "device_type",
    "pair_date",
    "encrypted_account_key",
    "secrets_creation_date",
    "device_registration",
    "public_key_address",
    "device_type_information",
    "encrypted_user_secrets",
    "identity_key",
    "identity_key_candidates",
    "encrypted_identity_key",
    "encrypted_identity_key_candidates",
    "owner_key_version",
    "time_anchors_debug",
    # FIX: Add device metadata fields for phones
    "manufacturer",
    "model",
    "fast_pair_model_id",
)


def _update_preserve_metadata(
    target: dict[str, Any], source: Mapping[str, Any]
) -> None:
    """Merge source into target without overwriting persisted metadata keys with None.

    This prevents a payload with None values (e.g., from a phone device listing
    that lacks key material) from clobbering valid data already in the target
    (e.g., from a prior FCM locate response).

    For keys in _PERSISTED_METADATA_KEYS:
      - Only update if source value is not None
    For all other keys:
      - Normal dict.update() semantics apply
    """
    for key, value in source.items():
        if key in _PERSISTED_METADATA_KEYS:
            # Preserve existing non-None values: only overwrite if new value is not None
            if value is not None:
                target[key] = value
            # If value is None and key already exists with a value, keep the existing
        else:
            # Normal update for non-metadata keys
            target[key] = value


# _clamp is imported from coordinator_geo (module extraction Phase 1)


# DiagnosticsBuffer is imported from coordinator_stats (module extraction Phase 2)


# --- Subentry metadata ---------------------------------------------------------
@dataclass(slots=True, frozen=True)
class SubentryMetadata:
    """Lightweight view of a config-entry subentry relevant to platforms."""

    key: str
    config_subentry_id: str | None
    features: tuple[str, ...]
    title: str | None
    poll_intervals: Mapping[str, int]
    filters: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    visible_device_ids: tuple[str, ...]
    enabled_device_ids: tuple[str, ...]

    def stable_identifier(self) -> str:
        """Return the identifier to use when namespacing entities."""

        return self.config_subentry_id or self.key

    @property
    def subentry_id(self) -> str | None:
        """Backwards-compatible alias for the config subentry identifier."""

        return self.config_subentry_id


def _sanitize_subentry_identifier(candidate: Any) -> str | None:
    """Return a normalized subentry identifier or ``None`` when fabricated."""
    return _sanitize_subentry_id_impl(candidate)


# --- Epoch normalization (ms→s tolerant) -----------------------------------
def normalize_epoch_seconds(value: Any) -> int | None:
    """Return epoch seconds as an ``int`` with millisecond tolerance."""
    return _normalize_epoch_impl(value)


def _normalize_epoch_seconds(value: Any) -> int | None:
    """Backward-compatible alias for :func:`normalize_epoch_seconds`."""
    return _normalize_epoch_impl(value)


# NOTE: keep helper public for reuse in entities/system health snapshots.
def format_epoch_utc(value: Any) -> str | None:
    """Return an ISO 8601 UTC timestamp for epoch values (seconds or ms)."""
    return _format_epoch_utc_impl(value)


# --- Decoder-row Normalization & Attribute Helpers -------------------------
_MISSING = object()


def _get_common_pb2() -> Any:
    """Import Common_pb2 lazily to defer protobuf initialization."""

    from .. import get_proto_decoder

    return get_proto_decoder("Common_pb2")


def _row_source_label(row: dict[str, Any]) -> tuple[int, str]:
    """
    Determine (rank, label) for the source of a report.
    Rank: 3=owner, 2=crowdsourced, 1=aggregated, 0=semantic/unknown
    Label: 'owner' | 'crowdsourced' | 'aggregated' | 'semantic/unknown'

    Internal label mapping aligns with the protobuf Status enum and Google
    Contribution Settings:
    - 'crowdsourced' comes from Status.CROWDSOURCED (finder opted into
      "with network in all areas").
    - 'aggregated' comes from Status.AGGREGATED (finder opted into "with
      network in high-traffic areas only").
    """
    is_own = bool(row.get("is_own_report"))
    status_code = row.get("status_code")
    try:
        status_code_int = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code_int = None

    raw_status = row.get("status")
    if isinstance(raw_status, str):
        status_name = raw_status.strip().lower()
    elif isinstance(raw_status, (int, float)):
        try:
            status_name = _get_common_pb2().Status.Name(int(raw_status)).lower()
        except Exception:
            status_name = str(int(raw_status))
    else:
        status_name = ""

    hint = str(row.get("_report_hint") or "").strip().lower()
    common_pb2 = _get_common_pb2()
    cs = getattr(common_pb2, "CROWDSOURCED", _MISSING)
    ag = getattr(common_pb2, "AGGREGATED", _MISSING)

    if is_own:
        return 3, "owner"
    if (
        (cs is not _MISSING and status_code_int == cs)
        or "crowdsourced" in status_name
        or "in_all_areas" in status_name
        or hint == "in_all_areas"
    ):
        return 2, "crowdsourced"
    if (
        (ag is not _MISSING and status_code_int == ag)
        or "aggregated" in status_name
        or "high_traffic" in status_name
        or hint == "high_traffic"
    ):
        return 1, "aggregated"
    return 0, "semantic/unknown"


def _parse_last_seen_timestamp(value: Any) -> float | None:
    """Parse a last_seen candidate into epoch seconds."""
    return _parse_last_seen_impl(value)


def _resolve_last_seen_from_attributes(
    attributes: Mapping[str, Any] | None, fallback: float | None
) -> float | None:
    """Prefer attribute-derived timestamps and fall back to provided default."""

    if not attributes:
        return fallback

    candidate: Any = attributes.get("last_seen")
    if candidate is None:
        candidate = attributes.get("last_seen_utc")

    ts = _parse_last_seen_timestamp(candidate)
    if ts is not None:
        return ts
    return fallback


def _sanitize_decoder_row(row: dict[str, Any]) -> dict[str, Any]:
    """Enforce protocol invariants and prepare HA attributes.

    Notes:
        - Ensures timestamps are normalized and provides an ISO UTC mirror.
        - Derives a stable `source_label`/`source_rank` used for stats and UX.
        - Zero-accuracy semantic entries are coerced to None to avoid noise.
    """
    r = dict(row)
    rank, label = _row_source_label(r)

    if label == "semantic/unknown" and r.get("is_own_report"):
        r["is_own_report"] = False
    if label == "semantic/unknown" and r.get("accuracy") in (0, 0.0):
        r["accuracy"] = None

    ts = _normalize_epoch_seconds(r.get("last_seen"))
    r["last_seen"] = ts  # Store normalized float
    r["last_seen_utc"] = format_epoch_utc(ts)

    r["source_label"] = label
    r["source_rank"] = rank
    return r


def _as_ha_attributes(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Create a curated, stable attribute set for HA entities (recorder-friendly)."""
    if not row:
        return None
    r = _sanitize_decoder_row(row)

    def _cf(value: Any) -> float | None:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
        return candidate if math.isfinite(candidate) else None

    lat = _cf(r.get("latitude"))
    lon = _cf(r.get("longitude"))
    acc = _cf(r.get("accuracy"))
    alt = _cf(r.get("altitude"))

    last_seen_iso = format_epoch_utc(r.get("last_seen"))
    last_seen_utc = r.get("last_seen_utc") or last_seen_iso

    out: dict[str, Any] = {
        "device_name": r.get("name"),
        "device_id": r.get("device_id") or r.get("id"),
        "status": r.get("status"),
        "semantic_name": r.get("semantic_name"),
        "battery_level": r.get("battery_level"),
        "last_seen": last_seen_iso,
        "last_seen_utc": last_seen_utc,
        "source_label": r.get("source_label"),
        "source_rank": r.get("source_rank"),
        "is_own_report": r.get("is_own_report"),
    }
    if lat is not None and lon is not None:
        out["latitude"] = lat
        out["longitude"] = lon
    if acc is not None:
        out["accuracy_m"] = acc
    if alt is not None:
        out["altitude_m"] = alt
    return {k: v for k, v in out.items() if v is not None}


class _RecorderHistoryProxy:
    """Lazy loader for Recorder history helpers."""

    _module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            from homeassistant.components.recorder import history as history_module

            self._module = history_module
        return self._module

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - proxy passthrough
        return getattr(self._load(), name)

    def __setattr__(
        self, name: str, value: Any
    ) -> None:  # pragma: no cover - proxy passthrough
        if name == "_module":
            super().__setattr__(name, value)
            return
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:  # pragma: no cover - proxy passthrough
        if name == "_module":
            super().__delattr__(name)
            return
        try:
            delattr(self._load(), name)
        except AttributeError:
            # Mirror monkeypatch's raising=False semantics by ignoring missing attrs.
            return


recorder_history = _RecorderHistoryProxy()


def get_recorder(hass: HomeAssistant) -> Any:
    """Lazily import and return the Recorder instance for a hass object."""

    from homeassistant.components.recorder import get_instance as get_recorder_instance

    return get_recorder_instance(hass)


# -------------------------------------------------------------------------
# Synchronous history helper (runs in Recorder executor)
# -------------------------------------------------------------------------
def _sync_get_last_gps_from_history(
    hass: HomeAssistant, entity_id: str
) -> dict[str, Any] | None:
    """Fetch the last state with GPS coordinates from Recorder History.

    IMPORTANT:
        This function is synchronous and performs database I/O. It MUST be
        run in a worker thread (e.g., the Recorder's executor) to avoid
        blocking the Home Assistant event loop.

    Args:
        hass: The Home Assistant instance.
        entity_id: The entity ID of the device_tracker to query.

    Returns:
        A dictionary containing the last known location data, or None if not found.
    """
    try:
        # Minimal query: the last single change for this entity_id
        # API expects an iterable of entity_ids.
        changes = recorder_history.get_last_state_changes(hass, 1, [entity_id])
        samples = changes.get(entity_id, [])
        if not samples:
            return None

        last_state = samples[-1]
        attrs = getattr(last_state, "attributes", {}) or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat is None or lon is None:
            return None

        last_seen_ts = _resolve_last_seen_from_attributes(
            attrs, last_state.last_updated.timestamp()
        )
        return {
            "latitude": lat,
            "longitude": lon,
            "accuracy": attrs.get("gps_accuracy"),
            "last_seen": last_seen_ts,
            "status": "Using historical data",
        }
    except Exception as err:
        _LOGGER.debug("History lookup failed for %s: %s", entity_id, err)
        return None


# StatusSnapshot, ApiStatus, FcmStatus are imported from coordinator_stats (Phase 2)


@dataclass
class SemanticLabelRecord:
    """Observed semantic label metadata cached for diagnostics."""

    label: str
    first_seen: float
    last_seen: float
    devices: set[str] = field(default_factory=set)

    def copy(self) -> SemanticLabelRecord:
        """Return a shallow copy with a cloned device set."""

        return replace(self, devices=set(self.devices))


@dataclass(slots=True)
class DeviceIdentity:
    """Stable identifier and key material for a tracker.

    Attributes:
        registry_id: Home Assistant device registry identifier for the tracker.
        canonical_id: Namespaced device identifier used by the integration's API.
        identity_key: Ephemeral identity key used to derive rotating EIDs.
        encrypted_identity_key: Encrypted EIK blob (hex-decoded) for on-demand decryption.
        owner_key_version: Version of the owner key used to encrypt the EIK.
        device_type: SpotDeviceType enum value reported by the tracker, when known.
        config_entry_id: Parent config entry ID that owns the tracker.
        fast_pair_model_id: Fast Pair model identifier advertised by the tracker.
        manufacturer: Tracker manufacturer string extracted from registration metadata.
        model: Tracker model string extracted from registration metadata.
        pair_date: Hypothesized tracker pairing epoch (seconds) derived from registrations
            or cached secrets; treated as advisory when reconciling EID timelines.
        secrets_creation_date: Creation time for the encrypted user secrets bundle, if
            present; surfaced for debugging because the exact server-side semantics are
            not yet confirmed.
        encrypted_account_key: Encrypted account key blob (hex-decoded) used for future
            account-key recovery flows.
        public_key_address: Encrypted SHA256 public address associated with the account
            key; surfaced for debugging and potential resolver extensions.
        time_anchors_debug: Optional debug payload with server-provided anchor hints used
            to reason about EID drift; shape may vary and is forwarded best-effort.
    """

    registry_id: str
    canonical_id: str
    identity_key: bytes | None
    encrypted_identity_key: bytes | None = None
    owner_key_version: int | None = None
    device_type: int | None = None
    config_entry_id: str | None = None
    fast_pair_model_id: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    pair_date: int | None = None
    secrets_creation_date: int | None = None
    encrypted_account_key: bytes | None = None
    public_key_address: bytes | None = None
    time_anchors_debug: Any | None = None


class GoogleFindMyCoordinator(
    RegistryOperations,
    SubentryOperations,
    PollingOperations,
    IdentityOperations,
    LocateOperations,
    DataUpdateCoordinator[list[dict[str, Any]]],
):
    """Coordinator that manages polling, cache, and push updates for Google Find My Device.

    Thread-safety & event loop rules (IMPORTANT):
    - All interactions that create HA tasks or publish state must occur on the HA event loop thread.
    - This class provides small helpers to "hop" from any background thread into the loop
      using `loop.call_soon_threadsafe(...)` before touching HA APIs.

    Pitfalls & mitigations (general guidance for future reviewers):
    - Pitfall 1 – return values with `call_soon_threadsafe`:
      `call_soon_threadsafe` does not propagate return values to the calling thread.
      **Mitigation:** All methods we marshal to the loop in this module (e.g. `increment_stat`,
      `update_device_cache`, `push_updated`, `purge_device`) are consciously `None`-returning.
    - Pitfall 2 – excessive thread hops:
      Unnecessary hops add overhead if used for micro-operations.
      **Mitigation:** We hop **once at the public method boundary**, then execute the
      complete logic on the HA loop (single-threaded, deterministic).
    - Pitfall 3 – complex external locks:
      Using extra `threading.Lock`s for shared state increases complexity and risk.
      **Mitigation:** We **serialize** state changes by marshalling to the **single-threaded**
      HA event loop – the loop itself is the synchronization primitive.
    """

    # ---------------------------- Lifecycle ---------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        cache: CacheProtocol,
        *,
        location_poll_interval: int = 300,
        device_poll_delay: int = 5,
        min_poll_interval: int = DEFAULT_MIN_POLL_INTERVAL,
        allow_history_fallback: bool = False,
        contributor_mode: str = DEFAULT_CONTRIBUTOR_MODE,
        contributor_mode_switch_epoch: int | None = None,
    ) -> None:
        """Initialize the coordinator.

        This sets up the central data management for the integration, including
        API communication, state caching, and polling logic.

        Notes:
            - Credentials and related metadata are provided via the entry-scoped TokenCache.
            - The HA-managed aiohttp ClientSession is reused to avoid per-call pools.

        Args:
            hass: The Home Assistant instance.
            cache: An object implementing the CacheProtocol for persistent storage.
            location_poll_interval: The interval in seconds between polling cycles.
            device_poll_delay: The delay in seconds between polling individual devices.
            min_poll_interval: The minimum allowed interval between polling cycles.
            allow_history_fallback: Whether to fall back to Recorder history for location.
            contributor_mode: Preferred Nova contributor mode ("high_traffic" or "in_all_areas").
            contributor_mode_switch_epoch: Epoch timestamp when the contributor mode last changed.
        """
        self.hass = hass
        self._cache = cache
        self.config_entry: ConfigEntry | None = getattr(self, "config_entry", None)

        # Try to ensure entry-scoped namespace on the cache early when possible.
        # This will be finalized in async_setup() once the ConfigEntry is bound.
        try:
            if (
                not getattr(self._cache, "entry_id", None)
                and self.config_entry is not None
            ):
                setattr(self._cache, "entry_id", self.config_entry.entry_id)
        except Exception:
            pass

        # Get the singleton aiohttp.ClientSession from Home Assistant and reuse it.
        self._session = async_get_clientsession(hass)

        normalized_mode = self._sanitize_contributor_mode(contributor_mode)
        if contributor_mode_switch_epoch is None or contributor_mode_switch_epoch <= 0:
            contributor_mode_switch_epoch = int(time.time())
        self._contributor_mode = normalized_mode
        self._contributor_mode_switch_epoch = int(contributor_mode_switch_epoch)

        self.api = GoogleFindMyAPI(
            cache=self._cache,
            session=self._session,
            contributor_mode=self._contributor_mode,
            contributor_mode_switch_epoch=self._contributor_mode_switch_epoch,
        )

        # Configuration (user options; updated via update_settings())
        self.location_poll_interval = int(location_poll_interval)
        self.device_poll_delay = int(device_poll_delay)
        self.min_poll_interval = int(
            min_poll_interval
        )  # hard lower bound between cycles
        self.allow_history_fallback = bool(allow_history_fallback)

        # Initialize diagnostics buffer and one-shot warning guard for malformed IDs.
        self._diag = DiagnosticsBuffer(max_items=200)
        self._warned_bad_identifier_devices: set[str] = set()

        # Internal caches & bookkeeping
        self._device_location_data: dict[
            str, dict[str, Any]
        ] = {}  # device_id -> location dict
        self._device_names: dict[str, str] = {}  # device_id -> human name
        self._device_caps: dict[
            str, dict[str, Any]
        ] = {}  # device_id -> caps (e.g., {"can_ring": True})
        self._device_update_history: dict[str, deque[float]] = {}
        self._present_device_ids: set[str] = (
            set()
        )  # diagnostics-only set from latest non-empty list
        self._semantic_label_cache: dict[str, SemanticLabelRecord] = {}

        # Flag to separate initial discovery from later runtime additions.
        # After the first successful non-empty device list is processed, this becomes True.
        self._initial_discovery_done: bool = False

        # Presence smoothing (TTL):
        # - Per-device "last seen in full list" timestamp (monotonic)
        # - Cold-start marker: timestamp of last non-empty list (monotonic)
        # - Presence TTL in seconds (derived from poll interval, min 120s)
        self._present_last_seen: dict[str, float] = {}
        self._last_nonempty_wall: float = 0.0
        self._presence_ttl_s: int = 120

        # Minimal hardening state (empty-list quorum)
        self._last_device_list: list[dict[str, Any]] = []
        self._empty_list_streak: int = 0

        # Polling state
        self._poll_lock = asyncio.Lock()
        self._is_polling = False
        self._startup_complete = False
        self._last_poll_mono: float = 0.0  # monotonic timestamp for scheduling
        self._last_list_poll_mono: float = (
            0.0  # monotonic timestamp for discovery pacing
        )

        # Push readiness deferral/escalation bookkeeping
        self._fcm_defer_started_mono: float = 0.0
        self._fcm_last_stage: int = 0  # 0=none, 1=warned, 2=errored

        # Push readiness memoization and cooldown after transport errors
        self._push_ready_memo: bool | None = None
        self._push_cooldown_until: float = 0.0

        # Manual locate gating (UX + server protection)
        self._locate_inflight: set[str] = set()  # device_id -> in-flight flag
        self._locate_cooldown_until: dict[str, float] = {}  # device_id -> mono deadline

        # Per-device action locks to prevent race conditions on concurrent requests
        self._device_action_locks: dict[str, asyncio.Lock] = {}

        # Play Sound UUID tracking (needed to properly cancel sound requests)
        self._sound_request_uuids: dict[str, str] = {}  # device_id -> request_uuid
        self._sound_request_timestamps: dict[str, float] = {}  # device_id -> creation timestamp

        # Per-device poll cooldowns after owner/crowdsourced reports.
        self._device_poll_cooldown_until: dict[str, float] = {}

        # DR-driven poll targeting
        self._enabled_poll_device_ids: set[str] = set()
        self._devices_with_entry: set[str] = set()
        self._dr_unsub: Callable[[], None] | None = None
        self._unsub_scheduler: Callable[[], None] | None = None
        self.eid_resolver: Any | None = None
        # Subentry awareness (feature groups / platform scoping)
        self._subentry_metadata: dict[str, SubentryMetadata] = {}
        self._subentry_snapshots: dict[str, tuple[dict[str, Any], ...]] = {}
        self._feature_to_subentry: dict[str, str] = {}
        self._default_subentry_key_value: str = "core_tracking"
        self._subentry_manager: ConfigEntrySubEntryManager | None = None
        self._pending_subentry_repair: asyncio.Task[None] | None = None

        # Statistics (extend as needed)
        self.stats: dict[str, int] = {
            "background_updates": 0,  # FCM/push-driven updates + manual commits
            "polled_updates": 0,  # sequential poll-driven updates
            "crowd_sourced_updates": 0,  # number of crowdsourced updates observed
            "history_fallback_used": 0,  # times we had to fall back to Recorder history
            "timeouts": 0,  # request timeouts
            "invalid_coords": 0,  # coordinate validation failures
            "invalid_ts_drop_count": 0,  # invalid or stale (< existing) timestamps
            "future_ts_drop_count": 0,  # timestamps too far in the future
            "drop_reason_invalid_ts": 0,  # invalid/stale timestamps (detail bucket)
            "fused_updates": 0,  # overlapping fixes fused to stabilize coordinates
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        self._consecutive_timeouts: int = 0
        self._last_poll_result: str | None = None

        # Granular status tracking (API polling vs. push transport)
        self._api_status_state: str = ApiStatus.UNKNOWN
        self._api_status_reason: str | None = None
        self._api_status_changed_at: float | None = None
        self._fcm_status_state: str = FcmStatus.UNKNOWN
        self._fcm_status_reason: str | None = None
        self._fcm_status_changed_at: float | None = None

        # Performance metrics (timestamps, durations) & recent errors (bounded)
        self.performance_metrics: dict[str, float] = {}
        self.recent_errors: deque[tuple[float, str, str]] = deque(maxlen=5)

        # Debounced stats persistence (avoid flushing on every increment)
        self._stats_save_task: asyncio.Task[None] | None = None
        self._stats_debounce_seconds: float = 5.0

        # Load persistent statistics asynchronously (name the task for better debugging)
        self.hass.async_create_task(
            self._async_load_stats(), name=f"{DOMAIN}.load_stats"
        )

        # One-shot device list refresh trigger (used after reconfigure)
        self._force_device_list_refresh: bool = False
        self._force_device_list_reason: str | None = None

        # Marker for diagnostics after reconfigure-triggered reloads
        self._recent_reconfigure_at: float | None = None

        # Short-retry scheduling handle (coalesced)
        self._short_retry_cancel: Callable[[], None] | None = None

        # NEW: Authentication/repairs state
        self._auth_error_active: bool = False
        self._auth_error_since: float = 0.0
        self._auth_error_message: str | None = None

        # FIX: FCM error counter to avoid triggering re-auth on transient errors (#114)
        self._fcm_error_count: int = 0
        self._fcm_last_error: str | None = None

        # Transient auth error tracking: only trigger reauth after consecutive failures
        # across multiple poll cycles. This prevents premature reauth prompts when
        # Google's backend is temporarily slow to propagate refreshed tokens.
        self._consecutive_transient_auth_failures: int = 0
        self._last_transient_auth_error: str | None = None

        # Reload guard: defer core subentry repairs once after reload-driven attach
        self._skip_repair_during_reload_refresh: bool = False
        self._reload_repair_skip_pending_release: bool = False

        # Shared tracker support: identity_key → set of device_ids mapping
        # Enables Location propagation across devices sharing the same tracker
        self._identity_key_to_devices: dict[bytes, set[str]] = {}
        # Guard against infinite propagation loops
        self._propagating_location: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def async_config_entry_first_refresh(self) -> None:
        """Run the first refresh, tolerating coordinators without the helper."""

        try:
            parent_first_refresh = super().async_config_entry_first_refresh
        except AttributeError:  # pragma: no cover - compatibility with older cores
            parent_first_refresh = None

        if parent_first_refresh is not None:
            await parent_first_refresh()
            return

        _LOGGER.debug(
            "[%s] Falling back to async_refresh for initial coordinator sync",
            self._entry_id() or "unknown",
        )
        await self.async_refresh()

    def mark_recent_reconfigure(self, when: float | None = None) -> None:
        """Record that a reconfigure just completed for diagnostics."""

        when_ts = when if when is not None else time.time()
        try:
            self._recent_reconfigure_at = float(when_ts)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            self._recent_reconfigure_at = None

    @property
    def recent_reconfigure_at(self) -> float | None:
        """Return the most recent reconfigure marker if present."""

        return self._recent_reconfigure_at

    def request_device_list_refresh(
        self, *, reason: str | None = None, schedule_refresh: bool = True
    ) -> None:
        """Force the next update cycle to refetch the device list immediately."""

        if not self._is_on_hass_loop():
            self._run_on_hass_loop(
                self.request_device_list_refresh,
                reason=reason,
                schedule_refresh=schedule_refresh,
            )
            return

        if not self._force_device_list_refresh:
            self._force_device_list_refresh = True
            self._force_device_list_reason = reason

            _LOGGER.debug(
                "[%s] Forcing device list refresh%s",
                self._entry_id() or "unknown",
                f" ({reason})" if reason else "",
            )

        if schedule_refresh:
            self._dispatch_async_request_refresh(
                task_name=f"{DOMAIN}.force_device_list_refresh",
                log_context="request_device_list_refresh",
            )

    @property
    def cache(self) -> CacheProtocol:
        """Return the entry-scoped token cache backing this coordinator."""

        return self._cache

    def attach_subentry_manager(
        self, manager: ConfigEntrySubEntryManager, *, is_reload: bool = False
    ) -> None:
        """Attach the config entry subentry manager to the coordinator."""

        self._subentry_manager = manager
        self._skip_repair_during_reload_refresh = bool(is_reload)
        self._reload_repair_skip_pending_release = False
        if manager is None:
            return

        try:
            self._refresh_subentry_index(
                skip_manager_update=True, skip_repair=is_reload
            )
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "Initial subentry refresh failed during setup: %s",
                err,
            )
            return

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)
        if service_meta is not None and service_meta.config_subentry_id:
            try:
                self._ensure_service_device_exists()
            except Exception as err:  # noqa: BLE001 - defensive guard
                _LOGGER.debug(
                    "Service device ensure skipped during setup: %s",
                    err,
                )

    def _default_subentry_key(self) -> str:
        """Return the default subentry key used when no explicit mapping exists."""

        return self._default_subentry_key_value or "core_tracking"

    async def async_wait_subentry_visibility_updates(self) -> None:
        """Await pending visibility updates scheduled by the subentry manager."""

        manager = self._subentry_manager
        wait_visible = getattr(manager, "async_wait_visible_device_updates", None)
        if not callable(wait_visible):
            return

        try:
            await wait_visible()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "[%s] Visibility wait helper skipped due to: %s",
                self._entry_id() or "unknown",
                err,
            )

    def _build_core_subentry_definitions(
        self,
    ) -> list[ConfigEntrySubentryDefinition]:
        """Return definitions for the core tracker/service subentries."""

        entry = self.config_entry or getattr(self, "entry", None)
        entry_id = getattr(entry, "entry_id", None) if entry is not None else None
        if entry is None or not isinstance(entry_id, str) or not entry_id:
            _LOGGER.debug(
                "Skipping core subentry repair: config entry unavailable (entry=%s)",
                entry,
            )
            return []

        try:
            from .. import ConfigEntrySubentryDefinition  # local import to avoid cycles
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Skipping core subentry repair: definition factory import failed (%s)",
                err,
            )
            return []

        runtime_data = getattr(entry, "runtime_data", None)
        fcm_receiver = getattr(runtime_data, "fcm_receiver", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)

        fcm_push_enabled = fcm_receiver is not None
        has_google_home_filter = google_home_filter is not None
        entry_title = getattr(entry, "title", None) or "Google Find My"

        tracker_features = list(_TRACKER_SUBENTRY_FEATURES or TRACKER_FEATURE_PLATFORMS)
        service_features = list(_SERVICE_SUBENTRY_FEATURES or SERVICE_FEATURE_PLATFORMS)

        tracker_definition = ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Google Find My devices",
            data={
                "features": tracker_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
            translation_key=TRACKER_SUBENTRY_TRANSLATION_KEY,
        )
        service_definition = ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title="Google Find Hub Service",
            data={
                "features": service_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
            translation_key=SERVICE_SUBENTRY_TRANSLATION_KEY,
        )

        return [tracker_definition, service_definition]

    def _schedule_core_subentry_repair(self, missing_keys: set[str]) -> None:
        """Schedule a repair task to recreate missing core subentries."""

        if not missing_keys:
            return

        manager = self._subentry_manager
        hass = getattr(self, "hass", None)
        if manager is None or hass is None:
            return

        pending = self._pending_subentry_repair
        if pending is not None and not pending.done():
            _LOGGER.debug(
                "Core subentry repair already running; deferring additional request (%s)",
                sorted(missing_keys),
            )
            return

        entry_id = self._entry_id() or "unknown"

        async def _repair() -> None:
            try:
                if not self._config_entry_exists(entry_id):
                    _LOGGER.debug(
                        "Skipping core subentry repair for %s: entry removed", entry_id
                    )
                    return

                definitions = self._build_core_subentry_definitions()
                if not definitions:
                    _LOGGER.debug(
                        "Core subentry repair skipped for %s: definitions unavailable",
                        entry_id,
                    )
                    return

                _LOGGER.debug(
                    "Repairing missing subentries %s for entry %s",
                    sorted(missing_keys),
                    entry_id,
                )
                await manager.async_sync(definitions)
            except asyncio.CancelledError:  # pragma: no cover - task cancelled
                raise
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.warning(
                    "Core subentry repair failed for entry %s: %s",
                    entry_id,
                    err,
                )
                return
            finally:
                self._pending_subentry_repair = None

            if not self._config_entry_exists(entry_id):
                _LOGGER.debug(
                    "Skipping core subentry post-processing for %s: entry removed",
                    entry_id,
                )
                return

            self._ensure_service_device_exists()
            self._refresh_subentry_index()
            _LOGGER.debug("Core subentry repair completed for entry %s", entry_id)

        task_name = f"{DOMAIN}.repair_core_subentries"
        create_task = getattr(hass, "async_create_task", None)
        if callable(create_task):
            task = create_task(_repair(), name=task_name)
        else:  # pragma: no cover - fallback for legacy stubs
            task = asyncio.create_task(_repair(), name=task_name)
        self._pending_subentry_repair = task

    def _cancel_pending_subentry_repair(self) -> None:
        """Cancel any pending core subentry repair task."""

        pending = self._pending_subentry_repair
        if pending is None:
            return

        if not pending.done():
            pending.cancel()

        self._pending_subentry_repair = None

    def _refresh_subentry_index(
        self,
        visible_devices: Sequence[Mapping[str, Any]] | None = None,
        *,
        skip_manager_update: bool = False,
        skip_repair: bool = False,
    ) -> None:
        """Refresh internal subentry metadata caches."""

        if not hasattr(self, "_present_last_seen"):
            self._present_last_seen = {}
        if not hasattr(self, "_present_device_ids"):
            self._present_device_ids = set()

        reload_skip_active = bool(
            getattr(self, "_skip_repair_during_reload_refresh", False)
        )
        reload_skip_consumed = False
        if reload_skip_active and not skip_repair:
            skip_repair = True
            reload_skip_consumed = True
            self._reload_repair_skip_pending_release = True

        entry = self.config_entry

        entry_id = getattr(entry, "entry_id", None)
        entry_service_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "service_subentry_id", None))
            if entry is not None
            else None
        )
        entry_tracker_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "tracker_subentry_id", None))
            if entry is not None
            else None
        )

        service_provisional_seen = False
        tracker_provisional_seen = False

        raw_entries: list[tuple[str, str | None, dict[str, Any], str | None]] = []
        core_group_keys_present: set[str] = set()
        if entry and getattr(entry, "subentries", None):
            for subentry in entry.subentries.values():
                data = dict(getattr(subentry, "data", {}) or {})
                subentry_id_raw = getattr(subentry, "subentry_id", None)
                group_key = _extract_group_key_impl(data, subentry_id_raw)
                if group_key in (SERVICE_SUBENTRY_KEY, TRACKER_SUBENTRY_KEY):
                    core_group_keys_present.add(group_key)
                identifier = _sanitize_subentry_identifier(subentry_id_raw)

                # Use filter_provisional_identifier for service subentries
                if group_key == SERVICE_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_service_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        service_provisional_seen = True
                # Use filter_provisional_identifier for tracker subentries
                elif group_key == TRACKER_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_tracker_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        tracker_provisional_seen = True

                raw_entries.append(
                    (
                        group_key,
                        identifier,
                        data,
                        getattr(subentry, "title", None),
                    )
                )

        if entry is not None:
            missing_core_keys = _detect_missing_core_keys_impl(
                core_group_keys_present,
                SERVICE_SUBENTRY_KEY,
                TRACKER_SUBENTRY_KEY,
            )
        else:
            missing_core_keys = set()

        if not raw_entries:
            raw_entries.append(
                (
                    "core_tracking",
                    None,
                    {
                        "features": _DEFAULT_SUBENTRY_FEATURES,
                        "feature_flags": {},
                    },
                    getattr(entry, "title", None),
                )
            )

        ignored = self._get_ignored_set()
        device_index: dict[str, dict[str, Any]] = {}
        now_mono = time.monotonic()

        device_registry: dr.DeviceRegistry | None = None
        registry_lookup: Callable[[str], dr.DeviceEntry | None] | None = None
        hass_obj = getattr(self, "hass", None)
        if hass_obj is not None:
            try:
                device_registry = dr.async_get(hass_obj)
            except Exception:  # defensive: registry helpers may not be patched in tests
                device_registry = None
            else:
                candidate_lookup = getattr(device_registry, "async_get", None)
                if callable(candidate_lookup):
                    registry_lookup = candidate_lookup

        canonical_to_registry_id: dict[str, str] = {}
        registry_to_canonical: dict[str, str] = {}
        if device_registry is not None:
            candidate_entries: list[Any] = []
            raw_devices = getattr(device_registry, "devices", None)
            if isinstance(raw_devices, Mapping):
                candidate_entries.extend(raw_devices.values())
            else:
                registry_entries = getattr(device_registry, "_entries", None)
                if isinstance(registry_entries, Mapping):
                    candidate_entries.extend(registry_entries.values())

            if not candidate_entries:
                entry_id = self._entry_id()
                fetch_entries = getattr(dr, "async_entries_for_config_entry", None)
                if callable(fetch_entries) and entry_id:
                    try:
                        candidate_entries.extend(
                            fetch_entries(device_registry, entry_id)
                        )
                    except Exception:  # defensive: stub mismatches / legacy HA versions
                        candidate_entries = []

            for device_entry in candidate_entries:
                try:
                    canonical = self._extract_our_identifier(device_entry)
                except Exception:  # defensive: tolerate stub deviations
                    canonical = None
                if not canonical:
                    continue
                device_id_attr = getattr(device_entry, "id", None)
                if isinstance(device_id_attr, str) and device_id_attr:
                    canonical_to_registry_id.setdefault(canonical, device_id_attr)
                    registry_to_canonical.setdefault(device_id_attr, canonical)

        def _register_device(candidate: Mapping[str, Any]) -> None:
            dev_id = candidate.get("id")
            if not isinstance(dev_id, str) or not dev_id:
                fallback_id = candidate.get("device_id")
                if isinstance(fallback_id, str) and fallback_id:
                    dev_id = fallback_id
                else:
                    return
            if dev_id in ignored:
                return
            name = (
                candidate.get("name")
                if isinstance(candidate.get("name"), str)
                else None
            )
            device_index.setdefault(dev_id, {"id": dev_id, "name": name})

        if visible_devices is not None:
            for dev in visible_devices:
                if isinstance(dev, Mapping):
                    _register_device(dev)
        else:
            for dev in self.data or []:
                if isinstance(dev, Mapping):
                    _register_device(dev)

        if device_index:
            self._present_device_ids = set(device_index)
            for dev_id in device_index:
                self._present_last_seen.setdefault(dev_id, now_mono)

        previous_visible: dict[str, tuple[str, ...]] = {
            key: meta.visible_device_ids
            for key, meta in self._subentry_metadata.items()
        }

        metadata: dict[str, SubentryMetadata] = {}
        feature_map: dict[str, str] = {}
        default_key: str | None = None
        manager_visible: dict[str, tuple[str, ...]] = {}

        def _current_poll_intervals() -> Mapping[str, int]:
            return MappingProxyType(
                {
                    "location": int(self.location_poll_interval),
                    "minimum": int(self.min_poll_interval),
                    "device": int(self.device_poll_delay),
                }
            )

        def _current_filters() -> Mapping[str, Any]:
            return MappingProxyType(
                {
                    "ignored_device_ids": tuple(sorted(ignored)),
                    "allow_history_fallback": bool(self.allow_history_fallback),
                }
            )

        for group_key, subentry_id, data, title in raw_entries:
            raw_features = data.get("features")
            if isinstance(raw_features, (list, tuple, set)):
                normalized_features = tuple(
                    sorted(
                        {
                            str(feature)
                            for feature in raw_features
                            if isinstance(feature, str)
                        }
                    )
                )
            else:
                normalized_features = _DEFAULT_SUBENTRY_FEATURES

            if group_key == SERVICE_SUBENTRY_KEY:
                features = _SERVICE_SUBENTRY_FEATURES or normalized_features
            elif group_key == TRACKER_SUBENTRY_KEY:
                features = _TRACKER_SUBENTRY_FEATURES or normalized_features
            else:
                features = normalized_features or _DEFAULT_SUBENTRY_FEATURES

            raw_flags = data.get("feature_flags")
            feature_flags: dict[str, Any]
            if isinstance(raw_flags, Mapping):
                feature_flags = {str(key): raw_flags[key] for key in raw_flags}
            else:
                feature_flags = dict[str, Any]()

            raw_allowed = data.get("visible_device_ids")
            normalized_allowed: set[str] | None = None
            if isinstance(raw_allowed, (list, tuple, set)):
                collected: set[str] = set()
                for item in raw_allowed:
                    if not isinstance(item, str) or not item:
                        continue
                    cleaned = item.rsplit(":", 1)[-1] if ":" in item else item
                    if cleaned:
                        collected.add(cleaned)
                if collected:
                    normalized_allowed = set(collected)
                    if registry_lookup is not None:
                        resolved: set[str] = set()
                        for candidate in collected:
                            try:
                                device_entry = registry_lookup(candidate)
                            except Exception:  # defensive against stub mismatches
                                device_entry = None
                            if device_entry is None:
                                continue
                            canonical = self._extract_our_identifier(device_entry)
                            if canonical:
                                resolved.add(canonical)
                        if resolved:
                            normalized_allowed.update(resolved)
                else:
                    normalized_allowed = None

            allow_filter = normalized_allowed

            if device_index:
                base_ids = [
                    dev_id
                    for dev_id in device_index
                    if allow_filter is None or dev_id in allow_filter
                ]
            else:
                base_ids = [
                    dev_id
                    for dev_id in previous_visible.get(group_key, ())
                    if allow_filter is None or dev_id in allow_filter
                ]

            visibility_candidates: list[str] = list(base_ids)
            if normalized_allowed:
                visibility_candidates.extend(normalized_allowed)

            visible_ids = tuple(sorted(dict.fromkeys(visibility_candidates)))
            if group_key != SERVICE_SUBENTRY_KEY and registry_to_canonical:
                canonicalized_ids: list[str] = []
                for dev_id in visible_ids:
                    canonicalized_ids.append(dev_id)
                    canonical_id = registry_to_canonical.get(dev_id)
                    if canonical_id and canonical_id != dev_id:
                        canonicalized_ids.append(canonical_id)
                visible_ids = tuple(sorted(dict.fromkeys(canonicalized_ids)))

            if group_key == SERVICE_SUBENTRY_KEY:
                visible_ids = cast(tuple[str, ...], ())
                enabled_ids = cast(tuple[str, ...], ())
                manager_visible_ids = cast(tuple[str, ...], ())
            else:
                enabled_ids = tuple(
                    sorted(
                        dev_id
                        for dev_id in visible_ids
                        if dev_id in self._enabled_poll_device_ids
                    )
                )
                manager_visible_ids = tuple(
                    dict.fromkeys(
                        canonical_to_registry_id.get(dev_id, dev_id)
                        for dev_id in visible_ids
                    )
                )

            metadata[group_key] = SubentryMetadata(
                key=group_key,
                config_subentry_id=subentry_id,
                features=features,
                title=title,
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType(dict(feature_flags)),
                visible_device_ids=visible_ids,
                enabled_device_ids=enabled_ids,
            )

            if group_key != SERVICE_SUBENTRY_KEY:
                manager_visible[group_key] = manager_visible_ids

            for feature in features:
                feature_map.setdefault(feature, group_key)

            if default_key is None:
                default_key = group_key

        if SERVICE_SUBENTRY_KEY not in metadata:
            service_features = _SERVICE_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            stable_service_id: str | None
            if isinstance(entry_id, str) and entry_id and not service_provisional_seen:
                stable_service_id = f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry"
            else:
                stable_service_id = None
            metadata[SERVICE_SUBENTRY_KEY] = SubentryMetadata(
                key=SERVICE_SUBENTRY_KEY,
                config_subentry_id=stable_service_id,
                features=service_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=(),
                enabled_device_ids=(),
            )
            for feature in service_features:
                feature_map.setdefault(feature, SERVICE_SUBENTRY_KEY)

        if TRACKER_SUBENTRY_KEY not in metadata:
            tracker_features = _TRACKER_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            previous_tracker_visible = previous_visible.get(TRACKER_SUBENTRY_KEY, ())
            stable_tracker_id: str | None
            if isinstance(entry_id, str) and entry_id and not tracker_provisional_seen:
                stable_tracker_id = f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry"
            else:
                stable_tracker_id = None

            if device_index:
                tracker_visible_ids = tuple(sorted(device_index.keys()))
            else:
                tracker_visible_ids = previous_tracker_visible
            metadata[TRACKER_SUBENTRY_KEY] = SubentryMetadata(
                key=TRACKER_SUBENTRY_KEY,
                config_subentry_id=stable_tracker_id,
                features=tracker_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=tracker_visible_ids,
                enabled_device_ids=tuple(
                    dev_id
                    for dev_id in tracker_visible_ids
                    if dev_id in self._enabled_poll_device_ids
                ),
            )
            manager_visible[TRACKER_SUBENTRY_KEY] = tuple(
                dict.fromkeys(
                    canonical_to_registry_id.get(dev_id, dev_id)
                    for dev_id in tracker_visible_ids
                )
            )
            for feature in tracker_features:
                feature_map.setdefault(feature, TRACKER_SUBENTRY_KEY)

        if isinstance(entry_id, str) and entry_id:
            stable_ids = {
                SERVICE_SUBENTRY_KEY: f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry",
                TRACKER_SUBENTRY_KEY: f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry",
            }

            for key, default_id in stable_ids.items():
                if (key == SERVICE_SUBENTRY_KEY and service_provisional_seen) or (
                    key == TRACKER_SUBENTRY_KEY and tracker_provisional_seen
                ):
                    continue
                meta = metadata.get(key)
                if meta is None or meta.config_subentry_id is not None:
                    continue

                metadata[key] = replace(meta, config_subentry_id=default_id)

        self._subentry_metadata = metadata
        self._feature_to_subentry = feature_map
        if TRACKER_SUBENTRY_KEY in metadata:
            default_key = TRACKER_SUBENTRY_KEY
        elif default_key is None and metadata:
            default_key = next(iter(metadata))
        if default_key:
            self._default_subentry_key_value = default_key

        manager = self._subentry_manager
        if reload_skip_consumed:
            if visible_devices is not None or missing_core_keys:
                self._skip_repair_during_reload_refresh = False
                self._reload_repair_skip_pending_release = False
        elif (
            reload_skip_active
            and self._reload_repair_skip_pending_release
            and (visible_devices is not None or missing_core_keys)
        ):
            self._skip_repair_during_reload_refresh = False
            self._reload_repair_skip_pending_release = False

        if not skip_repair and missing_core_keys:
            self._schedule_core_subentry_repair(missing_core_keys)

        if manager and manager_visible and not skip_manager_update:
            for group_key, visible_ids in manager_visible.items():
                if group_key == SERVICE_SUBENTRY_KEY:
                    continue
                manager.update_visible_device_ids(group_key, visible_ids)
        # Ensure snapshot container has entries for all known keys
        for key in list(self._subentry_snapshots):
            if key not in metadata:
                self._subentry_snapshots.pop(key, None)
        for key in metadata:
            self._subentry_snapshots.setdefault(key, ())

    def _group_snapshot_by_subentry(
        self, snapshot: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return snapshot entries grouped by subentry key."""
        # Build device-to-subentry mapping from metadata
        device_to_key: dict[str, str] = {}
        for key, meta in self._subentry_metadata.items():
            for dev_id in meta.visible_device_ids:
                device_to_key.setdefault(dev_id, key)

        return _group_devices_impl(
            snapshot,
            device_to_key,
            self._default_subentry_key(),
            set(self._subentry_metadata.keys()),
        )

    def _store_subentry_snapshots(self, snapshot: Sequence[Mapping[str, Any]]) -> None:
        """Persist grouped snapshots for subentry-aware consumers."""

        grouped = self._group_snapshot_by_subentry(snapshot)
        self._subentry_snapshots = {
            key: tuple(entries) for key, entries in grouped.items()
        }

    def _resolve_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key for a platform feature without warnings."""

        return self._feature_to_subentry.get(feature, self._default_subentry_key())

    def get_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key responsible for a platform feature."""

        warnings.warn(
            "get_subentry_key_for_feature() is deprecated; pass the subentry key "
            "explicitly when constructing entities.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._resolve_subentry_key_for_feature(feature)

    def get_subentry_metadata(
        self, *, key: str | None = None, feature: str | None = None
    ) -> SubentryMetadata | None:
        """Return metadata for a given subentry key or feature."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            return None
        return self._subentry_metadata.get(lookup_key)

    def stable_subentry_identifier(
        self, *, key: str | None = None, feature: str | None = None
    ) -> str:
        """Return the stable identifier string for a subentry."""

        meta = self.get_subentry_metadata(key=key, feature=feature)
        if meta is not None:
            return meta.stable_identifier()
        if key:
            return key
        if feature:
            return feature
        return self._default_subentry_key()

    def get_subentry_snapshot(
        self, key: str | None = None, *, feature: str | None = None
    ) -> list[dict[str, Any]]:
        """Return a copy of the current snapshot for a subentry."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            lookup_key = self._default_subentry_key()
        entries = self._subentry_snapshots.get(lookup_key)
        if not entries:
            return []
        return [dict(row) for row in entries]

    def is_device_visible_in_subentry(self, subentry_key: str, device_id: str) -> bool:
        """Return True if a device is visible within the subentry scope.

        Handles both raw device IDs and namespaced identifiers (ENTRY_ID:DEVICE_ID)
        to ensure robust visibility checks in multi-account setups.
        """

        meta = self._subentry_metadata.get(subentry_key)
        if meta is None:
            return False

        # Fast path: Check for exact match (raw ID)
        if device_id in meta.visible_device_ids:
            return True

        # Robust path: Check for namespaced IDs (e.g., "01KBB...:DEVICE_ID")
        # The registry index may contain the fully qualified identifier.
        suffix = f":{device_id}"
        for visible_id in meta.visible_device_ids:
            if visible_id.endswith(suffix):
                return True

        return False

    def get_device_location_data_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return location data for a device if it belongs to the subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_location_data(device_id)

    def get_device_last_seen_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> datetime | None:
        """Return last_seen for a device within the given subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_last_seen(device_id)

    @staticmethod
    def _sanitize_contributor_mode(mode: str | None) -> str:
        """Normalize the contributor mode to a supported value."""

        if isinstance(mode, str):
            normalized = mode.strip().lower()
            if normalized in (
                CONTRIBUTOR_MODE_HIGH_TRAFFIC,
                CONTRIBUTOR_MODE_IN_ALL_AREAS,
            ):
                return normalized
        return DEFAULT_CONTRIBUTOR_MODE

    def _async_persist_contributor_mode(self) -> None:
        """Persist the contributor mode preferences asynchronously."""

        async def _persist() -> None:
            try:
                await self._cache.async_set_cached_value(
                    CACHE_KEY_CONTRIBUTOR_MODE, self._contributor_mode
                )
                await self._cache.async_set_cached_value(
                    CACHE_KEY_LAST_MODE_SWITCH,
                    self._contributor_mode_switch_epoch,
                )
            except Exception as err:  # pragma: no cover - defensive persistence
                _LOGGER.debug("Failed to persist contributor mode state: %s", err)

        self.hass.async_create_task(
            _persist(), name=f"{DOMAIN}.persist_contributor_mode"
        )

    async def async_setup(self) -> None:
        """One-time async setup called from __init__.py (entry setup).

        - Loads stats (already scheduled in __init__, so this is idempotent).
        - Indexes poll targets from the Device Registry.
        - Subscribes to DR updates (unsubscribed in `async_shutdown()`).
        - Ensures the per-entry "service device" exists in the Device Registry.
        - Enforces entry-scoped namespace by attaching `entry_id` to the cache object.
        """
        # Ensure the cache carries our entry_id namespace for downstream Nova/API helpers.
        try:
            entry = self.config_entry or getattr(self, "entry", None)
            if entry and not getattr(self._cache, "entry_id", None):
                setattr(self._cache, "entry_id", entry.entry_id)
        except Exception:
            pass

        # Make sure the service device exists early so end devices can link to it promptly.
        self._ensure_service_device_exists()

        # Initial index (works even if config_entry is not yet bound; will re-run on DR event)
        self._reindex_poll_targets_from_device_registry()
        if self._dr_unsub is None:
            self._dr_unsub = self.hass.bus.async_listen(
                EVENT_DEVICE_REGISTRY_UPDATED, self._handle_dr_event
            )

    def _get_google_home_filter(self) -> GoogleHomeFilterProtocol | None:
        """Return the Google Home filter associated with this coordinator."""

        entry = self.config_entry or getattr(self, "entry", None)
        runtime_data = getattr(entry, "runtime_data", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)
        return cast("GoogleHomeFilterProtocol | None", google_home_filter)

    def _should_preserve_precise_home_coordinates(
        self,
        prev_location: Mapping[str, Any] | None,
        replacement_attrs: Mapping[str, Any],
    ) -> bool:
        """Return True when cached coordinates are precise and still inside Home.

        The Google Home filter proposes substituting a semantic detection with the
        Home zone's coordinates and radius. Preserve the previously cached
        coordinates only when they are **both** more precise than the proposed
        radius **and** still fall within that radius, so we do not pin a device to
        an outdated off-site fix after a semantic Home report arrives.
        """

        if not prev_location:
            return False

        try:
            prev_lat = float(prev_location["latitude"])
            prev_lon = float(prev_location["longitude"])
            prev_acc = float(prev_location["accuracy"])
            proposed_lat = float(replacement_attrs["latitude"])
            proposed_lon = float(replacement_attrs["longitude"])
            proposed_radius = float(replacement_attrs["radius"])
        except (KeyError, TypeError, ValueError):
            return False

        if not (
            math.isfinite(prev_lat)
            and math.isfinite(prev_lon)
            and math.isfinite(prev_acc)
            and math.isfinite(proposed_lat)
            and math.isfinite(proposed_lon)
            and math.isfinite(proposed_radius)
        ):
            return False

        if proposed_radius <= 0 or prev_acc >= proposed_radius:
            return False

        return (
            self._haversine_distance(prev_lat, prev_lon, proposed_lat, proposed_lon)
            <= proposed_radius
        )

    async def async_shutdown(self) -> None:
        """Clean up listeners and timers on entry unload to avoid leaks."""
        self._cancel_pending_subentry_repair()
        # Unsubscribe DR listener
        dr_unsub = getattr(self, "_dr_unsub", None)
        if dr_unsub is not None:
            try:
                dr_unsub()
            except Exception:
                pass
            self._dr_unsub = None
        # Cancel short-retry callback if scheduled
        short_retry_cancel = getattr(self, "_short_retry_cancel", None)
        if short_retry_cancel is not None:
            try:
                short_retry_cancel()
            except Exception:
                pass
            finally:
                self._short_retry_cancel = None
        # Cancel pending debounced stats write
        stats_save_task = getattr(self, "_stats_save_task", None)
        if stats_save_task and not stats_save_task.done():
            stats_save_task.cancel()

        await self._async_unload()

    async def _async_unload(self) -> None:
        """Cleanup resources on unload."""
        unsub_scheduler = getattr(self, "_unsub_scheduler", None)
        if unsub_scheduler:
            unsub_scheduler()
            self._unsub_scheduler = None

        eid_resolver = getattr(self, "eid_resolver", None)
        if eid_resolver is not None:
            eid_resolver.stop()

        api = getattr(self, "api", None)
        if api is not None:
            # Safe to call close() now that it strictly un-refs the session
            # without killing the connection.
            await api.close()

    # --- NEW: Repairs + Auth state helpers ---------------------------------
    def _set_auth_state(self, *, failed: bool, reason: str | None = None) -> None:
        """State machine for authentication error transitions.

        - When entering the "failed" state:
            * create a Repairs issue (idempotent)
            * fire a domain-scoped HA event (EVENT_AUTH_ERROR)
            * set `auth_error_active = True` and store a short message
            * push updated data so diagnostic sensors can flip to `on`
        - When entering the "ok" state from "failed":
            * dismiss the Repairs issue
            * fire an OK event (EVENT_AUTH_OK)
            * set `auth_error_active = False` and clear the message
            * push updated data so diagnostic sensors can flip back to `off`
        """
        if failed and not self._auth_error_active:
            self._auth_error_active = True
            self._auth_error_since = time.time()
            self._auth_error_message = (reason or "Authentication failed").strip()
            # Repairs + event
            self._create_auth_issue()
            entry_id = self.config_entry.entry_id if self.config_entry else ""
            self.hass.bus.async_fire(
                EVENT_AUTH_ERROR,
                {
                    "entry_id": entry_id,
                    "email": self._get_account_email(),
                    "message": self._auth_error_message,
                },
            )
            # Notify listeners (binary_sensor etc.)
            try:
                self.async_set_updated_data(self.data)
            except Exception:
                pass
        elif not failed:
            issue_dismissed = self._dismiss_auth_issue()
            state_changed = False

            if self._auth_error_active:
                self._auth_error_active = False
                self._auth_error_message = None
                state_changed = True

            if issue_dismissed or state_changed:
                entry_id = self.config_entry.entry_id if self.config_entry else ""
                self.hass.bus.async_fire(
                    EVENT_AUTH_OK,
                    {
                        "entry_id": entry_id,
                        "email": self._get_account_email(),
                    },
                )
                try:
                    self.async_set_updated_data(self.data)
                except Exception:
                    pass

    @property
    def auth_error_active(self) -> bool:
        """Expose the current "auth failed" condition for diagnostic entities (binary_sensor)."""
        return self._auth_error_active

    # Former `_async_start_reauth_flow` helper removed: rely on HA's automatic
    # reauth trigger when `ConfigEntryAuthFailed` bubbles up.

    # --- END: Add/Replace inside Coordinator class --------------------------------

    # ---------------------------- Semantic mappings ------------------------
    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Return a float representation or ``None`` when conversion fails."""
        return _coerce_float_impl(value)

    def _find_semantic_match(
        self, raw_name: str, mapping: Mapping[str, dict[str, float]]
    ) -> dict[str, float] | None:
        """Return a semantic mapping entry using normalized matching."""

        normalized_name = raw_name.casefold().strip()
        if normalized_name.startswith("near "):
            normalized_name = normalized_name[len("near ") :].strip()

        normalized_mapping = {key.casefold(): value for key, value in mapping.items()}
        return normalized_mapping.get(normalized_name)

    def _apply_semantic_mapping(self, payload: dict[str, Any]) -> bool:
        """Substitute coordinates using user-defined semantic mappings when available."""

        semantic_name = payload.get("semantic_name")
        if not isinstance(semantic_name, str) or not semantic_name.strip():
            return False

        # Skip replayed semantic hints to preserve debounce behaviour when mappings are absent.
        if payload.get("is_replayed") is True or payload.get("replayed") is True:
            return False

        entry = getattr(self, "config_entry", None)
        if entry is None:
            return False

        raw_mappings = entry.options.get(OPT_SEMANTIC_LOCATIONS)
        if not raw_mappings:
            raw_mappings = entry.data.get(OPT_SEMANTIC_LOCATIONS)
        if not isinstance(raw_mappings, Mapping):
            return False

        normalized: dict[str, dict[str, float]] = {}
        for name, coords in raw_mappings.items():
            if not isinstance(name, str) or not isinstance(coords, Mapping):
                continue

            latitude = self._coerce_float(coords.get("latitude"))
            longitude = self._coerce_float(coords.get("longitude"))
            if latitude is None or longitude is None:
                continue

            accuracy = self._coerce_float(coords.get("accuracy"))
            if accuracy is None:
                accuracy = 0.0

            normalized[name.casefold()] = {
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": accuracy,
            }

        mapped = self._find_semantic_match(semantic_name, normalized)
        if mapped is None:
            return False

        payload["latitude"] = mapped["latitude"]
        payload["longitude"] = mapped["longitude"]
        payload["accuracy"] = mapped["accuracy"]
        payload["location_type"] = "trusted"

        return True

    def _record_semantic_label(
        self,
        payload: Mapping[str, Any],
        *,
        device_id: str | None = None,
    ) -> None:
        """Track observed semantic labels for diagnostics and UX helpers."""

        raw_name = payload.get("semantic_name")
        if not isinstance(raw_name, str):
            return

        semantic_name = raw_name.strip()
        if not semantic_name:
            return

        cache = getattr(self, "_semantic_label_cache", None)
        if cache is None:
            cache = {}
            self._semantic_label_cache = cache

        normalized = semantic_name.casefold()
        now = time.time()
        record = cache.get(normalized)
        if record is None:
            record = SemanticLabelRecord(
                label=semantic_name, first_seen=now, last_seen=now
            )
            cache[normalized] = record
        else:
            record.last_seen = now

        if device_id:
            record.devices.add(device_id)

    def get_observed_semantic_labels(self) -> list[SemanticLabelRecord]:
        """Return a stable snapshot of observed semantic labels."""

        cache = getattr(self, "_semantic_label_cache", None)
        if not cache:
            return []

        snapshot = [record.copy() for record in cache.values()]
        snapshot.sort(key=lambda item: item.label.casefold())
        return snapshot

    # ---------------------------- Ignore helpers ----------------------------
    @staticmethod
    def _normalize_identity_key(raw: object) -> bytes | None:
        """Normalize candidate identity key values into bytes."""

        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        if isinstance(raw, str):
            try:
                return bytes.fromhex(raw)
            except ValueError:
                # Silent failure: registry/API payloads are not always trustworthy.
                return None
        return None

    @staticmethod
    def _normalize_identity_key_candidates(raw: object) -> list[bytes]:
        """Normalize collections of candidate identity keys."""

        if raw is None:
            return []
        if isinstance(raw, (bytes, bytearray, str)):
            one = GoogleFindMyCoordinator._normalize_identity_key(raw)
            return [one] if one is not None else []
        if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, bytearray)):
            out: list[bytes] = []
            for item in raw:
                key = GoogleFindMyCoordinator._normalize_identity_key(item)
                if key is not None and key not in out:
                    out.append(key)
            return out
        return []

    @staticmethod
    def _normalize_optional_string(raw: object) -> str | None:
        """Return a stripped string when available, otherwise ``None``."""

        if isinstance(raw, str):
            value = raw.strip()
            return value or None
        return None

    @staticmethod
    def _normalize_encrypted_blob(raw: object) -> bytes | None:
        """Normalize encrypted payloads encoded as bytes or hex strings."""

        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        if isinstance(raw, str):
            try:
                return bytes.fromhex(raw)
            except ValueError:
                return None
        return None

    def _schedule_eid_resolver_refresh(self) -> None:
        """Refresh the global EID resolver when active device sets change."""

        hass = getattr(self, "hass", None)
        hass_data = getattr(hass, "data", None)
        if not isinstance(hass_data, dict):
            return

        bucket = hass_data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return

        resolver = bucket.get(DATA_EID_RESOLVER)
        refresh = getattr(resolver, "async_refresh", None)
        if callable(refresh):
            create_task = getattr(self.hass, "async_create_task", None)
            if callable(create_task):
                create_task(refresh())

    def get_active_device_identities(self) -> list[DeviceIdentity]:
        """Return identity keys for enabled, non-ignored devices.

        Devices disabled in the device registry or ignored via options are
        excluded from the returned list. Only devices currently eligible for
        polling (tracked in ``_enabled_poll_device_ids``) are considered for
        EID resolution. The returned ``registry_id`` refers to the Home
        Assistant Device Registry identifier for each tracker. Pairing and
        secrets-creation timestamps are forwarded when available to help the
        resolver reason about EID rotation windows, but the integration treats
        them as hypotheses because Google has not documented the server-side
        semantics. Any debug time anchor hints present in cached payloads are
        also forwarded best-effort for diagnostics.
        """
        # Use imported helpers from coordinator_identity.py instead of inline functions
        _normalize_device_type = _normalize_device_type_impl
        _normalize_fast_pair_model_id = _normalize_fast_pair_model_id_impl
        _lookup_prio = _lookup_prio_impl
        _lookup_prio_with_source = _lookup_prio_with_source_impl
        _store_if_value = _store_if_value_impl
        _normalize_timestamp = _normalize_identity_timestamp_impl
        _extract_pair_date = _extract_pair_date_impl
        _extract_secrets_creation_date = _extract_secrets_creation_date_impl
        _extract_time_anchors_debug = _extract_time_anchors_debug_impl

        _expected_identity_key_length = 32

        cached_owner_key: bytes | None = None
        cached_owner_key_version: int | None = None
        owner_key_checked = False

        def _get_cached_owner_key() -> tuple[bytes | None, int | None]:
            """Return the cached owner key when available without I/O."""

            nonlocal cached_owner_key, cached_owner_key_version, owner_key_checked

            if owner_key_checked:
                return cached_owner_key, cached_owner_key_version

            owner_key_checked = True

            cache_obj = getattr(self, "_cache", None)
            if not isinstance(cache_obj, TokenCache):
                return None, None

            cache_snapshot = getattr(cache_obj, "_data", None)
            if not isinstance(cache_snapshot, Mapping):
                return None, None

            username = cache_snapshot.get("username")
            if not isinstance(username, str) or not username:
                return None, None

            owner_entry = cache_snapshot.get(f"owner_key_{username}") or cache_snapshot.get(
                "owner_key"
            )

            raw_key: Any | None
            version: int | None = None
            if isinstance(owner_entry, Mapping):
                raw_key = owner_entry.get("key")
                version_value = owner_entry.get("version")
                version = version_value if isinstance(version_value, int) else None
            else:
                raw_key = owner_entry

            candidate: bytes | None = None
            if isinstance(raw_key, str):
                try:
                    candidate = bytes.fromhex(raw_key.strip())
                except ValueError:
                    candidate = None
            elif isinstance(raw_key, (bytes, bytearray)):
                candidate = bytes(raw_key)

            if candidate is None or len(candidate) != _expected_identity_key_length:
                return None, None

            cached_owner_key = candidate
            cached_owner_key_version = version
            return cached_owner_key, cached_owner_key_version

        def _decrypt_identity_key(
            ciphertext: bytes, canonical_id: str
        ) -> tuple[bytes | None, int | None]:
            """Attempt to decrypt the encrypted identity key using cached material.

            FIX: Differentiate between expected and unexpected decrypt errors (#132).
            Expected errors (key rotation, padding issues) are logged at DEBUG level.
            Unexpected errors are logged at WARNING and recorded in diagnostics.
            """

            owner_key, owner_version = _get_cached_owner_key()
            if owner_key is None:
                return None, None

            try:
                decrypted = decrypt_eik(owner_key, ciphertext)
            except Exception as err:  # noqa: BLE001 - defensive
                err_type = type(err).__name__

                # FIX: Use exception type matching instead of keyword search (#132)
                # Expected exceptions from decrypt_eik and underlying crypto:
                # - ValueError: invalid length, IV problems, key size issues
                # - InvalidTag: AES-GCM authentication failed (wrong key, corrupted data)
                # - InvalidKey: cryptography library key validation failure
                expected_exception_types = ("ValueError", "InvalidTag", "InvalidKey")
                is_expected = err_type in expected_exception_types

                if is_expected:
                    _LOGGER.debug(
                        "Identity key decryption failed for %s (%s): %s",
                        canonical_id,
                        err_type,
                        err,
                    )
                else:
                    # Unexpected error - log at WARNING and record in diagnostics
                    _LOGGER.warning(
                        "Unexpected decryption error for %s: %s (%s)",
                        canonical_id,
                        err_type,
                        err,
                    )
                    # Record in diagnostics buffer for troubleshooting
                    diag = getattr(self, "_diag", None)
                    if diag is not None:
                        diag.add_warning(
                            "decrypt_error",
                            {
                                "device_id": canonical_id,
                                "error_type": err_type,
                                "error_msg": str(err)[:100] if str(err) else "unknown",
                            },
                        )
                return None, None

            if not isinstance(decrypted, (bytes, bytearray)):
                return None, None

            key_bytes = bytes(decrypted)
            if len(key_bytes) != _expected_identity_key_length:
                _LOGGER.warning(
                    "Decrypted identity key for %s has invalid length: %s (expected %s)",
                    canonical_id,
                    len(key_bytes),
                    _expected_identity_key_length,
                )
                return None, None

            _LOGGER.debug("Successfully decrypted identity key for %s", canonical_id)
            return key_bytes, owner_version

        enabled_ids = set(self._enabled_poll_device_ids)
        ignored = self._get_ignored_set()
        entry = self.config_entry
        entry_id = entry.entry_id if entry is not None else None

        # =====================================================================
        # EID DATA SOURCES - Documentation for maintainers
        # =====================================================================
        # The EID resolver requires identity_key, pair_date, and secrets_creation_date
        # to generate Ephemeral Identifiers. These are sourced from (in priority order):
        #
        # 1. cache_* (from _device_location_data):
        #    - Populated by decrypt_locations via FCM push or manual locate
        #    - Contains decrypted identity_key from encryptedUserSecrets
        #    - Most authoritative source for recently-updated devices
        #
        # 2. data_* (from self.data - current API response):
        #    - Populated by _async_update_data polling cycle
        #    - Contains raw API payloads with encrypted_identity_key
        #
        # 3. last_* (from _last_device_list - nbe_list_devices):
        #    - Populated by async_get_basic_device_list()
        #    - Contains device metadata including encrypted_identity_key
        #    - Useful for devices that haven't received FCM updates yet
        #
        # 4. registry_* (DEPRECATED - was never functional):
        #    - Previously attempted to use DeviceRegistry custom_fields
        #    - custom_fields does NOT exist in Home Assistant's DeviceRegistry API
        #    - These dicts remain for API compatibility but are always empty
        #
        # The identity_key is typically obtained via:
        # - decrypt_locations (from FCM/LocateTracker responses)
        # - EID resolver's own decryption of encrypted_identity_key using owner_key
        # =====================================================================

        registry_map: dict[str, tuple[str, bytes | None]] = {}
        # These dicts were intended for DeviceRegistry persistence but custom_fields
        # does not exist in HA's API. They remain empty for backward compatibility.
        registry_pair_dates: dict[str, int] = {}
        registry_secrets_creation_dates: dict[str, int] = {}
        registry_time_anchors_debug: dict[str, Any] = {}

        if entry is not None:
            device_reg = dr.async_get(self.hass)
            extract_identifier = getattr(self, "_extract_our_identifier", None)
            for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
                if device.disabled_by is not None:
                    continue

                canonical_id: str | None = None
                if callable(extract_identifier):
                    canonical_id = extract_identifier(device)

                if not canonical_id:
                    _LOGGER.debug(
                        "Device registry entry %s skipped: No canonical identifier",
                        device.id,
                    )
                    continue

                if canonical_id in ignored:
                    continue

                # Map canonical_id -> (device.id, identity_key)
                # Note: identity_key from DeviceRegistry is always None because
                # custom_fields does not exist in HA's DeviceRegistry API.
                # The actual identity_key comes from cache_identities, data_identities,
                # or last_identities (populated from API responses and FCM).
                registry_map[canonical_id] = (device.id, None)

        device_ids_set = {dev_id for dev_id in enabled_ids if dev_id not in ignored}
        device_ids_set.update(registry_map)
        device_ids = sorted(device_ids_set)
        if not device_ids:
            return []

        _LOGGER.debug(
            "Collecting EID identities for %d eligible devices (polling=%d, registry=%d)",
            len(device_ids),
            len(enabled_ids),
            len(registry_map),
        )

        allowed_raw_ids = {did.split(":")[-1] for did in device_ids}
        registry_ids: set[str] = {registry for registry, _ in registry_map.values()}
        allowed_payload_ids = device_ids_set | allowed_raw_ids | registry_ids

        last_device_list = getattr(self, "_last_device_list", None)
        last_identities: dict[str, bytes] = {}
        last_encrypted: dict[str, tuple[bytes | None, int | None]] = {}
        last_device_types: dict[str, int | None] = {}
        last_fast_pair_model_ids: dict[str, str | None] = {}
        last_raw_keys: dict[str, list[str]] = {}
        last_identity_candidates: dict[str, list[bytes]] = {}
        last_pair_dates: dict[str, int] = {}
        last_secrets_creation_dates: dict[str, int] = {}
        last_time_anchors_debug: dict[str, Any] = {}
        last_payloads: dict[str, Mapping[str, Any]] = {}
        if isinstance(last_device_list, Iterable):
            for raw in last_device_list:
                if not isinstance(raw, dict):
                    continue
                dev_id = raw.get("id")
                if not isinstance(dev_id, str):
                    continue
                if dev_id not in allowed_payload_ids:
                    continue

                last_payloads[dev_id] = raw
                last_raw_keys[dev_id] = list(raw.keys())
                parsed = self._normalize_identity_key(
                    raw.get("identity_key") or raw.get("identityKey") or raw.get("eik")
                )
                _store_if_value(last_identities, dev_id, parsed)

                candidate_list = self._normalize_identity_key_candidates(
                    raw.get("identity_key_candidates")
                    or raw.get("identityKeyCandidates")
                )
                if candidate_list:
                    last_identity_candidates[dev_id] = candidate_list

                encrypted_identity = self._normalize_identity_key(
                    raw.get("encrypted_identity_key") or raw.get("encryptedIdentityKey")
                )
                owner_key_version = raw.get("owner_key_version")
                if isinstance(owner_key_version, str) and owner_key_version.isdigit():
                    owner_key_version = int(owner_key_version)
                elif not isinstance(owner_key_version, int):
                    owner_key_version = None

                if encrypted_identity is not None or owner_key_version is not None:
                    last_encrypted[dev_id] = (encrypted_identity, owner_key_version)

                device_type = _normalize_device_type(raw.get("device_type"))
                if device_type is not None:
                    last_device_types[dev_id] = device_type

                fast_pair_model_id = _normalize_fast_pair_model_id(
                    raw.get("fast_pair_model_id") or raw.get("fastPairModelId")
                )
                if fast_pair_model_id is not None:
                    last_fast_pair_model_ids[dev_id] = fast_pair_model_id

                pair_date = _extract_pair_date(raw)
                _store_if_value(last_pair_dates, dev_id, pair_date)

                secrets_creation_date = _extract_secrets_creation_date(raw)
                _store_if_value(
                    last_secrets_creation_dates, dev_id, secrets_creation_date
                )

                anchors_debug = _extract_time_anchors_debug(raw)
                if anchors_debug is not None:
                    last_time_anchors_debug[dev_id] = anchors_debug

        data_identities: dict[str, bytes] = {}
        data_encrypted: dict[str, tuple[bytes | None, int | None]] = {}
        data_device_types: dict[str, int | None] = {}
        data_fast_pair_model_ids: dict[str, str | None] = {}
        raw_data_keys: dict[str, list[str]] = {}
        data_identity_candidates: dict[str, list[bytes]] = {}
        data_pair_dates: dict[str, int] = {}
        data_secrets_creation_dates: dict[str, int] = {}
        data_time_anchors_debug: dict[str, Any] = {}
        data_payloads: dict[str, Mapping[str, Any]] = {}
        device_data = getattr(self, "data", None)
        if isinstance(device_data, Iterable):
            for raw in device_data:
                if not isinstance(raw, dict):
                    continue
                dev_id = raw.get("id")
                if not isinstance(dev_id, str):
                    continue
                if dev_id not in allowed_payload_ids:
                    continue
                data_payloads[dev_id] = raw
                raw_data_keys[dev_id] = list(raw.keys())
                parsed = self._normalize_identity_key(
                    raw.get("identity_key") or raw.get("identityKey") or raw.get("eik")
                )
                _store_if_value(data_identities, dev_id, parsed)

                candidate_list = self._normalize_identity_key_candidates(
                    raw.get("identity_key_candidates")
                    or raw.get("identityKeyCandidates")
                )
                if candidate_list:
                    data_identity_candidates[dev_id] = candidate_list

                encrypted_identity = self._normalize_identity_key(
                    raw.get("encrypted_identity_key") or raw.get("encryptedIdentityKey")
                )
                owner_key_version = raw.get("owner_key_version")
                if isinstance(owner_key_version, str) and owner_key_version.isdigit():
                    owner_key_version = int(owner_key_version)
                elif not isinstance(owner_key_version, int):
                    owner_key_version = None

                if encrypted_identity is not None or owner_key_version is not None:
                    data_encrypted[dev_id] = (encrypted_identity, owner_key_version)

                device_type = _normalize_device_type(raw.get("device_type"))
                if device_type is not None:
                    data_device_types[dev_id] = device_type

                fast_pair_model_id = _normalize_fast_pair_model_id(
                    raw.get("fast_pair_model_id") or raw.get("fastPairModelId")
                )
                if fast_pair_model_id is not None:
                    data_fast_pair_model_ids[dev_id] = fast_pair_model_id

                pair_date = _extract_pair_date(raw)
                _store_if_value(data_pair_dates, dev_id, pair_date)

                secrets_creation_date = _extract_secrets_creation_date(raw)
                _store_if_value(
                    data_secrets_creation_dates, dev_id, secrets_creation_date
                )

                anchors_debug = _extract_time_anchors_debug(raw)
                if anchors_debug is not None:
                    data_time_anchors_debug[dev_id] = anchors_debug

        cache = getattr(self, "_device_location_data", None)
        internal_cache: dict[str, Any] = cache if isinstance(cache, dict) else {}
        cache_identities: dict[str, bytes] = {}
        cache_encrypted: dict[str, tuple[bytes | None, int | None]] = {}
        cache_device_types: dict[str, int | None] = {}
        cache_fast_pair_model_ids: dict[str, str | None] = {}
        cache_data_keys: dict[str, list[str]] = {}
        cache_identity_candidates: dict[str, list[bytes]] = {}
        cache_pair_dates: dict[str, int] = {}
        cache_secrets_creation_dates: dict[str, int] = {}
        cache_time_anchors_debug: dict[str, Any] = {}
        if internal_cache:
            allowed_cache_keys = set(device_ids) | allowed_raw_ids | registry_ids
            for dev_id, payload in internal_cache.items():
                if dev_id not in allowed_cache_keys:
                    continue
                if not isinstance(payload, dict):
                    continue
                cache_data_keys[dev_id] = list(payload.keys())
                parsed = self._normalize_identity_key(
                    payload.get("identity_key")
                    or payload.get("identityKey")
                    or payload.get("eik")
                )
                _store_if_value(cache_identities, dev_id, parsed)

                candidate_list = self._normalize_identity_key_candidates(
                    payload.get("identity_key_candidates")
                    or payload.get("identityKeyCandidates")
                )
                if candidate_list:
                    cache_identity_candidates[dev_id] = candidate_list

                encrypted_identity = self._normalize_identity_key(
                    payload.get("encrypted_identity_key")
                    or payload.get("encryptedIdentityKey")
                )
                owner_key_version = payload.get("owner_key_version")
                if isinstance(owner_key_version, str) and owner_key_version.isdigit():
                    owner_key_version = int(owner_key_version)
                elif not isinstance(owner_key_version, int):
                    owner_key_version = None

                if encrypted_identity is not None or owner_key_version is not None:
                    cache_encrypted[dev_id] = (encrypted_identity, owner_key_version)

                device_type = _normalize_device_type(payload.get("device_type"))
                if device_type is not None:
                    cache_device_types[dev_id] = device_type

                fast_pair_model_id = _normalize_fast_pair_model_id(
                    payload.get("fast_pair_model_id") or payload.get("fastPairModelId")
                )
                if fast_pair_model_id is not None:
                    cache_fast_pair_model_ids[dev_id] = fast_pair_model_id

                pair_date = _extract_pair_date(payload)
                _store_if_value(cache_pair_dates, dev_id, pair_date)

                secrets_creation_date = _extract_secrets_creation_date(payload)
                _store_if_value(
                    cache_secrets_creation_dates, dev_id, secrets_creation_date
                )

                anchors_debug = _extract_time_anchors_debug(payload)
                if anchors_debug is not None:
                    cache_time_anchors_debug[dev_id] = anchors_debug

        identities: list[DeviceIdentity] = []
        for canonical_id in device_ids:
            lookup_id = canonical_id.split(":")[-1]
            registry_entry = registry_map.get(canonical_id)
            if registry_entry is None:
                continue

            registry_id, registry_key = registry_entry

            cache_lookup: Mapping[str, Any] | None = (
                cache if isinstance(cache, Mapping) else None
            )
            cache_keys = (canonical_id, lookup_id, registry_id)
            merged_device_data: dict[str, Any] = {}

            for source in (
                cache_lookup,
                internal_cache,
                last_payloads,
                data_payloads,
            ):
                if not isinstance(source, Mapping):
                    continue
                for key in cache_keys:
                    if key is None or key not in source:
                        continue
                    payload = source.get(key)
                    if isinstance(payload, Mapping):
                        _update_preserve_metadata(merged_device_data, payload)

            _LOGGER.debug(
                "Building Identity for %s: cached_data=%s", canonical_id, merged_device_data
            )

            direct_pair_date = _extract_pair_date(merged_device_data)
            direct_secrets_date = _extract_secrets_creation_date(merged_device_data)

            lookup_keys = (canonical_id, lookup_id, registry_id)

            identity_key = _lookup_prio(
                lookup_keys, cache_identities, data_identities, last_identities
            )
            if identity_key is None:
                identity_key = registry_key

            identity_candidates = _lookup_prio(
                lookup_keys,
                cache_identity_candidates,
                data_identity_candidates,
                last_identity_candidates,
            )
            if identity_candidates is None:
                identity_candidates = []

            encrypted_identity_tuple = _lookup_prio(
                lookup_keys, cache_encrypted, data_encrypted, last_encrypted
            )
            encrypted_identity_key, owner_key_version = encrypted_identity_tuple or (
                None,
                None,
            )
            device_type = _lookup_prio(
                lookup_keys, cache_device_types, data_device_types, last_device_types
            )

            fast_pair_model_id = _lookup_prio(
                lookup_keys,
                cache_fast_pair_model_ids,
                data_fast_pair_model_ids,
                last_fast_pair_model_ids,
            )

            pair_date_source: str | None = None
            pair_date_raw, pair_date_source = _lookup_prio_with_source(
                lookup_keys,
                (cache_pair_dates, "cache"),
                (data_pair_dates, "live"),
                (last_pair_dates, "last"),
            )
            if pair_date_raw is None:
                registry_pair_date, registry_pair_source = _lookup_prio_with_source(
                    lookup_keys, (registry_pair_dates, "registry")
                )
                if registry_pair_date is not None:
                    pair_date_raw = registry_pair_date
                    pair_date_source = registry_pair_source
            if pair_date_raw is None:
                pair_date_raw = direct_pair_date
                if pair_date_raw is not None:
                    pair_date_source = "merged"
            pair_date = normalize_epoch_seconds(pair_date_raw)
            if pair_date is None:
                pair_date_source = None

            secrets_creation_source: str | None = None
            secrets_creation_raw, secrets_creation_source = _lookup_prio_with_source(
                lookup_keys,
                (cache_secrets_creation_dates, "cache"),
                (data_secrets_creation_dates, "live"),
                (last_secrets_creation_dates, "last"),
            )
            if secrets_creation_raw is None:
                registry_secrets_date, registry_secrets_source = _lookup_prio_with_source(
                    lookup_keys, (registry_secrets_creation_dates, "registry")
                )
                if registry_secrets_date is not None:
                    secrets_creation_raw = registry_secrets_date
                    secrets_creation_source = registry_secrets_source
            if secrets_creation_raw is None:
                secrets_creation_raw = direct_secrets_date
                if secrets_creation_raw is not None:
                    secrets_creation_source = "merged"
            secrets_creation_date = normalize_epoch_seconds(secrets_creation_raw)
            if secrets_creation_date is None:
                secrets_creation_source = None

            # Anchor fallback: use secrets_creation_date as pair_date when pair_date
            # is missing or invalid (0). This is common for Android phones that lack
            # deviceRegistration data but have valid encrypted_user_secrets bundles.
            if (pair_date is None or pair_date <= 0) and (
                secrets_creation_date is not None and secrets_creation_date > 0
            ):
                _LOGGER.debug(
                    "Anchor fallback for %s: pair_date=%s invalid, "
                    "using secrets_creation_date=%s as pair_date",
                    canonical_id,
                    pair_date,
                    secrets_creation_date,
                )
                pair_date = secrets_creation_date
                pair_date_source = f"fallback:{secrets_creation_source or 'secrets'}"

            anchors_debug = _lookup_prio(
                lookup_keys,
                registry_time_anchors_debug,
                cache_time_anchors_debug,
                data_time_anchors_debug,
                last_time_anchors_debug,
            )

            manufacturer = self._normalize_optional_string(
                merged_device_data.get("manufacturer")
            )
            model = self._normalize_optional_string(merged_device_data.get("model"))
            encrypted_account_key = self._normalize_encrypted_blob(
                merged_device_data.get("encrypted_account_key")
                or merged_device_data.get("encryptedAccountKey")
            )
            public_key_address = self._normalize_encrypted_blob(
                merged_device_data.get("public_key_address")
                or merged_device_data.get("encryptedSha256AccountKeyPublicAddress")
            )

            if not identity_candidates and identity_key is not None:
                identity_candidates = [identity_key]
            elif not identity_candidates and registry_key is not None:
                identity_candidates = [registry_key]

            normalized_candidates: list[bytes] = []
            for candidate in identity_candidates:
                normalized_key = self._normalize_identity_key(candidate)
                if (
                    normalized_key is not None
                    and normalized_key not in normalized_candidates
                ):
                    normalized_candidates.append(normalized_key)

            # Normalize identity_key before length validation to handle hex strings
            if identity_key is not None:
                normalized_identity_key = self._normalize_identity_key(identity_key)
                if normalized_identity_key is None:
                    _LOGGER.debug(
                        "Could not normalize identity key for %s",
                        canonical_id,
                    )
                    identity_key = None
                elif len(normalized_identity_key) != _expected_identity_key_length:
                    _LOGGER.debug(
                        "Discarding identity key with invalid length %s for %s",
                        len(normalized_identity_key),
                        canonical_id,
                    )
                    identity_key = None
                else:
                    # Use the normalized bytes version
                    identity_key = normalized_identity_key

            decrypted_owner_version: int | None = None
            if (
                identity_key is None
                and isinstance(encrypted_identity_key, (bytes, bytearray))
            ):
                decrypted_identity_key, decrypted_owner_version = _decrypt_identity_key(
                    encrypted_identity_key, canonical_id
                )
                if decrypted_identity_key is not None:
                    identity_key = decrypted_identity_key
                    if owner_key_version is None:
                        owner_key_version = decrypted_owner_version
                    if not identity_candidates:
                        identity_candidates = [identity_key]

            effective_identity_for_log = identity_key
            if effective_identity_for_log is None and normalized_candidates:
                effective_identity_for_log = normalized_candidates[0]
            has_key = bool(normalized_candidates) or identity_key is not None
            has_key = has_key or encrypted_identity_key is not None
            if not has_key:
                _LOGGER.debug(
                    "Missing crypto material for %s: key=%s (pair=%s). Skipping resolution.",
                    canonical_id,
                    effective_identity_for_log,
                    pair_date,
                )
                continue

            if not normalized_candidates and encrypted_identity_key is None:
                _LOGGER.debug(
                    "Device %s skipped: No identity key found (Data: %s)",
                    canonical_id,
                    last_raw_keys.get(lookup_id)
                    or raw_data_keys.get(lookup_id)
                    or cache_data_keys.get(lookup_id)
                    or [],
                )
                continue

            _LOGGER.debug(
                "Resolving Identity for %s: Anchor=%s (source=%s), PairDate=%s (source=%s)",
                canonical_id,
                secrets_creation_date,
                (secrets_creation_source or "unknown").upper(),
                pair_date,
                (pair_date_source or "unknown").upper(),
            )

            if normalized_candidates:
                for candidate in normalized_candidates:
                    identities.append(
                        DeviceIdentity(
                            registry_id=registry_id,
                            canonical_id=canonical_id,
                            identity_key=candidate,
                            encrypted_identity_key=encrypted_identity_key,
                            owner_key_version=owner_key_version,
                            device_type=device_type,
                            config_entry_id=entry_id,
                            fast_pair_model_id=fast_pair_model_id,
                            manufacturer=manufacturer,
                            model=model,
                            pair_date=pair_date,
                            secrets_creation_date=secrets_creation_date,
                            encrypted_account_key=encrypted_account_key,
                            public_key_address=public_key_address,
                            time_anchors_debug=anchors_debug,
                        )
                    )
            else:
                identities.append(
                    DeviceIdentity(
                        registry_id=registry_id,
                        canonical_id=canonical_id,
                        identity_key=identity_key,
                        encrypted_identity_key=encrypted_identity_key,
                        owner_key_version=owner_key_version,
                        device_type=device_type,
                        config_entry_id=entry_id,
                        fast_pair_model_id=fast_pair_model_id,
                        manufacturer=manufacturer,
                        model=model,
                        pair_date=pair_date,
                        secrets_creation_date=secrets_creation_date,
                        encrypted_account_key=encrypted_account_key,
                        public_key_address=public_key_address,
                        time_anchors_debug=anchors_debug,
                    )
                )

        _LOGGER.debug("Returning %d identities to resolver", len(identities))
        return identities

    def _get_ignored_set(self) -> set[str]:
        """Return the set of device IDs the user chose to ignore (options-first).

        Notes:
            - Uses config_entry.options if available; falls back to an attribute
              'ignored_devices' when set through update_settings().
            - Intentionally simple equality (no normalization) to avoid surprises.
        """
        try:
            entry = self.config_entry
            if entry is not None:
                raw = entry.options.get(
                    OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES, {})
                )
                # Accept list[str] (legacy) or mapping (current)
                mapping, _migrated = coerce_ignored_mapping(raw)
                if mapping:
                    return set(mapping.keys())
        except Exception:  # defensive
            pass
        raw_attr = getattr(self, "ignored_devices", None)
        if isinstance(raw_attr, list):
            return {x for x in raw_attr if isinstance(x, str)}
        return set()

    def is_ignored(self, device_id: str) -> bool:
        """Return True if the device is currently ignored by user choice."""
        return device_id in self._get_ignored_set()

    # Public read-only state for diagnostics/UI
    @property
    def is_polling(self) -> bool:
        """Expose current polling state (public read-only API).

        Returns:
            True if a polling cycle is currently in progress.
        """
        return self._is_polling

    # ---------------------------- Metrics & errors helpers ------------------
    def safe_update_metric(self, key: str, value: float) -> None:
        """Safely set a numeric performance metric (float-coerced)."""
        try:
            self.performance_metrics[str(key)] = float(value)
        except Exception:
            # Never raise from diagnostics helpers
            pass

    def _short_error_message(self, exc: Exception | str) -> str:
        """Return a compact, single-line error string."""
        return _short_error_message_impl(exc)

    def _append_recent_error(self, err_type: str, message: str) -> None:
        """Append a (timestamp, type, message) triple to the bounded deque."""
        try:
            self.recent_errors.append(
                (time.time(), err_type, self._short_error_message(message))
            )
        except Exception:
            pass

    def note_error(
        self, exc: Exception, *, where: str = "", device: str | None = None
    ) -> None:
        """Public helper to record non-fatal errors with minimal context."""
        prefix = where or "coordinator"
        if device:
            prefix += f"({device})"
        err_type = type(exc).__name__
        self._append_recent_error(err_type, f"{prefix}: {exc}")

    # Safe getters for durations based on keys that __init__.py may set.
    def get_metric(self, key: str) -> float | None:
        val = self.performance_metrics.get(key)
        return float(val) if isinstance(val, (int, float)) else None

    def _get_duration(self, start_key: str, end_key: str) -> float | None:
        """Calculate duration between two metric keys."""
        return _get_duration_impl(self.get_metric(start_key), self.get_metric(end_key))

    def get_setup_duration_seconds(self) -> float | None:
        """Duration between 'setup_start_monotonic' and 'setup_end_monotonic'."""
        return self._get_duration("setup_start_monotonic", "setup_end_monotonic")

    def get_fcm_acquire_duration_seconds(self) -> float | None:
        """Duration between 'setup_start_monotonic' and 'fcm_acquired_monotonic'."""
        return _get_duration_impl(
            self.get_metric("setup_start_monotonic"),
            self.get_metric("fcm_acquired_monotonic"),
        )

    def get_last_poll_duration_seconds(self) -> float | None:
        """Duration of the most recent sequential polling cycle (if recorded)."""
        return self._get_duration("last_poll_start_mono", "last_poll_end_mono")

    def get_recent_errors(self) -> list[dict[str, Any]]:
        """Return a JSON-friendly copy of recent error triples."""
        return format_recent_errors(self.recent_errors)

    # ---------------------------- HA Coordinator ----------------------------
    def _is_fcm_ready_soft(self) -> bool:
        """Return True if push transport appears ready (no awaits, no I/O).

        Priority order:
          1) Ask API (single source of truth if available).
          2) Receiver-level booleans.
          3) Push-client heuristic (run_state + do_listen).
        """
        try:
            # 1) API knowledge (preferred)
            try:
                fn = getattr(self.api, "is_push_ready", None)
                if callable(fn):
                    return bool(fn())
            except Exception:
                pass

            # 2) Receiver-level flags
            fcm = self.hass.data.get(DOMAIN, {}).get("fcm_receiver")
            if not fcm:
                return False
            fatal_error: str | None = getattr(fcm, "_fatal_error", None)
            entry_id = self._entry_id()
            fatal_by_entry = getattr(fcm, "_fatal_errors", None)
            if isinstance(fatal_by_entry, Mapping) and entry_id:
                fatal_error = fatal_by_entry.get(entry_id) or fatal_error
            if isinstance(fatal_error, str) and fatal_error:
                return False
            for attr in ("is_ready", "ready"):
                val = getattr(fcm, attr, None)
                if isinstance(val, bool):
                    return val

            # 3) Heuristic: push client state (no enum import)
            pc = getattr(fcm, "pc", None)
            if pc is not None:
                state = getattr(pc, "run_state", None)
                state_name = getattr(state, "name", state)
                if state_name == "STARTED" and bool(getattr(pc, "do_listen", False)):
                    return True

            return False
        except Exception:
            return False

    def _note_fcm_deferral(self, now_mono: float) -> None:
        """Advance a quiet escalation timeline while FCM is not ready.

        FIX: Use less aggressive log levels to reduce log spam (#124).
        Emits at most:
            - one INFO after ~2 minutes (was WARNING after 60s)
            - one WARNING after ~5 minutes (was ERROR after 300s)
        Resets when readiness returns.
        """
        if self._fcm_defer_started_mono == 0.0:
            self._fcm_defer_started_mono = now_mono
            self._fcm_last_stage = 0
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport not ready; awaiting connection",
            )
            return
        elapsed = now_mono - self._fcm_defer_started_mono
        # Stage 1: After 2 minutes, log at INFO level (not WARNING)
        if elapsed >= 120 and self._fcm_last_stage < 1:
            self._fcm_last_stage = 1
            _LOGGER.info(
                "Push transport connection taking longer than expected (2 min). "
                "Push updates may be delayed, but polling continues."
            )
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport waiting for connection (2 min elapsed)",
            )
        # Stage 2: After 5 minutes, log at WARNING level (not ERROR)
        if elapsed >= 300 and self._fcm_last_stage < 2:
            self._fcm_last_stage = 2
            _LOGGER.warning(
                "Push transport not connected after 5 minutes. "
                "Check network connectivity and credentials if this persists."
            )
            self._set_fcm_status(
                FcmStatus.DISCONNECTED,
                reason="Push transport not connected after prolonged wait",
            )

    def _clear_fcm_deferral(self) -> None:
        """Clear the escalation timeline once FCM becomes ready (log once)."""
        if self._fcm_defer_started_mono:
            _LOGGER.info("FCM/push is ready; resuming scheduled polling.")
        self._fcm_defer_started_mono = 0.0
        self._fcm_last_stage = 0
        self._set_fcm_status(FcmStatus.CONNECTED)

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        Discovery semantics:
        - Refresh the **full** lightweight device list (no executor) on a paced interval
          and reuse the cached list between refreshes.
        - Update presence and metadata caches for **all** devices.
        - The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
        - The sequential **polling cycle** polls devices that are enabled in HA's Device Registry
          **for this config entry** and not explicitly ignored in integration options.

        Returns:
            A list of dictionaries, where each dictionary represents a device's state.

        Raises:
            ConfigEntryAuthFailed: If authentication fails during device list fetching.
            UpdateFailed: For other transient or unexpected errors.
        """
        try:
            # Check for fatal FCM errors (for example, 404/401 during registration) to trigger re-auth
            # FIX: Only trigger re-auth after multiple consecutive failures (#114)
            entry = self.config_entry
            runtime = getattr(entry, "runtime_data", None)
            fcm_receiver = getattr(runtime, "fcm_receiver", None)

            fatal_error: str | None = None
            if fcm_receiver is not None:
                fatal_by_entry = getattr(fcm_receiver, "_fatal_errors", None)
                entry_id = self._entry_id()

                if isinstance(fatal_by_entry, Mapping) and entry_id:
                    fatal_error = fatal_by_entry.get(entry_id)

            if isinstance(fatal_error, str) and fatal_error:
                # Check if this is a fatal auth error that should fail immediately
                # (e.g., 404/401 with credentials issues - no point retrying)
                is_fatal_auth = _is_fatal_fcm_auth_error_impl(fatal_error)

                if is_fatal_auth:
                    # Fatal auth errors - fail immediately, no retry
                    _LOGGER.error(
                        "Fatal FCM authentication error: %s. Triggering re-authentication.",
                        fatal_error,
                    )
                    self._set_auth_state(failed=True, reason=fatal_error)
                    raise ConfigEntryAuthFailed(fatal_error)

                # Track consecutive FCM errors for transient issues
                if fatal_error != self._fcm_last_error:
                    # New error type, reset counter
                    self._fcm_error_count = 1
                    self._fcm_last_error = fatal_error
                else:
                    self._fcm_error_count += 1

                # Only trigger re-auth after _FCM_ERROR_RETRY_THRESHOLD consecutive failures
                if self._fcm_error_count >= _FCM_ERROR_RETRY_THRESHOLD:
                    _LOGGER.error(
                        "FCM error persisted after %d attempts: %s. Triggering re-authentication.",
                        self._fcm_error_count,
                        fatal_error,
                    )
                    self._set_auth_state(failed=True, reason=fatal_error)
                    raise ConfigEntryAuthFailed(fatal_error)
                else:
                    _LOGGER.warning(
                        "FCM error detected (%d/%d): %s. Will retry before triggering re-auth.",
                        self._fcm_error_count,
                        _FCM_ERROR_RETRY_THRESHOLD,
                        fatal_error,
                    )
            # No error - reset counter on successful check
            elif self._fcm_error_count > 0:
                _LOGGER.debug("FCM error cleared after %d attempts", self._fcm_error_count)
                self._fcm_error_count = 0
                self._fcm_last_error = None

            # One-time wait for FCM on first run.
            # FIX: Better user feedback during FCM initialization (#116)
            if not self._startup_complete:
                fcm_evt = getattr(self, "fcm_ready_event", None)
                if isinstance(fcm_evt, asyncio.Event) and not fcm_evt.is_set():
                    _LOGGER.info(
                        "Waiting for push notification service (FCM)... "
                        "This may take up to 15 seconds on first start."
                    )
                    try:
                        # Wait in 5-second increments with progress logging
                        for attempt in range(3):
                            try:
                                await asyncio.wait_for(fcm_evt.wait(), timeout=5.0)
                                _LOGGER.info("Push notification service is ready.")
                                break
                            except TimeoutError:
                                if attempt < 2:
                                    _LOGGER.debug(
                                        "FCM not ready yet, waiting... (%d/3)",
                                        attempt + 1,
                                    )
                        else:
                            # All 3 attempts exhausted
                            _LOGGER.warning(
                                "Push notification service not ready after 15s; "
                                "continuing in degraded mode. Push updates may be delayed."
                            )
                    except Exception as fcm_wait_err:
                        _LOGGER.debug("FCM wait error: %s", fcm_wait_err)
                self._startup_complete = True

            if self._is_fcm_ready_soft():
                self._set_fcm_status(FcmStatus.CONNECTED)
            elif self._fcm_last_stage < 2:
                self._set_fcm_status(
                    FcmStatus.DEGRADED,
                    reason="Push transport not ready; continuing with cached data",
                )

            list_check_mono = time.monotonic()
            list_due = (
                list_check_mono - self._last_list_poll_mono
            ) >= DEVICE_LIST_POLL_INTERVAL
            force_list_refresh = self._force_device_list_refresh
            if force_list_refresh:
                self._force_device_list_refresh = False
                force_reason = self._force_device_list_reason
                self._force_device_list_reason = None
                use_cached_list = False
                self._last_list_poll_mono = 0.0
                _LOGGER.debug(
                    "[%s] Device list refresh forced%s",
                    self._entry_id() or "unknown",
                    f" ({force_reason})" if force_reason else "",
                )
            else:
                use_cached_list = not list_due and bool(self._last_device_list)

            filtered_devices: list[dict[str, Any]]
            if use_cached_list:
                filtered_devices = list(self._last_device_list)
                self._set_api_status(ApiStatus.OK)
                _LOGGER.debug(
                    "Skipping device list refresh (next in %.0fs); using cached list (%d devices)",
                    max(
                        0.0,
                        DEVICE_LIST_POLL_INTERVAL
                        - (list_check_mono - self._last_list_poll_mono),
                    ),
                    len(filtered_devices),
                )
            else:
                # 1) Fetch the lightweight FULL device list using the native async API
                payload = await self.api.async_get_basic_device_list()

                # Success path: if we were in an auth error state, clear it now.
                self._set_auth_state(failed=False)
                self._set_api_status(ApiStatus.OK)

                # Normalize payloads and filter/dedupe devices using pure helpers
                device_candidates = _normalize_device_list_impl(payload)
                filtered_devices, seen_ids = _filter_and_dedupe_impl(device_candidates)

                # Log skipped devices for debugging (pure helper doesn't log)
                _logged_ids: set[str] = set()
                for candidate in device_candidates:
                    if not isinstance(candidate, Mapping):
                        continue
                    dev_id_raw = candidate.get("id")
                    if not isinstance(dev_id_raw, str) or not dev_id_raw.strip():
                        _LOGGER.debug("Skipping device without valid id: %r", candidate)
                        continue
                    dev_id = dev_id_raw.strip()
                    if dev_id in _logged_ids:
                        _LOGGER.debug(
                            "Skipping duplicate device entry for id=%s", dev_id
                        )
                    _logged_ids.add(dev_id)

                # Minimal hardening against false empties (keep prior behaviour)
                if not filtered_devices:
                    self._empty_list_streak += 1
                    # Use helper to decide if we should defer the empty list
                    if _should_defer_empty_list_impl(
                        self._empty_list_streak,
                        _EMPTY_LIST_QUORUM,
                        bool(self._last_device_list),
                    ):
                        # Defer clearing once; keep previous view stable.
                        _LOGGER.debug(
                            "Successful empty device list received (%d/%d). Deferring clear until quorum is met.",
                            self._empty_list_streak,
                            _EMPTY_LIST_QUORUM,
                        )
                        filtered_devices = list(self._last_device_list)
                    else:
                        _LOGGER.debug(
                            "Accepting empty device list after %d consecutive empties.",
                            self._empty_list_streak,
                        )
                        # Once accepted, forget any prior list so snapshot becomes empty below.
                        self._last_device_list = []
                        self._last_list_poll_mono = list_check_mono
                else:
                    # Non-empty result: reset streak and remember latest good list.
                    self._empty_list_streak = 0
                    self._last_device_list = list(filtered_devices)
                    self._last_list_poll_mono = list_check_mono

            # Presence TTL derives from the effective poll cadence
            effective_interval = max(
                self.location_poll_interval, self.min_poll_interval
            )
            self._presence_ttl_s = _calculate_presence_ttl_impl(effective_interval, 120)
            now_mono = time.monotonic()

            predicted_target = self._get_predicted_poll_time()
            predictive_due = False
            predictive_block = False
            if predicted_target is not None:
                wall_now = time.time()
                time_until = predicted_target - wall_now

                if 0 < time_until <= effective_interval:
                    delay = time_until + _PREDICTION_BUFFER_S
                    _LOGGER.debug(
                        "Predictive polling: update expected in %.1fs; scheduling retry in %.1fs (buffer=%ss)",
                        time_until,
                        delay,
                        _PREDICTION_BUFFER_S,
                    )
                    self._schedule_short_retry(delay)
                    predictive_block = True
                elif time_until <= 0:
                    _LOGGER.debug(
                        "Predictive polling: update overdue by %.1fs; polling now if limits allow.",
                        abs(time_until),
                    )
                    predictive_due = True

            # Cold-start guard: if the very first seen list is empty, treat it as transient
            if not filtered_devices and self._last_nonempty_wall == 0.0:
                raise UpdateFailed(
                    "Cold start: empty device list; treating as transient."
                )

            # Maintain owner index for FCM fallback routing (entry-scoped).
            self._sync_owner_index(filtered_devices)

            ignored = self._get_ignored_set()

            # Record presence timestamps from the full list (unfiltered by ignore)
            if filtered_devices:
                for dev in filtered_devices:
                    dev_id = dev["id"]
                    self._present_last_seen[dev_id] = now_mono
                # Keep a diagnostics-only set mirroring the latest non-empty list
                self._present_device_ids = {dev["id"] for dev in filtered_devices}
                self._last_nonempty_wall = now_mono
            # If the list is empty, leave _present_last_seen untouched; TTL will decide availability.

            # 2) Update internal name/capability caches for ALL devices
            name_cache = self._ensure_device_name_cache()
            for dev in filtered_devices:
                dev_id = dev["id"]
                raw_name = dev.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name_cache[dev_id] = raw_name
                elif dev_id not in name_cache:
                    name_cache[dev_id] = dev_id

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 2.2) Seed the location cache with list-provided coordinates when available
            for dev in filtered_devices:
                dev_id = dev["id"]
                if dev_id in ignored:
                    continue

                lat = dev.get("latitude")
                lon = dev.get("longitude")
                if lat is None or lon is None:
                    continue

                if not self._normalize_coords(dev, device_label=dev_id):
                    continue

                cache_seed = dict(dev)

                # Avoid poisoning the cache with explicit None accuracy values so
                # stationary clamps do not overwrite fresher accuracy readings.
                if cache_seed.get("accuracy") is None:
                    cache_seed.pop("accuracy", None)

                self.update_device_cache(dev_id, cache_seed)

            # 2.5) Ensure Device Registry entries exist (service device + end-devices, namespaced)
            self._ensure_service_device_exists()
            created = self._ensure_registry_for_devices(filtered_devices, ignored)
            if created:
                _LOGGER.debug(
                    "Device Registry ensured/updated for %d device(s).", created
                )

            # 3) Decide whether to trigger a poll cycle (monotonic clock)
            # Build list of devices to POLL:
            # Poll devices that have at least one enabled DR entry for this config entry;
            # if a device has no DR entry yet, include it to allow initial discovery.
            devices_to_poll: list[dict[str, Any]] = []
            for dev in filtered_devices:
                dev_id = dev["id"]
                if dev_id in ignored:
                    continue
                if (
                    dev_id in self._enabled_poll_device_ids
                    or dev_id not in self._devices_with_entry
                ):
                    devices_to_poll.append(dev)

            # Apply per-device poll cooldowns
            if self._device_poll_cooldown_until and devices_to_poll:
                devices_to_poll = [
                    d
                    for d in devices_to_poll
                    if now_mono >= self._device_poll_cooldown_until.get(d["id"], 0.0)
                ]

            # Cold start detection: force immediate poll on first install when devices have no location data
            is_cold_start = (
                self._last_poll_mono == 0.0
                and filtered_devices
                and not any(
                    self._device_location_data.get(dev["id"])
                    for dev in filtered_devices
                )
            )

            elapsed_since_poll = now_mono - self._last_poll_mono
            hard_limit_passed = elapsed_since_poll >= self.min_poll_interval

            # Use helper to determine if poll cycle is due
            due = _is_poll_cycle_due_impl(
                elapsed=elapsed_since_poll,
                effective_interval=effective_interval,
                predictive_block=predictive_block,
                predictive_due=predictive_due,
                hard_limit_passed=hard_limit_passed,
                is_cold_start=is_cold_start,
            )
            if due and not self._is_polling and devices_to_poll:
                force_poll = False
                fcm_ready = self._is_fcm_ready_soft()
                if not fcm_ready:
                    # No baseline jump; schedule a short retry and escalate politely.
                    self._note_fcm_deferral(now_mono)
                    defer_started = self._fcm_defer_started_mono or 0.0
                    if defer_started:
                        elapsed = now_mono - defer_started
                        if elapsed >= _PUSH_NOT_READY_TIMEOUT_S:
                            force_poll = True
                        else:
                            self._schedule_short_retry(
                                min(5.0, effective_interval / 2.0)
                            )
                    else:
                        self._schedule_short_retry(min(5.0, effective_interval / 2.0))
                elif self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()

                if fcm_ready or force_poll:
                    if force_poll:
                        _LOGGER.warning(
                            "Push transport unavailable for %ds; forcing poll cycle.",
                            _PUSH_NOT_READY_TIMEOUT_S,
                        )
                    _LOGGER.debug(
                        "Scheduling background polling cycle (devices=%d, interval=%ds)",
                        len(devices_to_poll),
                        effective_interval,
                    )
                    self.hass.async_create_task(
                        self._async_start_poll_cycle(devices_to_poll, force=force_poll),
                        name=f"{DOMAIN}.poll_cycle",
                    )
            else:
                _LOGGER.debug(
                    "Poll not due (elapsed=%.1fs/%ss) or already running=%s",
                    now_mono - self._last_poll_mono,
                    effective_interval,
                    self._is_polling,
                )

            # 4) Build data snapshot for devices visible to the user (ignore-filter applied)
            visible_devices = [
                dev for dev in filtered_devices if dev["id"] not in ignored
            ]
            for dev in visible_devices:
                dev_id = dev["id"]
                cached_name = name_cache.get(dev_id)
                name = dev.get("name")
                if cached_name and (not isinstance(name, str) or not name.strip()):
                    dev["name"] = cached_name
            self._refresh_subentry_index(visible_devices)
            snapshot = await self._async_build_device_snapshot_with_fallbacks(
                visible_devices
            )
            self._store_subentry_snapshots(snapshot)

            # 4.5) Close the initial discovery window once we have a non-empty full list
            if not self._initial_discovery_done and filtered_devices:
                self._initial_discovery_done = True
                _LOGGER.info(
                    "Initial discovery window closed; newly discovered devices will be created disabled by default."
                )

            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(
                    max(
                        0,
                        effective_interval - (time.monotonic() - self._last_poll_mono),
                    )
                ),
            )
            return snapshot

        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as auth_exc:
            # Surface up to HA to trigger re-auth flow; create Repairs issue & flag before bubbling up.
            reason = self._short_error_message(auth_exc)
            self._set_api_status(ApiStatus.REAUTH, reason=reason)
            self._set_auth_state(
                failed=True,
                reason=f"Auth failed while fetching device list: {reason}",
            )
            raise auth_exc
        except UpdateFailed as update_err:
            # Let pre-wrapped UpdateFailed bubble as-is after updating status
            self._set_api_status(
                ApiStatus.ERROR,
                reason=self._short_error_message(update_err),
            )
            raise
        except Exception as err:
            # Record and raise as UpdateFailed per coordinator contract
            self.note_error(err, where="_async_update_data")
            message = self._short_error_message(err)
            self._set_api_status(ApiStatus.ERROR, reason=message)
            _LOGGER.exception("Unexpected error during coordinator update")
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(
        self, devices: list[dict[str, Any]], *, force: bool = False
    ) -> None:
        """Run a full sequential polling cycle in a background task.

        This runs with a lock to avoid overlapping cycles, updates the
        internal cache, and pushes snapshots at start and end.

        Throttling awareness:
        - If a device returns a crowdsourced location with `_report_hint` equal to
          "in_all_areas" (~10 min throttle) or "high_traffic" (~5 min throttle),
          we apply a per-device cooldown so subsequent polls avoid the throttled window.
          (See POPETS'25 for measured behaviour.)
        - The cooldown is at least the server minimum and at least one user poll interval.

        Args:
            devices: A list of device dictionaries to poll.
        """
        if not devices:
            return

        async with self._poll_lock:
            if self._is_polling:
                return

            # Double-check FCM readiness inside the lock to avoid a narrow race:
            # if readiness regressed between scheduling and execution, skip cleanly
            # unless we are explicitly forcing the cycle after a prolonged outage.
            if not self._is_fcm_ready_soft():
                if not force:
                    # No baseline jump; schedule a short retry and keep escalation ticking.
                    self._note_fcm_deferral(time.monotonic())
                    self._schedule_short_retry(5.0)
                    return
                _LOGGER.warning(
                    "Starting poll cycle without push transport; continuing in degraded mode."
                )
            elif self._fcm_defer_started_mono:
                # If we were deferring previously, clear the escalation timeline.
                self._clear_fcm_deferral()

            self._is_polling = True
            self.safe_update_metric("last_poll_start_mono", time.monotonic())
            _LOGGER.debug("Starting sequential poll of %d devices", len(devices))

            google_home_filter = self._get_google_home_filter()

            last_exception: Exception | None = None
            try:
                cycle_failed = False
                for idx, dev in enumerate(devices):
                    dev_id = dev["id"]
                    dev_name = dev.get("name", dev_id)
                    _LOGGER.debug(
                        "Sequential poll: requesting location for %s (%d/%d)",
                        dev_name,
                        idx + 1,
                        len(devices),
                    )

                    try:
                        # Protect API awaitable with timeout
                        location = await asyncio.wait_for(
                            self.api.async_get_device_location(dev_id, dev_name),
                            timeout=LOCATION_REQUEST_TIMEOUT_S,
                        )

                        # Success path: ensure any previous auth error is cleared
                        self._set_auth_state(failed=False)
                        # Reset transient auth failure counter on success
                        if self._consecutive_transient_auth_failures > 0:
                            _LOGGER.info(
                                "Location request succeeded; clearing %d transient auth failure(s).",
                                self._consecutive_transient_auth_failures,
                            )
                            self._consecutive_transient_auth_failures = 0
                            self._last_transient_auth_error = None

                        if not location:
                            _LOGGER.warning(
                                "No location data returned for %s", dev_name
                            )
                            continue

                        self._record_semantic_label(location, device_id=dev_id)
                        raw_semantic_name = (
                            location.get("semantic_name")
                            if isinstance(location.get("semantic_name"), str)
                            else None
                        )
                        semantic_replaced = False

                        cached_loc = self._device_location_data.get(dev_id)
                        is_replay = False
                        if isinstance(cached_loc, Mapping):
                            new_ts = _normalize_epoch_seconds(location.get("last_seen"))
                            old_ts = _normalize_epoch_seconds(
                                cached_loc.get("last_seen")
                            )
                            if (
                                new_ts is not None
                                and old_ts is not None
                                and new_ts == old_ts
                            ):
                                is_replay = True

                        location["is_replayed"] = is_replay
                        mapping_applied = self._apply_semantic_mapping(location)

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        # Consume coordinate substitution from the filter when needed.
                        semantic_name = location.get("semantic_name")
                        if (
                            not mapping_applied
                            and not is_replay
                            and semantic_name
                            and google_home_filter is not None
                        ):
                            try:
                                (
                                    should_filter,
                                    replacement_attrs,
                                ) = google_home_filter.should_filter_detection(
                                    dev_id, semantic_name
                                )
                            except Exception as gf_err:
                                _LOGGER.debug(
                                    "Google Home filter error for %s: %s",
                                    dev_name,
                                    gf_err,
                                )
                            else:
                                if should_filter:
                                    _LOGGER.debug(
                                        "Filtering out Google Home spam detection for %s",
                                        dev_name,
                                    )
                                    continue
                                if replacement_attrs:
                                    prev_location = self._device_location_data.get(
                                        dev_id
                                    )
                                    keep_previous_precise = (
                                        self._should_preserve_precise_home_coordinates(
                                            prev_location, replacement_attrs
                                        )
                                    )

                                    location = dict(location)
                                    if (
                                        keep_previous_precise
                                        and prev_location is not None
                                    ):
                                        _LOGGER.debug(
                                            "Google Home filter: %s detected at '%s', preserving previous precise coordinates",
                                            dev_name,
                                            semantic_name,
                                        )
                                        location["latitude"] = prev_location["latitude"]
                                        location["longitude"] = prev_location[
                                            "longitude"
                                        ]
                                        location["accuracy"] = prev_location["accuracy"]
                                    else:
                                        _LOGGER.info(
                                            "Google Home filter: %s detected at '%s', substituting with Home coordinates",
                                            dev_name,
                                            semantic_name,
                                        )
                                        if (
                                            "latitude" in replacement_attrs
                                            and "longitude" in replacement_attrs
                                        ):
                                            location["latitude"] = (
                                                replacement_attrs.get("latitude")
                                            )
                                            location["longitude"] = (
                                                replacement_attrs.get("longitude")
                                            )
                                        if (
                                            "radius" in replacement_attrs
                                            and replacement_attrs.get("radius")
                                            is not None
                                        ):
                                            location["accuracy"] = (
                                                replacement_attrs.get("radius")
                                            )
                                    # Clear semantic name so HA Core's zone engine determines the final state.
                                    location["semantic_name"] = None
                                    semantic_replaced = True
                        location.pop("is_replayed", None)
                        # ------------------------------------------------------------------

                        if raw_semantic_name and not semantic_replaced:
                            location["semantic_name"] = raw_semantic_name

                        # If we only got a semantic location, preserve previous coordinates.
                        # Use Phase 11 helper for the check
                        from .helpers.polling import (
                            calculate_location_age_hours,
                            get_age_log_level,
                            should_preserve_previous_coordinates,
                        )

                        if should_preserve_previous_coordinates(location):
                            prev = self._device_location_data.get(dev_id, {})
                            if prev:
                                location["latitude"] = prev.get("latitude")
                                location["longitude"] = prev.get("longitude")
                                location["accuracy"] = prev.get("accuracy")
                                location["status"] = (
                                    "Semantic location; preserving previous coordinates"
                                )

                        # Validate/normalize coordinates (and accuracy if present).
                        clear_metadata_only = (
                            (location.get("latitude") is not None
                            or location.get("longitude") is not None)
                            and location.get("metadata_only") is not True
                        )
                        self._persist_anchor_metadata(
                            dev_id, location, clear_metadata_only=clear_metadata_only
                        )
                        if not self._normalize_coords(location, device_label=dev_name):
                            if not location.get("semantic_name"):
                                _LOGGER.debug(
                                    "No location data (coordinates or semantic name) available for %s in this update.",
                                    dev_name,
                                )
                            # Nothing to commit/update in cache
                            # Strip any internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # Sanitize invariants + enrich fields (label, utc-string)
                        location = _sanitize_decoder_row(location)

                        if not self._apply_weighted_location_fusion(dev_id, location):
                            continue

                        # Skip redundant fusion inside update_device_cache when reusing this payload.
                        location["_fusion_preapplied"] = True

                        # Age diagnostics (informational) - use Phase 11 helpers
                        wall_now = time.time()
                        last_seen = location.get("last_seen", 0)
                        age_hours = calculate_location_age_hours(last_seen, wall_now)
                        if age_hours is not None:
                            log_level, should_log = get_age_log_level(age_hours)
                            if should_log and log_level == "info":
                                _LOGGER.info(
                                    "Using old location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )
                            elif should_log and log_level == "debug":
                                _LOGGER.debug(
                                    "Using location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )

                        # Commit via the shared cache helper when available;
                        # test doubles may stub this method, so fall back to a
                        # minimal cache write in that case.
                        helper = getattr(self.update_device_cache, "__func__", None)
                        if helper is GoogleFindMyCoordinator.update_device_cache:
                            self.update_device_cache(dev_id, location)
                        else:
                            location.pop("_fusion_preapplied", None)
                            location.pop("_report_hint", None)
                            location.setdefault("last_updated", wall_now)
                            merged_location = self._merge_with_existing_cache_row(
                                dev_id, location
                            )
                            self._device_location_data[dev_id] = merged_location

                        self.increment_stat("polled_updates")
                        self._consecutive_timeouts = 0

                        # Immediate per-device update for more responsive UI during long poll cycles.
                        self.push_updated([dev_id])

                    except TimeoutError as terr:
                        if self.is_fcm_connected:
                            _LOGGER.warning(
                                "Poll timed out for %s (FCM connected); ignoring error to keep status healthy.",
                                dev_name,
                            )
                            self.increment_stat("timeouts")
                        else:
                            _LOGGER.info(
                                "Location request timed out for %s after %s seconds",
                                dev_name,
                                LOCATION_REQUEST_TIMEOUT_S,
                            )
                            self.increment_stat("timeouts")
                            self._consecutive_timeouts += 1
                            cycle_failed = True
                            self.note_error(terr, where="poll_timeout", device=dev_name)
                            if last_exception is None:
                                last_exception = UpdateFailed(
                                    f"Location request timed out for {dev_name}"
                                )
                                last_exception.__cause__ = terr
                    except SpotAuthPermanentError as auth_err:
                        _LOGGER.warning(
                            "Authentication failed for %s; triggering reauth flow.",
                            dev_name,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: session invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        auth_exc = ConfigEntryAuthFailed(
                            "Google session invalid; re-authentication required"
                        )
                        last_exception = auth_exc
                        raise auth_exc from auth_err
                    except SpotApiEmptyResponseError:
                        _LOGGER.warning(
                            "Authentication failed for %s; triggering reauth flow.",
                            dev_name,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: session invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        auth_exc = ConfigEntryAuthFailed(
                            "Google session invalid; re-authentication required"
                        )
                        last_exception = auth_exc
                        raise auth_exc
                    except NovaAuthPermanentError as perm_err:
                        # Permanent auth failure (AAS token invalid) - immediate reauth
                        _LOGGER.error(
                            "Permanent authentication failure for %s: %s. Re-authentication required.",
                            dev_name,
                            perm_err,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Permanent auth failure for {dev_name}: credentials invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        self._consecutive_transient_auth_failures = 0
                        auth_exc = ConfigEntryAuthFailed(
                            "Google credentials invalid; re-authentication required"
                        )
                        last_exception = auth_exc
                        raise auth_exc from perm_err
                    except NovaAuthError as transient_err:
                        # Transient auth failure - may self-heal in subsequent poll cycles.
                        # Only trigger reauth after multiple consecutive failures.
                        self._consecutive_transient_auth_failures += 1
                        self._last_transient_auth_error = str(transient_err)

                        if self._consecutive_transient_auth_failures >= _MAX_TRANSIENT_AUTH_FAILURES:
                            _LOGGER.error(
                                "Transient auth failure for %s persisted across %d poll cycles: %s. "
                                "Triggering re-authentication.",
                                dev_name,
                                self._consecutive_transient_auth_failures,
                                transient_err,
                            )
                            self._set_auth_state(
                                failed=True,
                                reason=f"Auth failed for {dev_name} after {self._consecutive_transient_auth_failures} attempts",
                            )
                            cycle_failed = True
                            self._last_poll_result = "failed"
                            self._consecutive_timeouts = 0
                            auth_exc = ConfigEntryAuthFailed(
                                f"Authentication failed after {self._consecutive_transient_auth_failures} attempts; re-authentication required"
                            )
                            last_exception = auth_exc
                            raise auth_exc from transient_err

                        # Not yet at threshold - log warning and continue to next device
                        _LOGGER.warning(
                            "Transient auth failure for %s (%d/%d): %s. "
                            "Will retry in next poll cycle.",
                            dev_name,
                            self._consecutive_transient_auth_failures,
                            _MAX_TRANSIENT_AUTH_FAILURES,
                            transient_err,
                        )
                        cycle_failed = True
                        if last_exception is None:
                            last_exception = transient_err
                        continue  # Try next device instead of aborting entire cycle
                    except ConfigEntryAuthFailed as auth_exc:
                        # Mark auth failures to HA; abort remaining devices by re-raising.
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: {auth_exc}",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        last_exception = auth_exc
                        raise
                    except Exception as err:
                        _LOGGER.error(
                            "Failed to get location for %s: %s", dev_name, err
                        )
                        cycle_failed = True
                        self._consecutive_timeouts = 0
                        self.note_error(err, where="poll_exception", device=dev_name)
                        if last_exception is None:
                            last_exception = err

                    # Inter-device delay (except after the last one)
                    if idx < len(devices) - 1 and self.device_poll_delay > 0:
                        await asyncio.sleep(self.device_poll_delay)

                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
            finally:
                # Update scheduling baseline and clear flag, then push end snapshot
                self._last_poll_mono = time.monotonic()
                self._is_polling = False
                self.safe_update_metric("last_poll_end_mono", time.monotonic())
                if cycle_failed:
                    self._last_poll_result = "failed"
                else:
                    self._last_poll_result = "success"
                # Always publish the full snapshot so cached devices remain visible
                ignored = self._get_ignored_set()
                # Use the latest remembered full list; filter ignored
                visible_devices = [
                    d
                    for d in (self._last_device_list or [])
                    if d.get("id") not in ignored
                ]
                end_snapshot = self._build_snapshot_from_cache(
                    visible_devices, wall_now=time.time()
                )
                self.async_set_updated_data(end_snapshot)
                self._schedule_eid_resolver_refresh()
                if last_exception:
                    self.async_set_update_error(last_exception)

    # ---------------------------- Snapshot helpers --------------------------
    def _build_base_snapshot_entry(self, device_dict: dict[str, Any]) -> dict[str, Any]:
        """Create the base snapshot entry for a device."""
        return _build_base_snapshot_entry_impl(device_dict)

    def _update_entry_from_cache(self, entry: dict[str, Any], wall_now: float) -> bool:
        """Update the given snapshot entry in place from the in-memory cache.

        Args:
            entry: The device snapshot entry to update.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            True if the cache contained data for this device and the entry was updated, else False.
        """
        dev_id = entry["device_id"]
        cached = self._device_location_data.get(dev_id)
        if not cached:
            return False

        if isinstance(cached, Mapping):
            entry.update(cached)
        else:
            entry.update({"status": "Anchor metadata cached"})

        if entry.get("metadata_only"):
            entry.setdefault("status", "Anchor metadata cached")
            return True

        last_updated_ts = cached.get("last_updated", 0) if isinstance(cached, Mapping) else 0
        age = max(0.0, wall_now - float(last_updated_ts))
        entry["status"] = determine_location_status(age, self.location_poll_interval)
        return True

    def _build_snapshot_from_cache(
        self, devices: list[dict[str, Any]], wall_now: float
    ) -> list[dict[str, Any]]:
        """Build a lightweight snapshot using only the in-memory cache.

        This never touches HA state or the database; it is safe in background tasks.

        Args:
            devices: A list of device dictionaries to include in the snapshot.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            A list of device state dictionaries built from the cache.
        """
        snapshot: list[dict[str, Any]] = []
        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)
            # If cache has info, update status accordingly; otherwise keep default status.
            self._update_entry_from_cache(entry, wall_now)
            snapshot.append(entry)
        return snapshot

    async def _async_build_device_snapshot_with_fallbacks(
        self, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Build a snapshot using cache, HA state and (optionally) history fallback.

        Args:
            devices: A list of device dictionaries to build the snapshot for.

        Returns:
            A complete list of device state dictionaries with fallbacks applied.
        """
        snapshot: list[dict[str, Any]] = []
        wall_now = time.time()

        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)

            # Prefer cached result
            if self._update_entry_from_cache(entry, wall_now):
                entry = _sanitize_decoder_row(entry)
                snapshot.append(entry)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            dev_id = entry["device_id"]
            registry_entry = self._find_tracker_entity_entry(dev_id)
            if registry_entry is None:
                _LOGGER.debug(
                    "Skipping state/history fallback for '%s' because tracker cache entry is unavailable.",
                    entry["name"],
                )
                snapshot.append(entry)
                continue

            entity_id = registry_entry.entity_id
            if not entity_id:
                _LOGGER.debug(
                    "Entity registry entry missing entity_id for tracker '%s' (unique_id=%s)",
                    entry["name"],
                    registry_entry.unique_id,
                )
                snapshot.append(entry)
                continue

            state = self.hass.states.get(entity_id)
            if state:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                acc = state.attributes.get("gps_accuracy")
                last_seen_ts = _resolve_last_seen_from_attributes(
                    state.attributes, state.last_updated.timestamp()
                )
                if lat is not None and lon is not None:
                    entry.update(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "accuracy": acc,
                            "last_seen": last_seen_ts,
                            "status": "Using current state",
                        }
                    )
                    entry = _sanitize_decoder_row(entry)
                    snapshot.append(entry)
                    continue

            # Optional history fallback
            if self.allow_history_fallback:
                _LOGGER.warning(
                    "No live state for %s (entity_id=%s); attempting history fallback via Recorder.",
                    entry["name"],
                    entity_id,
                )
                rec = get_recorder(self.hass)
                result = await rec.async_add_executor_job(
                    _sync_get_last_gps_from_history, self.hass, entity_id
                )
                if result:
                    entry.update(result)
                    self.increment_stat("history_fallback_used")
                else:
                    _LOGGER.warning(
                        "No historical GPS data found for %s (entity_id=%s). "
                        "Entity may be excluded from Recorder.",
                        entry["name"],
                        entity_id,
                    )

            entry = _sanitize_decoder_row(entry)
            snapshot.append(entry)

        return snapshot

    # ---------------------------- Stats persistence -------------------------
    async def _async_load_stats(self) -> None:
        """Load statistics from entry-scoped cache."""
        try:
            cached = await self._cache.async_get_cached_value("integration_stats")
            if cached and isinstance(cached, dict):
                for key in self.stats.keys():
                    if key in cached:
                        self.stats[key] = cached[key]
                _LOGGER.debug("Loaded statistics from cache: %s", self.stats)
        except Exception as err:
            _LOGGER.debug("Failed to load statistics from cache: %s", err)

        try:
            sound_request_uuids = await self._cache.async_get_cached_value(
                "sound_request_uuids"
            )
            if not isinstance(sound_request_uuids, dict):
                _LOGGER.debug("No cached Play Sound UUIDs found; keeping current map")
                return

            # FIX: Filter out stale UUIDs to prevent Play Sound re-trigger after restart (#108)
            # Sound requests older than _SOUND_UUID_MAX_AGE_S are considered expired
            max_age_seconds = _SOUND_UUID_MAX_AGE_S
            now = time.time()
            loaded_sound_request_uuids: dict[str, str] = {}
            loaded_sound_request_timestamps: dict[str, float] = {}

            for device_id, data in sound_request_uuids.items():
                if not isinstance(device_id, str):
                    continue

                # Support new format: {"uuid": str, "ts": float}
                if isinstance(data, dict):
                    uuid_val = data.get("uuid")
                    ts_val = data.get("ts", 0)
                    if not isinstance(uuid_val, str) or not uuid_val:
                        continue
                    # Discard if older than max_age_seconds
                    if now - float(ts_val) > max_age_seconds:
                        _LOGGER.debug(
                            "Discarding expired Play Sound UUID for %s (age: %.0fs)",
                            device_id,
                            now - float(ts_val),
                        )
                        continue
                    loaded_sound_request_uuids[device_id] = uuid_val
                    loaded_sound_request_timestamps[device_id] = float(ts_val)
                # Support legacy format: str (UUID only, no timestamp)
                elif isinstance(data, str) and data:
                    # Legacy entries without timestamp are discarded on restart
                    # to prevent potential re-triggers
                    _LOGGER.debug(
                        "Discarding legacy Play Sound UUID for %s (no timestamp)",
                        device_id,
                    )
                    continue

            if self._sound_request_uuids and not loaded_sound_request_uuids:
                _LOGGER.debug(
                    "Skipping cached empty Play Sound UUID map; runtime map already populated"
                )
                return

            merged_sound_request_uuids = {
                **loaded_sound_request_uuids,
                **self._sound_request_uuids,
            }
            merged_sound_request_timestamps = {
                **loaded_sound_request_timestamps,
                **self._sound_request_timestamps,
            }
            if merged_sound_request_uuids != self._sound_request_uuids:
                self._sound_request_uuids = merged_sound_request_uuids
                self._sound_request_timestamps = merged_sound_request_timestamps
                _LOGGER.debug(
                    "Loaded Play Sound UUIDs from cache: %s", self._sound_request_uuids
                )
        except Exception as err:
            _LOGGER.debug(
                "Failed to load Play Sound UUIDs from cache; keeping current map: %s",
                err,
            )

    async def _async_save_stats(self) -> None:
        """Persist statistics to entry-scoped cache."""
        try:
            await self._cache.async_set_cached_value(
                "integration_stats", self.stats.copy()
            )
        except Exception as err:
            _LOGGER.debug("Failed to save statistics to cache: %s", err)

    async def _async_save_sound_uuids(self) -> None:
        """Persist Play Sound request UUIDs to entry-scoped cache.

        FIX: Store with timestamps to allow expiry filtering on reload (#108).
        Format: {device_id: {"uuid": str, "ts": float}}
        The timestamp is set when the UUID is first created, not on every save.
        """
        try:
            # Convert internal format to timestamped format for persistence
            # Use the original creation timestamp, not current time
            now = time.time()
            timestamped_uuids = {
                device_id: {
                    "uuid": uuid_val,
                    "ts": self._sound_request_timestamps.get(device_id, now),
                }
                for device_id, uuid_val in self._sound_request_uuids.items()
            }
            await self._cache.async_set_cached_value(
                "sound_request_uuids", timestamped_uuids
            )
        except Exception as err:
            _LOGGER.debug("Failed to save Play Sound UUIDs to cache: %s", err)

    async def _debounced_save_stats(self) -> None:
        """Debounce wrapper to coalesce frequent stat updates into a single write.

        This coroutine MUST run on the HA event loop. It is scheduled safely via
        `_schedule_stats_persist()` which ensures loop-thread execution.
        """
        try:
            await asyncio.sleep(self._stats_debounce_seconds)
            await self._async_save_stats()
        except asyncio.CancelledError:
            # Expected if a new increment arrives before the delay elapses; do nothing.
            return
        except Exception as err:
            _LOGGER.debug("Debounced stats save failed: %s", err)

    def _schedule_stats_persist(self) -> None:
        """(Re)schedule a debounced persistence task for statistics.

        Thread-safe: may be called from any thread. Ensures cancellation and creation
        of the debounced task happen on the HA loop.
        """

        def _do_schedule() -> None:
            # Cancel a pending writer, if any, and schedule a fresh one (loop-local).
            if self._stats_save_task and not self._stats_save_task.done():
                self._stats_save_task.cancel()
            create_task = getattr(
                self.hass, "async_create_background_task", self.hass.async_create_task
            )
            self._stats_save_task = create_task(
                self._debounced_save_stats(),
                name=f"{DOMAIN}.save_stats_debounced",
            )

        if self._is_on_hass_loop():
            _do_schedule()
        else:
            self._run_on_hass_loop(_do_schedule)

    def _increment_stat_on_loop(self, stat_name: str) -> None:
        """Increment a statistic on the HA loop and schedule persistence."""
        if stat_name in self.stats:
            before = self.stats[stat_name]
            self.stats[stat_name] = before + 1
            _LOGGER.debug(
                "Incremented %s from %s to %s",
                stat_name,
                before,
                self.stats[stat_name],
            )
            self._schedule_stats_persist()
            try:
                self.async_update_listeners()
            except Exception as err:
                _LOGGER.debug("Stats listener notification failed: %s", err)
        else:
            _LOGGER.warning(
                "Tried to increment unknown stat '%s'; available=%s",
                stat_name,
                list(self.stats.keys()),
            )

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter (thread-safe).

        May be called from any thread. The actual mutation and scheduling are
        marshalled onto the HA event loop.

        Note on performance:
        - The "hop" to the loop occurs exactly once here (constant per call).
          We avoid repeated hops for inner micro-operations.
        """
        if self._is_on_hass_loop():
            self._increment_stat_on_loop(stat_name)
        else:
            self._run_on_hass_loop(self._increment_stat_on_loop, stat_name)

    # ---------------------------- Public platform API -----------------------
    def get_device_location_data(self, device_id: str) -> dict[str, Any] | None:
        """Return current cached location dict for a device (or None).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary of location data or None if not found.
        """
        return self._device_location_data.get(device_id)

    def prime_device_location_cache(self, device_id: str, data: dict[str, Any]) -> None:
        """Seed/update the location cache for a device with (lat/lon/accuracy).

        Args:
            device_id: The canonical ID of the device.
            data: A dictionary containing latitude, longitude, and accuracy.
        """
        slot = self._device_location_data.get(device_id, {})
        slot.update(
            {
                k: v
                for k, v in data.items()
                if k in ("latitude", "longitude", "accuracy")
            }
        )
        # Do not set last_updated/last_seen here; this is only priming.
        self._device_location_data[device_id] = slot

    def seed_device_last_seen(self, device_id: str, ts_epoch: float) -> None:
        """Seed last_seen (epoch seconds) without overriding fresh data.

        Args:
            device_id: The canonical ID of the device.
            ts_epoch: The timestamp in epoch seconds.
        """
        slot = self._device_location_data.setdefault(device_id, {})
        slot.setdefault("last_seen", float(ts_epoch))

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
        location: Mapping[str, Any] | None,
        *,
        clear_metadata_only: bool,
    ) -> None:
        """Persist anchor metadata to _device_location_data for EID resolution.

        This function extracts EID-relevant metadata (identity_key, pair_date,
        secrets_creation_date, etc.) from location payloads and stores them in
        the in-memory _device_location_data cache.

        Data sources that call this function:
        - decrypt_locations: FCM push messages and manual LocateTracker responses
        - update_device_cache: Unified cache update path for all location data

        The stored metadata is later read by get_active_device_identities() to
        provide DeviceIdentity objects to the EID resolver.

        Note: Previously attempted to persist to HA DeviceRegistry custom_fields,
        but that parameter does not exist in the HA API. Now stores in-memory only;
        data is refreshed from API on each polling cycle or FCM update.
        """

        if not isinstance(location, Mapping):
            return

        def _persist_registry_anchor_metadata(anchor_payload: Mapping[str, Any]) -> None:
            # This function previously attempted to write to DeviceRegistry custom_fields,
            # which does not exist in HA's API. The anchor_payload is now stored only in
            # _device_location_data (see below). This nested function is retained to
            # trigger EID resolver refresh when identity_key changes.
            #
            # The EID resolver refresh ensures that newly-received identity keys from
            # decrypt_locations are immediately used for EID generation.
            if "identity_key" in anchor_payload:
                # FIX: Get eid_resolver from hass.data[DOMAIN][DATA_EID_RESOLVER],
                # NOT from self.eid_resolver (which was never set on the coordinator).
                # This was causing phones with offline detection to never trigger
                # EID resolver refresh, even though identity_key was being received.
                hass_obj = getattr(self, "hass", None)
                if hass_obj is None:
                    return
                domain_bucket = hass_obj.data.get(DOMAIN) if hasattr(hass_obj, "data") else None
                if not isinstance(domain_bucket, dict):
                    return
                eid_resolver = domain_bucket.get(DATA_EID_RESOLVER)
                if eid_resolver is not None:
                    _LOGGER.debug(
                        "Triggering EID Resolver refresh for %s (identity_key in anchor_payload)",
                        device_id,
                    )
                    # FIX: The method is called async_refresh, not async_trigger_immediate_refresh
                    refresh_coro = getattr(eid_resolver, "async_refresh", None)
                    if callable(refresh_coro):
                        hass_obj.async_create_task(refresh_coro())

        def _normalize_anchor_value(value: Any) -> int | Any:
            normalized = normalize_epoch_seconds(value)
            if normalized is not None:
                return normalized

            seconds: int | None = None
            nanos_raw: Any | None = None
            if isinstance(value, Mapping):
                seconds = normalize_epoch_seconds(value.get("seconds"))
                nanos_raw = value.get("nanos") or value.get("nsec")
            else:
                seconds = normalize_epoch_seconds(getattr(value, "seconds", None))
                nanos_raw = getattr(value, "nanos", None) or getattr(value, "nsec", None)

            nanos_fraction = None
            try:
                if nanos_raw is not None:
                    nanos_fraction = float(nanos_raw) / 1_000_000_000
            except (TypeError, ValueError):
                nanos_fraction = None

            if seconds is None and nanos_fraction is None:
                return value

            if seconds is None:
                return None
            if nanos_fraction:
                seconds += int(nanos_fraction)
            return seconds

        anchor_payload: dict[str, Any] = {}
        alt_keys = {
            "pair_date": ("pairDate",),
            "secrets_creation_date": ("secretsCreationDate",),
            "device_registration": ("deviceRegistration",),
            "device_type_information": ("deviceTypeInformation",),
            "encrypted_user_secrets": ("encryptedUserSecrets",),
            "identity_key": ("identityKey",),
            "identity_key_candidates": ("identityKeyCandidates",),
            "encrypted_identity_key": ("encryptedIdentityKey",),
            "encrypted_identity_key_candidates": (
                "encryptedIdentityKeyCandidates",
            ),
            "owner_key_version": ("ownerKeyVersion",),
            "time_anchors_debug": ("timeAnchorsDebug", "timeAnchors"),
        }

        for key in _PERSISTED_METADATA_KEYS:
            value = location.get(key)
            if value is None:
                for alt in alt_keys.get(key, ()):  # Prefer snake_case when present
                    if alt in location:
                        value = location.get(alt)
                        break

            if value is None:
                continue

            normalized_value = value
            if key in {"pair_date", "secrets_creation_date"}:
                normalized_value = _normalize_anchor_value(value)
                if normalized_value is None:
                    continue

            if isinstance(normalized_value, list) and normalized_value:
                anchor_payload[key] = list(normalized_value)
            elif isinstance(normalized_value, Mapping) and normalized_value:
                anchor_payload[key] = dict(normalized_value)
            else:
                anchor_payload[key] = normalized_value

        if not anchor_payload:
            return

        existing = self._device_location_data.get(device_id)
        merged: dict[str, Any] = {}
        if isinstance(existing, Mapping):
            merged.update(existing)

        if clear_metadata_only:
            merged.pop("metadata_only", None)

        merged.update(anchor_payload)
        if location.get("metadata_only") and not clear_metadata_only:
            merged["metadata_only"] = True

        self._device_location_data[device_id] = merged

        # FIX: Trigger EID refresh AFTER cache is updated, not before.
        # This ensures the phone's identity_key is available when the resolver
        # collects device identities.
        _persist_registry_anchor_metadata(anchor_payload)

    def _get_predicted_poll_time(self) -> float | None:
        """Predict the earliest next update time based on device histories."""

        history_store = getattr(self, "_device_update_history", None)
        if not history_store:
            return None

        predictions: list[float] = []

        for history in history_store.values():
            if len(history) < 2:
                continue

            intervals = [
                history[idx + 1] - history[idx] for idx in range(len(history) - 1)
            ]
            avg_interval = mean(intervals)

            if len(intervals) >= 2 and stdev(intervals) > 300:
                continue

            predictions.append(history[-1] + avg_interval)

        if not predictions:
            return None

        return min(predictions)

    def update_device_cache(
        self, device_id: str, location_data: dict[str, Any]
    ) -> None:
        """Public, encapsulated update of the internal location cache for one device (thread-safe).

        Used by the FCM receiver (push path) and by internal manual-commit call sites.
        Expects validated fields (decrypt layer performs fail-fast checks).

        Internal rules:
        - Applies type-aware **poll** cooldowns based on an internal `_report_hint` (if present).
        - Strips `_report_hint` from the cached payload to avoid exposing internal fields.
        - Applies weighted fusion and semantic-anchor protection to stabilize coordinates
          while still updating timestamps and metadata.
        """
        if not self._is_on_hass_loop():
            # Marshal entire update onto the HA loop to avoid cross-thread mutations.
            self._run_on_hass_loop(self.update_device_cache, device_id, location_data)
            return

        if not isinstance(location_data, dict):
            _LOGGER.debug(
                "Ignored cache update for %s: payload is not a dict", device_id
            )
            return

        # Shallow copy to avoid caller-side mutation
        from .helpers.cache import (
            preserve_metadata_fields,
            should_clear_metadata_only_flag,
        )

        slot = dict(location_data)

        previous_cached = self._device_location_data.get(device_id)
        if not isinstance(previous_cached, Mapping):
            previous_cached = None
        comparison_cached = previous_cached

        # Preserve metadata fields from previous cache using helper
        slot = preserve_metadata_fields(previous_cached, slot, _PERSISTED_METADATA_KEYS)

        # Handle metadata_only flag using helper
        incoming_metadata_only = location_data.get("metadata_only")
        if should_clear_metadata_only_flag(slot, incoming_metadata_only):
            slot.pop("metadata_only", None)

        has_location_payload = (
            slot.get("latitude") is not None or slot.get("longitude") is not None
        )
        clear_metadata_only = has_location_payload and incoming_metadata_only is not True
        self._persist_anchor_metadata(
            device_id, slot, clear_metadata_only=clear_metadata_only
        )
        self._record_semantic_label(slot, device_id=device_id)

        cached_loc = comparison_cached
        is_replay = False
        if isinstance(cached_loc, Mapping):
            new_ts = _normalize_epoch_seconds(slot.get("last_seen"))
            old_ts = _normalize_epoch_seconds(cached_loc.get("last_seen"))
            if new_ts is not None and old_ts is not None and new_ts == old_ts:
                is_replay = True

        slot["is_replayed"] = is_replay
        self._apply_semantic_mapping(slot)
        slot.pop("is_replayed", None)

        report_hint = slot.get("_report_hint")

        fusion_preapplied = bool(slot.pop("_fusion_preapplied", False))

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
        is_stationary_logic = (
            status
            in (
                "Fused (Weighted)",
                "Stationary (at Anchor)",
            )
            or is_replay
        )

        if fused_applied and status == "Fused (Weighted)":
            self.increment_stat("fused_updates")

        if is_stationary_logic:
            self._apply_report_type_cooldown(device_id, report_hint)
        else:
            _LOGGER.debug(
                "Skipping throttle cooldown for %s despite '%s' hint (movement detected)",
                device_id,
                report_hint,
            )

        # Track crowd-sourced updates when hint is present
        if report_hint:
            self.increment_stat("crowd_sourced_updates")

        slot.pop("_report_hint", None)
        slot.pop("is_replayed", None)

        # Sanitize invariants + enrich fields before gating
        slot = _sanitize_decoder_row(slot)

        raw_last_seen = _normalize_epoch_seconds(slot.get("last_seen"))
        self._track_device_interval(device_id, raw_last_seen)

        # Increment crowdsourced stats for push/manual commits as well
        if slot.get("source_label") == "crowdsourced":
            self.increment_stat("crowd_sourced_updates")

        if not self._is_significant_update(device_id, slot):
            _LOGGER.debug(
                "Dropping cache update for %s: update failed significance checks",
                device_id,
            )
            return

        resolver_refresh_needed = False

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
                (
                    "Identity key update detected for %s "
                    "(ownerKeyVersion=%s); scheduling EID resolver refresh."
                ),
                device_id,
                incoming_owner_key_version,
            )

        # Ensure last_updated is present
        slot.setdefault("last_updated", time.time())

        # Preserve recency/coordinate fidelity before committing to cache.
        slot = self._merge_with_existing_cache_row(device_id, slot)

        name_cache = self._ensure_device_name_cache()

        # Keep human-friendly name mapping up-to-date if provided alongside
        name = slot.get("name")
        if isinstance(name, str) and name:
            name_cache[device_id] = name

        self._device_location_data[device_id] = slot
        # Increment background updates to account for push/manual commits.
        self.increment_stat("background_updates")

        # Update identity_key mapping and propagate location to shared devices
        effective_identity_key = incoming_identity_key or cached_identity_key
        if effective_identity_key is not None:
            self._register_identity_key(device_id, effective_identity_key)
            # Propagate location to all devices sharing the same tracker
            self._propagate_location_to_shared_devices(
                device_id, effective_identity_key, slot
            )

        if resolver_refresh_needed:
            self._reset_resolver_offset(device_id)
            self._schedule_eid_resolver_refresh()

    def _register_identity_key(self, device_id: str, identity_key: bytes) -> None:
        """Register a device's identity_key for shared tracker detection.

        Maintains a mapping from identity_key to all device_ids that share the
        same physical tracker. This enables location propagation across accounts.

        Args:
            device_id: Canonical device identifier.
            identity_key: Normalized 32-byte identity key.
        """
        if not isinstance(identity_key, bytes) or len(identity_key) != 32:
            return

        device_set = self._identity_key_to_devices.setdefault(identity_key, set())
        if device_id not in device_set:
            device_set.add(device_id)
            if len(device_set) > 1:
                _LOGGER.info(
                    "Shared tracker detected: identity_key=%s... shared by %d devices: %s",
                    identity_key[:8].hex(),
                    len(device_set),
                    sorted(device_set),
                )

    def _propagate_location_to_shared_devices(
        self,
        source_device_id: str,
        identity_key: bytes,
        location_data: dict[str, Any],
    ) -> None:
        """Propagate location data to all devices sharing the same identity_key.

        When a tracker is shared between multiple Google accounts, each account
        has its own canonical_id but they share the same identity_key. This method
        ensures that location updates received for one device are propagated to
        all other devices representing the same physical tracker.

        Args:
            source_device_id: Device that received the original location update.
            identity_key: The shared identity key.
            location_data: Location data to propagate.
        """
        # Guard against infinite recursion (propagation calling propagation)
        if self._propagating_location:
            return

        device_set = self._identity_key_to_devices.get(identity_key)
        if not device_set or len(device_set) <= 1:
            return

        # Extract location-relevant and Bermuda-specific fields to propagate
        propagate_fields = (
            "latitude",
            "longitude",
            "accuracy",
            "last_seen",
            "last_updated",
            "altitude",
            "address",
            "source_label",
            "status",
            # Bermuda-specific fields
            "bermuda_area",
            "bermuda_rssi",
            "bermuda_distance",
            "bermuda_floor",
            "bermuda_scanner",
            "bermuda_last_seen",
        )
        propagated_data: dict[str, Any] = {}
        for key in propagate_fields:
            if key in location_data:
                propagated_data[key] = location_data[key]

        # Check if we have any data worth propagating (coordinates OR Bermuda data)
        has_coordinates = propagated_data.get("latitude") or propagated_data.get("longitude")
        has_bermuda_data = any(k.startswith("bermuda_") for k in propagated_data)

        if not has_coordinates and not has_bermuda_data:
            # No data to propagate
            return

        # Mark as propagated to prevent re-propagation
        propagated_data["_propagated_from"] = source_device_id

        self._propagating_location = True
        try:
            for target_device_id in device_set:
                if target_device_id == source_device_id:
                    continue

                target_cached = self._device_location_data.get(target_device_id)
                if target_cached is None:
                    # Target device not yet in cache (will be populated on next poll)
                    continue

                # Compare timestamps: only propagate if newer
                # For location data, use last_seen; for Bermuda data, use bermuda_last_seen
                should_propagate = True

                if has_coordinates:
                    source_ts = _normalize_epoch_seconds(propagated_data.get("last_seen"))
                    target_ts = _normalize_epoch_seconds(target_cached.get("last_seen"))

                    if source_ts is not None and target_ts is not None:
                        if source_ts <= target_ts:
                            # Target has same or newer location data
                            should_propagate = False

                if has_bermuda_data and should_propagate:
                    # For Bermuda data, compare bermuda_last_seen
                    source_bermuda_ts = propagated_data.get("bermuda_last_seen")
                    target_bermuda_ts = target_cached.get("bermuda_last_seen")

                    if source_bermuda_ts and target_bermuda_ts:
                        if source_bermuda_ts <= target_bermuda_ts:
                            # Target has same or newer Bermuda data, but still might need coordinates
                            if not has_coordinates:
                                should_propagate = False

                if not should_propagate:
                    continue

                # Merge propagated data into target's cached data
                merged = dict(target_cached)
                merged.update(propagated_data)
                merged["_propagated_from"] = source_device_id

                _LOGGER.debug(
                    "Propagating data from %s to shared device %s (identity_key=%s..., "
                    "has_coords=%s, has_bermuda=%s)",
                    source_device_id,
                    target_device_id,
                    identity_key[:8].hex(),
                    has_coordinates,
                    has_bermuda_data,
                )

                self._device_location_data[target_device_id] = merged
        finally:
            self._propagating_location = False

    def _reset_resolver_offset(self, device_id: str) -> None:
        """Clear resolver offsets using registry IDs when identity keys rotate."""

        hass = getattr(self, "hass", None)
        if hass is None:
            return

        registry_id: str | None = None
        entry_id = self._entry_id()

        dev_reg = dr.async_get(hass)
        if entry_id and dev_reg:
            identifiers = {
                (DOMAIN, f"{entry_id}:{device_id}"),
                (DOMAIN, device_id),
            }
            device = dev_reg.async_get_device(identifiers=identifiers)
            if device:
                registry_id = device.id

        if not registry_id:
            _LOGGER.debug(
                "Could not resolve Registry ID for canonical %s; skipping offset reset.",
                device_id,
            )
            return

        hass_data = getattr(hass, "data", None)
        if not isinstance(hass_data, dict):
            return

        bucket = hass_data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return

        resolver = bucket.get(DATA_EID_RESOLVER)
        if resolver is None:
            return

        reset = getattr(resolver, "reset_device_offset", None)
        if callable(reset):
            _LOGGER.debug(
                "Triggering resolver offset reset for %s (Registry ID: %s)",
                device_id,
                registry_id,
            )
            reset(registry_id)

    def _is_significant_update(self, device_id: str, new_data: dict[str, Any]) -> bool:
        """Validate temporal ordering before committing cache updates.

        Weighted fusion now stabilizes coordinates earlier in the pipeline, so
        this gate focuses on rejecting malformed or stale timestamps that would
        regress cached locations. Callers run fusion before invoking this shim;
        no additional coordinate logic belongs here.

        Args:
            device_id: Canonical device identifier.
            new_data: The latest location payload to evaluate.

        Returns:
            ``True`` when the caller should accept the payload.
        """

        if not isinstance(new_data, dict):
            _LOGGER.debug("Rejecting update for %s: payload is not a dict", device_id)
            return False

        n_seen_norm = _normalize_epoch_seconds(new_data.get("last_seen"))
        if n_seen_norm is not None:
            if n_seen_norm < 946684800.0:  # < 2000-01-01
                self.increment_stat("invalid_ts_drop_count")
                self.increment_stat("drop_reason_invalid_ts")
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
            _LOGGER.debug(
                "Rejecting update for %s: timestamp regressed (%s < %s)",
                device_id,
                n_seen_norm,
                e_seen_norm,
            )
            return False

        return True

    def _merge_with_existing_cache_row(
        self, device_id: str, incoming: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge accepted payloads with cached rows while respecting recency.

        Uses helper functions from coordinator_cache for decision logic while
        preserving metadata fields via _update_preserve_metadata.
        """
        from .helpers.cache import (
            LOCATION_FIELDS,
            fill_missing_coordinates,
            preserve_monotonic_timestamp,
            should_allow_location_update,
        )

        existing = self._device_location_data.get(device_id)
        if not existing:
            return incoming

        existing_seen = _normalize_epoch_seconds(existing.get("last_seen"))
        incoming_seen = _normalize_epoch_seconds(incoming.get("last_seen"))
        existing_rank = existing.get("source_rank")
        incoming_rank = incoming.get("source_rank")

        # Use helper for timestamp/rank decision
        allow_location_update = should_allow_location_update(
            existing_seen, incoming_seen, existing_rank, incoming_rank
        )

        # Handle case where distance check is needed (allow_location_update is None)
        if allow_location_update is None:
            incoming_lat = self._coerce_float(incoming.get("latitude"))
            incoming_lon = self._coerce_float(incoming.get("longitude"))
            existing_lat = self._coerce_float(existing.get("latitude"))
            existing_lon = self._coerce_float(existing.get("longitude"))

            if (
                incoming_lat is not None
                and incoming_lon is not None
                and existing_lat is not None
                and existing_lon is not None
            ):
                try:
                    dist = self._haversine_distance(
                        existing_lat, existing_lon, incoming_lat, incoming_lon
                    )
                    if dist > _LOCATION_SIGNIFICANT_CHANGE_M:
                        allow_location_update = True
                        _LOGGER.debug(
                            "Allowing location update for %s without timestamp "
                            "(position changed by %.1fm)",
                            device_id,
                            dist,
                        )
                    else:
                        allow_location_update = False
                except Exception:
                    allow_location_update = False
            else:
                allow_location_update = False

        # Build merged result using _update_preserve_metadata
        merged = dict(existing)
        if allow_location_update:
            _update_preserve_metadata(merged, incoming)
        else:
            _update_preserve_metadata(
                merged,
                {
                    key: value
                    for key, value in incoming.items()
                    if key not in LOCATION_FIELDS
                },
            )

        # Use helpers for timestamp and coordinate handling
        merged = preserve_monotonic_timestamp(existing, merged)
        merged = fill_missing_coordinates(existing, merged)

        return merged

    # ---------------------------- Significance / gating ----------------------
    def _haversine_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Return distance in meters between two WGS84 coordinates."""
        return _haversine_distance_impl(lat1, lon1, lat2, lon2)

    def _apply_weighted_location_fusion(
        self, device_id: str, new_data: dict[str, Any]
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

        new_lat = self._coerce_float(new_data.get("latitude"))
        new_lon = self._coerce_float(new_data.get("longitude"))
        if new_lat is None or new_lon is None:
            return True

        existing_lat = self._coerce_float(existing.get("latitude"))
        existing_lon = self._coerce_float(existing.get("longitude"))
        if existing_lat is None or existing_lon is None:
            return True

        existing_acc_raw = self._coerce_float(existing.get("accuracy"))
        new_acc_raw = self._coerce_float(new_data.get("accuracy"))

        existing_acc = safe_accuracy(existing_acc_raw)
        new_acc = safe_accuracy(new_acc_raw)

        try:
            dist = self._haversine_distance(
                existing_lat, existing_lon, new_lat, new_lon
            )
        except Exception:
            return True

        radius_sum = existing_acc + new_acc

        if new_data.get("location_type") == "trusted":
            return True

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

        if dist > radius_sum:
            return True

        w_old = 1 / (max(1.0, existing_acc) ** 2)
        w_new = 1 / (max(1.0, new_acc) ** 2)
        total_w = w_old + w_new
        if total_w == 0:
            return True

        lat_fused = (existing_lat * w_old + new_lat * w_new) / total_w
        lon_fused = (existing_lon * w_old + new_lon * w_new) / total_w

        new_data["latitude"] = lat_fused
        new_data["longitude"] = lon_fused

        best_accuracy: float | None
        if existing_acc_raw is not None and new_acc_raw is not None:
            best_accuracy = min(existing_acc_raw, new_acc_raw)
        else:
            best_accuracy = existing_acc_raw or new_acc_raw

        if best_accuracy is not None:
            new_data["accuracy"] = best_accuracy

        new_data["status"] = "Fused (Weighted)"
        new_data["_fused_applied"] = True
        return True

    def get_device_last_seen(self, device_id: str) -> datetime | None:
        """Return last_seen as timezone-aware datetime (UTC) if cached."""
        ts = self._device_location_data.get(device_id, {}).get("last_seen")
        return epoch_to_datetime_utc(ts)

    def get_device_display_name(self, device_id: str) -> str | None:
        """Return the human-readable device name if known.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            The display name as a string, or None.
        """
        return self._ensure_device_name_cache().get(device_id)

    def get_device_name_map(self) -> dict[str, str]:
        """Return a shallow copy of the internal device-id -> name mapping.

        Returns:
            A dictionary mapping device IDs to their names.
        """
        return dict(self._ensure_device_name_cache())

    # ---------------------------- Presence & Purge API ----------------------------
    def is_device_present(self, device_id: str) -> bool:
        """Return True if the given device_id is present (TTL-smoothed)."""
        ts = self._present_last_seen.get(device_id, 0.0)
        return not is_presence_expired(ts, time.monotonic(), self._presence_ttl_s)

    def get_absent_device_ids(self) -> list[str]:
        """Return ids known by name/cache that are expired under the presence TTL."""
        now_mono = time.monotonic()
        ttl = self._presence_ttl_s
        name_cache = self._ensure_device_name_cache()
        known = set(name_cache) | set(self._device_location_data)
        return sorted([
            d for d in known
            if is_presence_expired(self._present_last_seen.get(d, 0.0), now_mono, ttl)
        ])

    def purge_device(self, device_id: str) -> None:
        """Remove all cached data and cooldown state for a device (thread-safe publish).

        Called from the config-entry device deletion flow. This does not trigger a poll,
        but it immediately publishes an updated snapshot so UI can refresh.
        """
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(self.purge_device, device_id)
            return

        self._device_location_data.pop(device_id, None)
        self._ensure_device_name_cache().pop(device_id, None)
        self._device_caps.pop(device_id, None)
        self._locate_inflight.discard(device_id)
        self._locate_cooldown_until.pop(device_id, None)
        removed_uuid = self._sound_request_uuids.pop(device_id, None)
        self._sound_request_timestamps.pop(device_id, None)
        self._device_poll_cooldown_until.pop(device_id, None)
        self._present_device_ids.discard(device_id)

        if removed_uuid is not None:
            self.hass.async_create_task(self._async_save_sound_uuids())
        self._present_last_seen.pop(device_id, None)
        # Rebuild the cached snapshot without the purged device
        current_snapshot: list[dict[str, Any]] = []
        for row in list(self.data or []):
            if not isinstance(row, dict):
                continue
            if row.get("device_id") == device_id or row.get("id") == device_id:
                continue
            current_snapshot.append(dict(row))

        devices_stub = [
            {"id": entry.get("device_id") or entry.get("id"), "name": entry.get("name")}
            for entry in current_snapshot
            if isinstance(entry.get("device_id") or entry.get("id"), str)
        ]
        self._refresh_subentry_index(devices_stub)
        self._store_subentry_snapshots(current_snapshot)
        self.async_set_updated_data(current_snapshot)

    # ---------------------------- Push updates ------------------------------
    def push_updated(
        self,
        device_ids: list[str] | None = None,
        *,
        reset_baseline: bool = True,
    ) -> None:
        """Publish a fresh snapshot to listeners after push (FCM) cache updates.

        Thread-safe: may be called from any thread. This method ensures all state
        publishing happens on the HA event loop.

        This **does not** trigger a poll. It:
        - Immediately pushes cache state to entities via `async_set_updated_data()`.
        - Optionally resets the internal poll baseline to 'now' to prevent an immediate
          re-poll when push-driven updates arrive (`reset_baseline=True` by default).
        - Optionally limits the snapshot to `device_ids`; otherwise includes all known devices.

        Args:
            device_ids: An optional list of device IDs to include in the update.
            reset_baseline: If True (default), reset the scheduler baseline to now.
        """
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(
                self.push_updated, device_ids, reset_baseline=reset_baseline
            )
            return

        wall_now = time.time()
        self._set_fcm_status(FcmStatus.CONNECTED)
        if reset_baseline:
            self._last_poll_mono = time.monotonic()  # optional: reset poll timer

        # Choose device ids for the snapshot
        name_cache = self._ensure_device_name_cache()

        if device_ids:
            ids = device_ids
        else:
            # union of all known names and cached locations
            ids = list({*name_cache.keys(), *self._device_location_data.keys()})

        # Apply ignore filter first to avoid touching presence for ignored devices.
        ignored = self._get_ignored_set()
        ids = [d for d in ids if d not in ignored]

        # Touch presence timestamps for pushed devices (keeps presence stable)
        now_mono = time.monotonic()
        for dev_id in ids:
            self._present_last_seen[dev_id] = now_mono

        # Build "devices" stubs from id->name mapping
        devices_stub: list[dict[str, Any]] = []
        for dev_id in ids:
            cached_name = name_cache.get(dev_id)
            if isinstance(cached_name, str) and cached_name.strip():
                name = cached_name if cached_name != dev_id else "Google Find My Device"
            else:
                name = "Google Find My Device"
            devices_stub.append({"id": dev_id, "name": name})

        snapshot = self._build_snapshot_from_cache(devices_stub, wall_now=wall_now)
        self._refresh_subentry_index(devices_stub)
        self._store_subentry_snapshots(snapshot)
        self.async_set_updated_data(snapshot)
        _LOGGER.debug(
            "Pushed snapshot for %d device(s) via push_updated()", len(snapshot)
        )

    # ---------------------------- Play sound helpers ------------------------
    def _api_push_ready(self) -> bool:
        """Best-effort check whether push/FCM is initialized (backward compatible).

        Optimistic default: if we cannot determine readiness explicitly,
        return True so the UI stays usable; the API call will enforce reality.

        Returns:
            True if the push mechanism is believed to be ready.
        """
        # Short-circuit via cooldown window after a transport failure.
        now = time.monotonic()
        if now < self._push_cooldown_until:
            if self._push_ready_memo is not False:
                _LOGGER.debug(
                    "Push readiness: cooldown active -> treating as not ready"
                )
            self._push_ready_memo = False
            return False

        ready: bool | None = None
        try:
            fn = getattr(self.api, "is_push_ready", None)
            if callable(fn):
                ready = bool(fn())
            else:
                for attr in ("push_ready", "fcm_ready", "receiver_ready"):
                    val = getattr(self.api, attr, None)
                    if isinstance(val, bool):
                        ready = val
                        break
                if ready is None:
                    fcm = getattr(self.api, "fcm", None)
                    if fcm is not None:
                        for attr in ("is_ready", "ready"):
                            val = getattr(fcm, attr, None)
                            if isinstance(val, bool):
                                ready = val
                                break
        except Exception as err:
            _LOGGER.debug(
                "Push readiness check exception: %s (defaulting optimistic True)", err
            )
            ready = True

        if ready is None:
            ready = True  # optimistic default

        if ready != self._push_ready_memo:
            _LOGGER.debug("Push readiness changed: %s", ready)
            self._push_ready_memo = ready

        return ready

    def _note_push_transport_problem(self, cooldown_s: int = 90) -> None:
        """Enter a temporary cooldown after a push transport failure to avoid spamming.

        Args:
            cooldown_s: The duration of the cooldown in seconds.
        """
        self._push_cooldown_until = time.monotonic() + cooldown_s
        self._push_ready_memo = False
        _LOGGER.debug(
            "Entering push cooldown for %ss after transport failure", cooldown_s
        )
        self._set_fcm_status(
            FcmStatus.DEGRADED,
            reason=f"Push transport recovering from error (cooldown {cooldown_s}s)",
        )

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

    def update_settings(
        self,
        *,
        ignored_devices: list[str] | None = None,
        location_poll_interval: int | None = None,
        device_poll_delay: int | None = None,
        min_poll_interval: int | None = None,
        allow_history_fallback: bool | None = None,
        contributor_mode: str | None = None,
        contributor_mode_switch_epoch: int | None = None,
    ) -> None:
        """Apply updated user settings provided by the config entry (options-first).

        This method deliberately enforces basic typing/limits to keep the coordinator sane
        regardless of where the values came from.

        Args:
            ignored_devices: A list of device IDs to hide from snapshots/polling.
            location_poll_interval: The interval in seconds for location polling.
            device_poll_delay: The delay in seconds between polling devices.
            min_poll_interval: The minimum polling interval in seconds.
            allow_history_fallback: Whether to allow falling back to Recorder history.
            contributor_mode: Updated contributor mode ("high_traffic" or "in_all_areas").
            contributor_mode_switch_epoch: Epoch timestamp when the mode last changed.
        """
        if ignored_devices is not None:
            # This attribute is only used as a fallback when config_entry is not available.
            self.ignored_devices = list(ignored_devices)

        if location_poll_interval is not None:
            try:
                self.location_poll_interval = max(1, int(location_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid location_poll_interval=%r", location_poll_interval
                )

        if device_poll_delay is not None:
            try:
                self.device_poll_delay = max(0, int(device_poll_delay))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid device_poll_delay=%r", device_poll_delay
                )

        if min_poll_interval is not None:
            try:
                self.min_poll_interval = max(1, int(min_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid min_poll_interval=%r", min_poll_interval
                )

        if allow_history_fallback is not None:
            self.allow_history_fallback = bool(allow_history_fallback)

        if contributor_mode is not None:
            normalized_mode = self._sanitize_contributor_mode(contributor_mode)
            if (
                contributor_mode_switch_epoch is None
                or contributor_mode_switch_epoch <= 0
            ):
                contributor_mode_switch_epoch = int(time.time())
            epoch = int(contributor_mode_switch_epoch)
            if (
                normalized_mode != self._contributor_mode
                or epoch != self._contributor_mode_switch_epoch
            ):
                self._contributor_mode = normalized_mode
                self._contributor_mode_switch_epoch = epoch
                self.api.set_contributor_mode(
                    normalized_mode, switch_epoch=self._contributor_mode_switch_epoch
                )
                self._async_persist_contributor_mode()

        # Settings adjustments may change per-subentry views
        self._refresh_subentry_index()
        self._schedule_eid_resolver_refresh()

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

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
                _LOGGER.warning(
                    "Manual locate for %s is currently disabled (push transport not ready).",
                    name,
                )
                return {}

            # Enter in-flight and set a lower-bound cooldown window
            self._locate_inflight.add(device_id)
            self._locate_cooldown_until[device_id] = time.monotonic() + float(
                DEFAULT_MIN_POLL_INTERVAL
            )
            self.async_set_updated_data(self.data)

            google_home_filter = self._get_google_home_filter()

            try:
                location_data = await self.api.async_get_device_location(device_id, name)

                # Success path: clear any auth error state
                self._set_auth_state(failed=False)

                if not location_data:
                    return {}

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
                        _LOGGER.debug("Google Home filter error for %s: %s", name, gf_err)
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
                                location_data["accuracy"] = prev_location["accuracy"]
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
                        location_data.setdefault("accuracy", prev.get("accuracy"))
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

                # Apply type-aware cooldowns based on internal hint (if any), then strip it.
                report_hint = slot.get("_report_hint")
                self._apply_report_type_cooldown(device_id, report_hint)

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
                entry = getattr(self, "config_entry", None)
                reauth_started = False
                if entry is not None:
                    try:
                        await entry.async_start_reauth(self.hass)
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
            if ok and request_uuid is not None:
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
        except Exception as err:
            _LOGGER.debug(
                "async_play_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self.note_error(err, where="async_play_sound", device=device_id)
            self._note_push_transport_problem()
            return False

    async def async_stop_sound(
        self, device_id: str, request_uuid: str | None = None
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
            request_uuid_to_use = self._sound_request_uuids.get(device_id)
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
        except Exception as err:
            _LOGGER.debug(
                "async_stop_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self.note_error(err, where="async_stop_sound", device=device_id)
            self._note_push_transport_problem()
            return False

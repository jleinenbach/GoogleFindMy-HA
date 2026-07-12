# custom_components/googlefindmy/map_view.py
"""Map view for Google Find My Device locations."""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    WEEK_SECONDS,
    map_token_hex_digest,
    map_token_secret_seed,
)
from .ha_typing import HomeAssistantView
from .map_i18n import format_showing, is_rtl, resolve_map_labels
from .vendor.openlocationcode import encode as _encode_plus_code

_LOGGER = logging.getLogger(__name__)

_COORDINATOR_CLASS: type[Any] | None = None


def _resolve_coordinator_class() -> type[Any]:
    """Import the coordinator lazily to avoid pulling in HTTP at import time."""

    global _COORDINATOR_CLASS
    if _COORDINATOR_CLASS is None:
        from .coordinator import GoogleFindMyCoordinator as _GoogleFindMyCoordinator

        _COORDINATOR_CLASS = _GoogleFindMyCoordinator
    return _COORDINATOR_CLASS


def _resolve_language(hass: Any) -> str | None:
    """Best-effort read of the Home Assistant UI language.

    Returns the configured ``hass.config.language`` when present, otherwise
    ``None`` so the label resolver falls back to English. Read defensively via
    ``getattr`` so a degraded or stubbed ``hass`` (no ``config``) never crashes
    the map render; a missing language is a fail-safe English page, not an error.
    """

    config = getattr(hass, "config", None)
    language = getattr(config, "language", None)
    return language if isinstance(language, str) else None


# Inline SVG "content copy" icon (no icon-font dependency). Braceless, so it is
# safe to interpolate into the map HTML f-string via a single placeholder.
_MAP_COPY_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">'
    '<path fill="currentColor" d="M16 1H4a2 2 0 0 0-2 2v12h2V3h12V1zm3 4H8a2 2 '
    "0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"
    '"/></svg>'
)

# Clipboard copy helper injected once into the map <script>. Defined as a plain
# (non-f) string so its literal JS braces need no doubling; it is interpolated
# into the f-string via a single ``{_MAP_COPY_HELPER_JS}`` placeholder.
#
# Secure-context aware: navigator.clipboard is undefined on plain-LAN
# http://<ip>:8123 (window.isSecureContext === false); fall back to a hidden
# textarea + execCommand so copy still works outside HTTPS / Nabu Casa.
_MAP_COPY_HELPER_JS = """
        function gfmyFlashCopied(iconEl) {
            if (!iconEl) { return; }
            iconEl.classList.add('gfmy-copied');
            setTimeout(function() { iconEl.classList.remove('gfmy-copied'); }, 1000);
        }
        function gfmyFallbackCopy(value) {
            var ta = document.createElement('textarea');
            ta.value = value;            // property assignment, never innerHTML
            ta.setAttribute('readonly', '');
            ta.style.position = 'absolute';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) { /* no-op */ }
            document.body.removeChild(ta);
        }
        function gfmyCopyToClipboard(value, iconEl) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(value).then(
                    function() { gfmyFlashCopied(iconEl); },
                    function() { gfmyFallbackCopy(value); gfmyFlashCopied(iconEl); }
                );
                return;
            }
            gfmyFallbackCopy(value);
            gfmyFlashCopied(iconEl);
        }
"""


def _plus_code_for(lat: float, lon: float) -> str | None:
    """Return the full 10-digit Plus Code for a coordinate, or None if invalid.

    Offline, deterministic, no API key. Guards non-finite (NaN/inf) inputs so a
    corrupted fix yields no code rather than a bogus one. The 10-digit code is an
    11-character string including the ``+`` separator (e.g. ``8FVC9G8F+6X``).
    """
    try:
        if not math.isfinite(lat) or not math.isfinite(lon):
            return None
        return _encode_plus_code(float(lat), float(lon), 10)
    except (ValueError, TypeError):
        return None


def _build_location_entry(
    *,
    lat: float,
    lon: float,
    accuracy: float,
    accuracy_estimated: bool,
    timestamp: str,
    last_seen: float,
    is_own_report: Any,
    semantic_location: Any,
) -> dict[str, Any]:
    """Build one map location payload dict, incl. the derived Plus Code.

    Extracted as a pure module function so the server-side ``plus_code`` wiring
    is unit-testable without the HTTP stack. All pre-existing fields are passed
    through unchanged; ``plus_code`` is the only added key.
    """
    return {
        "lat": lat,
        "lon": lon,
        "accuracy": accuracy,
        "accuracy_estimated": accuracy_estimated,
        "timestamp": timestamp,
        "last_seen": last_seen,
        "is_own_report": is_own_report,
        "semantic_location": semantic_location,
        "plus_code": _plus_code_for(lat, lon),
    }


_SAFE_ACCURACY: Any = None


def _resolve_safe_accuracy() -> Any:
    """Import the canonical GPS accuracy normalizer lazily.

    ``safe_accuracy`` lives under the coordinator package, which map_view
    deliberately avoids importing at module load time (see
    ``_resolve_coordinator_class``). Resolving and caching it on first use
    preserves the same import-time guarantees while letting the map reuse the
    integration-wide accuracy policy instead of duplicating it.

    We import it as a direct attribute of ``.coordinator`` (re-exported there),
    exactly like ``_resolve_coordinator_class`` resolves the coordinator class.
    Reaching into the nested ``.coordinator.helpers.geo`` submodule instead would
    require the coordinator to be a real package, which breaks lightweight test
    loaders that install it as a plain ``ModuleType`` stub. Tests can also patch
    the cached ``_SAFE_ACCURACY`` directly or expose ``safe_accuracy`` on the stub.
    """

    global _SAFE_ACCURACY
    if _SAFE_ACCURACY is None:
        from .coordinator import safe_accuracy as _safe_accuracy

        _SAFE_ACCURACY = _safe_accuracy
    return _SAFE_ACCURACY


_IS_VALID_ACCURACY: Any = None


def _resolve_is_valid_accuracy() -> Any:
    """Import the canonical accuracy validity predicate lazily.

    ``is_valid_accuracy`` is the single source of truth for "does this raw
    gps_accuracy represent a real measurement, or would safe_accuracy fall back
    to the conservative radius?". Resolving it here (re-exported on
    ``.coordinator``, exactly like ``_resolve_safe_accuracy``) lets the map flag
    estimated points without duplicating the validity policy in Python or JS.
    """

    global _IS_VALID_ACCURACY
    if _IS_VALID_ACCURACY is None:
        try:
            from .coordinator import is_valid_accuracy as _is_valid_accuracy
        except ImportError:
            # Partial coordinator stubs (and any future re-export gaps) may not
            # expose ``is_valid_accuracy``. Fall back to a predicate that mirrors
            # the canonical geo.py policy so the map keeps rendering instead of
            # crashing the history view. Kept in sync with
            # coordinator.helpers.geo.is_valid_accuracy.
            _is_valid_accuracy = _fallback_is_valid_accuracy

        _IS_VALID_ACCURACY = _is_valid_accuracy
    return _IS_VALID_ACCURACY


# Minimum accuracy treated as a real measurement (1 millimeter). Mirrors
# coordinator.helpers.geo.MIN_VALID_ACCURACY; below this is the 0.0 error code.
_MIN_VALID_ACCURACY = 0.001


def _fallback_is_valid_accuracy(value: float | None) -> bool:
    """Mirror ``coordinator.helpers.geo.is_valid_accuracy`` without importing it.

    Used only when the canonical re-export cannot be resolved (see
    ``_resolve_is_valid_accuracy``). A value is valid when it is not None, casts
    to a finite float, and is at least ``_MIN_VALID_ACCURACY`` (0.001m).
    """

    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v >= _MIN_VALID_ACCURACY


_RECORDED_ACCURACY_PAIR: Any = None


def _resolve_recorded_accuracy_pair() -> Any:
    """Import the canonical recorded-accuracy reader lazily.

    ``recorded_accuracy_pair`` is the single authoritative reader for the
    ``(raw_accuracy, estimated_flag)`` pair of a recorded state. Reading the
    value and its provenance flag from the same source is what stops a stale
    or fallback radius from being paired with the wrong provenance. Resolving
    it here (re-exported on ``.coordinator``, exactly like
    ``_resolve_is_valid_accuracy``) keeps the map in lockstep with the producer
    instead of re-deriving the precedence in the view.
    """

    global _RECORDED_ACCURACY_PAIR
    if _RECORDED_ACCURACY_PAIR is None:
        try:
            from .coordinator import recorded_accuracy_pair as _pair
        except ImportError:
            # Partial coordinator stubs may not expose the reader yet. Fall
            # back to a copy of the canonical precedence so the history view
            # keeps rendering. Kept in sync with
            # coordinator.helpers.geo.recorded_accuracy_pair.
            _pair = _fallback_recorded_accuracy_pair

        _RECORDED_ACCURACY_PAIR = _pair
    return _RECORDED_ACCURACY_PAIR


def _fallback_recorded_accuracy_pair(
    attributes: Any,
) -> tuple[Any, bool | None]:
    """Mirror ``coordinator.helpers.geo.recorded_accuracy_pair`` without import.

    Used only when the canonical re-export cannot be resolved (see
    ``_resolve_recorded_accuracy_pair``). Prefers the stable producer
    ``accuracy_m`` over the volatile core ``gps_accuracy`` and returns the
    producer ``accuracy_estimated`` flag, or ``None`` for legacy rows.
    """

    raw = attributes.get("accuracy_m")
    if raw is None:
        raw = attributes.get("gps_accuracy")
    flag = attributes.get("accuracy_estimated")
    return raw, (bool(flag) if flag is not None else None)


_PARSE_LAST_SEEN_TIMESTAMP: Any = None


def _resolve_parse_last_seen_timestamp() -> Any:
    """Import the canonical last_seen timestamp parser lazily.

    ``parse_last_seen_timestamp`` is the single source of truth for turning a
    recorded ``last_seen``/``last_seen_utc`` attribute (numeric epoch or ISO 8601
    string) back into epoch seconds. Resolving it here (re-exported on
    ``.coordinator``, exactly like ``_resolve_safe_accuracy``) keeps the map
    history timestamps in lockstep with the device_tracker restore path instead
    of re-deriving the parsing in the view. The stub coordinator module exposes
    it as a direct attribute (see the test loader), mirroring ``safe_accuracy``,
    so no in-view fallback copy is needed.
    """

    global _PARSE_LAST_SEEN_TIMESTAMP
    if _PARSE_LAST_SEEN_TIMESTAMP is None:
        from .coordinator import parse_last_seen_timestamp as _parse

        _PARSE_LAST_SEEN_TIMESTAMP = _parse
    return _PARSE_LAST_SEEN_TIMESTAMP


# ------------------------------- HTML Helpers -------------------------------


def _html_response(title: str, body: str, status: int = 200) -> web.Response:
    """Return a minimal HTML response (no secrets, no stacktraces)."""
    return web.Response(
        text=f"""<!DOCTYPE html>
<html>
<head><meta charset=\"utf-8\"><title>{title}</title></head>
<body>
  <h1>{title}</h1>
  <p>{body}</p>
</body>
</html>""",
        content_type="text/html",
        status=status,
    )


# --------------------------- Token / Entry helpers ---------------------------


def _entry_accept_tokens(
    hass: HomeAssistant,
    entry_id: str,
    token_expiration_enabled: bool,
) -> set[str]:
    """Compute the accepted tokens for a given entry_id.

    Contract (must match Buttons/Sensor/Tracker):
      secret = map_token_secret_seed(...)
      token  = map_token_hex_digest(secret)

    For weekly tokens, accept the current and previous bucket (grace on week rollover).
    For static tokens, accept only the static form.
    """
    ha_uuid = str(hass.data.get("core.uuid", "ha"))
    tokens: set[str] = set()
    if token_expiration_enabled:
        now = int(time.time())
        current_secret = map_token_secret_seed(ha_uuid, entry_id, True, now=now)
        prev_secret = map_token_secret_seed(
            ha_uuid, entry_id, True, now=now - WEEK_SECONDS
        )
        tokens.add(map_token_hex_digest(current_secret))
        tokens.add(map_token_hex_digest(prev_secret))
    else:
        secret = map_token_secret_seed(ha_uuid, entry_id, False)
        tokens.add(map_token_hex_digest(secret))
    return tokens


def _resolve_entry_by_token(
    hass: HomeAssistant, auth_token: str
) -> tuple[ConfigEntry, set[str]] | tuple[None, None]:
    """Return (entry, accepted_tokens) for the entry that matches the token, else (None, None).

    We iterate over all config entries for this DOMAIN and compare the provided token
    against the per-entry accepted token set (weekly/static as per options).
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        token_exp = entry.options.get(
            OPT_MAP_VIEW_TOKEN_EXPIRATION,
            entry.data.get(
                OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            ),
        )
        accepted = _entry_accept_tokens(hass, entry.entry_id, bool(token_exp))
        if auth_token in accepted:
            return entry, accepted
    return None, None


# ------------------------------- Map View -----------------------------------


class GoogleFindMyMapView(HomeAssistantView):
    """View to serve device location maps with token validation and history."""

    url = "/api/googlefindmy/map/{device_id}"
    name = "api:googlefindmy:map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the Home Assistant instance to the view."""
        super().__init__()
        self.hass = hass

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Generate and serve a map for the device with history and filtering."""
        # 1. Security Check (Keep existing logic)
        auth_token = request.query.get("token")
        if not auth_token:
            return _html_response(
                "Unauthorized", "Missing authentication token.", status=401
            )

        entry, _accepted = _resolve_entry_by_token(self.hass, auth_token)
        if not entry:
            _LOGGER.debug("Map token mismatch for device_id=%s", device_id)
            return _html_response(
                "Unauthorized", "Invalid authentication token.", status=401
            )

        # 2. Resolve Device Name (Best effort from Coordinator)
        # We lazily resolve the coordinator to get the friendly name
        coordinator_cls = _resolve_coordinator_class()
        runtime = getattr(entry, "runtime_data", None)
        device_name: str | None = None

        # Try to find device in the entry's coordinator data
        if runtime:
            coordinator = (
                runtime
                if isinstance(runtime, coordinator_cls)
                else getattr(runtime, "coordinator", None)
            )
            if coordinator:
                data = getattr(coordinator, "data", []) or []
                for dev in data:
                    if dev.get("id") == device_id:
                        raw_name = dev.get("name")
                        if raw_name and raw_name.strip():
                            device_name = raw_name.strip()
                        break

        # 3. Find the Entity ID (for History Lookup)
        registry = er.async_get(self.hass)
        entity_id: str | None = None
        entity_entry: er.RegistryEntry | None = None
        # Try standard unique_id formats
        possible_unique_ids = [
            f"{entry.entry_id}:{device_id}",
            f"{DOMAIN}_{entry.entry_id}_{device_id}",
            f"{DOMAIN}_{device_id}",
        ]

        for uid in possible_unique_ids:
            ent = registry.async_get_entity_id("device_tracker", DOMAIN, uid)
            if not ent:
                continue

            registry_entry = getattr(registry, "async_get", lambda _eid: None)(ent)
            if registry_entry and registry_entry.config_entry_id != entry.entry_id:
                continue

            entity_id = ent
            entity_entry = registry_entry
            break

        # Fallback search by matching unique_id suffix if exact match fails
        if not entity_id:
            registry_entities = getattr(registry, "entities", None)
            if registry_entities:
                for entity in registry_entities.values():
                    if (
                        entity.platform == DOMAIN
                        and entity.config_entry_id == entry.entry_id
                    ):
                        if entity.unique_id.endswith(
                            f":{device_id}"
                        ) or entity.unique_id.endswith(f"_{device_id}"):
                            entity_id = entity.entity_id
                            entity_entry = entity
                            break

        if not entity_entry and entity_id:
            entity_entry = getattr(registry, "async_get", lambda _eid: None)(entity_id)

        if entity_entry:
            # Registry stubs used in tests may omit device links; guard the lookup.
            device_id_attr = getattr(entity_entry, "device_id", None)
            device_entry = None
            if device_id_attr:
                device_entry = dr.async_get(self.hass).async_get(device_id_attr)

            registry_name = None
            if device_entry:
                registry_name = device_entry.name_by_user or device_entry.name

            if registry_name and registry_name.strip() and not device_name:
                device_name = registry_name.strip()

        if not device_name:
            # Name fallback order: coordinator data -> device registry metadata -> placeholder.
            device_name = resolve_map_labels(_resolve_language(self.hass))[
                "unknown_device"
            ]

        # 4. Parse Filters (Time & Accuracy)
        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(days=7)  # Default 7 days history

        try:
            if s_param := request.query.get("start"):
                start_time = datetime.fromisoformat(s_param.replace("Z", "+00:00"))
            if e_param := request.query.get("end"):
                end_time = datetime.fromisoformat(e_param.replace("Z", "+00:00"))
        except ValueError:
            pass  # Use defaults on error

        # Ensure timezones
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=dt_util.UTC)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=dt_util.UTC)

        try:
            accuracy_filter = int(request.query.get("accuracy", 0))
        except (ValueError, TypeError):
            accuracy_filter = 0

        # 5. Fetch History from Recorder
        locations: list[dict[str, Any]] = []
        seen_timestamps: set[float] = set()
        safe_accuracy = _resolve_safe_accuracy()
        is_valid_accuracy = _resolve_is_valid_accuracy()
        recorded_accuracy_pair = _resolve_recorded_accuracy_pair()
        parse_last_seen_timestamp = _resolve_parse_last_seen_timestamp()
        if entity_id:
            try:
                from homeassistant.components.recorder import get_instance
            except ImportError:
                get_instance = None
            from homeassistant.components.recorder.history import get_significant_states

            recorder = get_instance(self.hass) if get_instance else None
            async_add_executor_job = getattr(
                recorder, "async_add_executor_job", self.hass.async_add_executor_job
            )
            try:
                # Run heavy DB query in recorder executor to avoid regression warnings
                history = await async_add_executor_job(
                    get_significant_states, self.hass, start_time, end_time, [entity_id]
                )

                if entity_id in history:
                    for state in history[entity_id]:
                        try:
                            # Stale/blank recorded states withhold the plain
                            # latitude/longitude keys; the last known fix lives in
                            # the recorder-only last_latitude/last_longitude keys.
                            # Fall back to those so the history still plots the
                            # point (a TypeError from a fully missing pair is
                            # caught below and skips the sample).
                            lat = float(
                                state.attributes.get(
                                    "latitude", state.attributes.get("last_latitude")
                                )
                            )
                            lon = float(
                                state.attributes.get(
                                    "longitude", state.attributes.get("last_longitude")
                                )
                            )
                            # Read the accuracy value AND its estimated-provenance
                            # flag from the same authoritative source. ``accuracy_m``
                            # is the stable producer attribute; it survives Home
                            # Assistant clearing the volatile core ``gps_accuracy``
                            # on stale states, where the entity drops lat/lon but
                            # the recorder still keeps accuracy_m/accuracy_estimated.
                            # Reading the value from gps_accuracy while reading the
                            # flag separately let a stale fallback row be drawn as a
                            # real circle (Codex review, PR #1124).
                            raw_accuracy, flag = recorded_accuracy_pair(
                                state.attributes
                            )
                            # Normalize accuracy through the integration's
                            # canonical policy: 0.0m (the Android error code),
                            # negative, NaN, Inf and missing values are corrupted
                            # data and map to a conservative fallback radius (see
                            # coordinator.helpers.geo.safe_accuracy). This keeps
                            # the map consistent with the live entity and stops
                            # unknown-quality points from masquerading as 0.0m.
                            acc = safe_accuracy(raw_accuracy)

                            # Apply the "Min Accuracy" filter from the UI slider.
                            # accuracy_filter <= 0 disables the filter (default),
                            # so every point is kept. Otherwise drop points whose
                            # accuracy radius is larger (worse) than the requested
                            # threshold. Because acc is normalized above, invalid
                            # or missing accuracies fall back to the conservative
                            # radius and are dropped under any tighter slider.
                            if accuracy_filter > 0 and acc > accuracy_filter:
                                continue

                            # Determine timestamp (prefer the recorded report
                            # time over the recorder write time for precision).
                            # Parse ``last_seen`` first, then ``last_seen_utc`` as
                            # the same fallback, through the canonical parser --
                            # mirroring the device_tracker restore path, which
                            # accepts recorder-only rows carrying only
                            # ``last_seen_utc``. Without the second key those
                            # restored/legacy rows would plot at ``last_updated``
                            # (the recorder write/restart time), making a stale
                            # fix appear fresh and sort/dedupe incorrectly.
                            ts = state.last_updated.timestamp()
                            parsed_ts = parse_last_seen_timestamp(
                                state.attributes.get("last_seen")
                            )
                            if parsed_ts is None:
                                parsed_ts = parse_last_seen_timestamp(
                                    state.attributes.get("last_seen_utc")
                                )
                            if parsed_ts is not None:
                                ts = parsed_ts

                            if ts in seen_timestamps:
                                continue

                            seen_timestamps.add(ts)
                            # Whether ``accuracy`` is the conservative fallback
                            # radius (raw gps_accuracy was missing/0.0/NaN/Inf/
                            # negative) rather than a real measurement. The client
                            # draws accuracy circles only for real measurements.
                            #
                            # Prefer the producer flag persisted by the cache
                            # sanitizer: once the raw value was replaced with the
                            # 200m fallback it is indistinguishable from a real
                            # 200m fix, so only the producer knows it was
                            # estimated. ``flag`` is None for legacy recorder rows
                            # predating the producer attribute; for those we fall
                            # back to the validity predicate on the same raw value
                            # the pair returned (accuracy_m, else gps_accuracy).
                            estimated = (
                                flag
                                if flag is not None
                                else not is_valid_accuracy(raw_accuracy)
                            )
                            locations.append(
                                _build_location_entry(
                                    lat=lat,
                                    lon=lon,
                                    accuracy=acc,
                                    accuracy_estimated=estimated,
                                    timestamp=datetime.fromtimestamp(
                                        ts, tz=dt_util.UTC
                                    ).isoformat(),
                                    last_seen=ts,
                                    is_own_report=state.attributes.get("is_own_report"),
                                    semantic_location=state.attributes.get(
                                        "semantic_name"
                                    ),
                                )
                            )
                        except (ValueError, TypeError, KeyError):
                            continue  # Skip invalid states
            except Exception as err:  # pragma: no cover - log only
                _LOGGER.warning("Failed to fetch history for map: %s", err)

        locations.sort(key=lambda location: location.get("last_seen", 0))

        # 6. Render
        html = self._generate_map_html(
            device_name, locations, device_id, start_time, end_time, accuracy_filter
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    def _generate_map_html(
        self,
        device_name: str,
        locations: list[dict[str, Any]],
        device_id: str,
        start_time: datetime,
        end_time: datetime,
        accuracy_filter: int,
    ) -> str:
        """Generate rich HTML map with Leaflet, history markers, and filter controls."""

        # Calculate center
        center_lat = 0.0
        center_lon = 0.0
        if locations:
            center_lat = sum(location["lat"] for location in locations) / len(locations)
            center_lon = sum(location["lon"] for location in locations) / len(locations)

        # Serialize data for JS
        def _sanitize(value: Any) -> Any:
            return escape(value) if isinstance(value, str) else value

        safe_locations = [
            {key: _sanitize(value) for key, value in location.items()}
            for location in locations
        ]

        locations_json = json.dumps(safe_locations)

        start_local = dt_util.as_local(start_time).strftime("%Y-%m-%dT%H:%M")
        end_local = dt_util.as_local(end_time).strftime("%Y-%m-%dT%H:%M")

        # Localize the self-rendered map. HA frontend translations do not reach
        # this hand-built HTML page, so labels are resolved server-side from the
        # UI language (English fallback). Client-JS labels are serialized once as
        # a JSON object and read by key in the browser; server-side panel labels
        # are interpolated as escaped f-string values (plan A2/A3).
        language = _resolve_language(self.hass)
        labels = resolve_map_labels(language)
        labels_json = json.dumps(labels)
        html_attrs = f'lang="{escape(language or "en")}"'
        if is_rtl(language):
            html_attrs += ' dir="rtl"'

        return f"""<!DOCTYPE html>
<html {html_attrs}>
<head>
    <title>{escape(device_name)} - {escape(labels["location_history"])}</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <style>
        body {{ margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; }}
        #map {{ height: 100vh; width: 100%; }}
        .controls {{
            position: absolute; top: 10px; right: 10px; z-index: 1000;
            background: rgba(255, 255, 255, 0.95); padding: 15px;
            border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.2);
            max-width: 300px; backdrop-filter: blur(5px);
        }}
        .controls h3 {{ margin: 0 0 10px; font-size: 16px; line-height: 1.2; }}
        .control-group {{ margin-bottom: 10px; }}
        label {{ display: block; font-size: 12px; font-weight: bold; color: #333; margin-bottom: 4px; }}
        input[type="datetime-local"], input[type="range"] {{ width: 100%; padding: 5px; border: 1px solid #ddd; border-radius: 4px; }}
        button {{
            background: #007bff; color: white; border: none; padding: 8px 15px;
            border-radius: 4px; cursor: pointer; width: 100%; font-weight: bold;
        }}
        button:hover {{ background: #0056b3; }}
        .stats {{ font-size: 12px; color: #666; text-align: center; margin-top: 10px; border-top: 1px solid #eee; padding-top: 5px; }}
        .gfmy-copy {{ cursor: pointer; color: #007bff; display: inline-flex; vertical-align: middle; margin-left: 3px; }}
        .gfmy-copy:hover {{ color: #0056b3; }}
        .gfmy-copy.gfmy-copied {{ color: #28a745; }}
        .controls details {{ margin: 0; }}
        .controls details > summary {{
            cursor: pointer; font-size: 14px; font-weight: bold; color: #333;
            padding: 12px 0; min-height: 20px; user-select: none;
        }}
        .controls details > summary:hover {{ color: #0056b3; }}
        .controls details[open] > summary {{ margin-bottom: 6px; }}
        .controls details > summary:focus-visible {{ outline: 2px solid #007bff; outline-offset: 2px; }}
    </style>
</head>
<body>
    <div class="controls">
        <h3>{escape(device_name)}</h3>
        <details open>
            <summary>{escape(labels["filters_summary"])}</summary>
            <div class="control-group">
                <label>{escape(labels["start_time"])}</label>
                <input type="datetime-local" id="start" value="{start_local}">
            </div>
            <div class="control-group">
                <label>{escape(labels["end_time"])}</label>
                <input type="datetime-local" id="end" value="{end_local}">
            </div>
            <div class="control-group">
                <label>{escape(labels["min_accuracy"])}: <span id="acc-val">{accuracy_filter}</span></label>
                <input type="range" id="accuracy" min="0" max="500" step="10" value="{accuracy_filter}" oninput="document.getElementById('acc-val').innerText = this.value">
            </div>
            <button onclick="applyFilters()">{escape(labels["apply_filters"])}</button>
        </details>
        <div class="stats">
            {escape(format_showing(labels, len(locations)))}
        </div>
    </div>
    <div id="map"></div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        var map = L.map('map').setView([{center_lat}, {center_lon}], 13);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors',
            referrerPolicy: 'origin'
        }}).addTo(map);

        var locations = {locations_json};
        var markers = L.layerGroup().addTo(map);
        var GFMY_COPY_ICON = '{_MAP_COPY_ICON_SVG}';
        // Localized labels, serialized once server-side; read by key in the
        // browser. json.dumps emits a valid JS object literal; the surrounding
        // f-string treats this as a single replacement field, so the object's
        // braces are inserted as data and never parsed as f-string syntax.
        var GFMY_LABELS = {labels_json};
        // Display-only precision for popup coordinates; copy keeps full precision.
        var GFMY_COORD_DISPLAY_DECIMALS = 5;
{_MAP_COPY_HELPER_JS}

        // Accuracy-circle / marker-fade constants (two separate concerns):
        // FOCUS_FILL_OPACITY: fill of the single focus disc. Deliberately below
        // the Leaflet default (L.Path.prototype.options.fillOpacity == 0.2) so a
        // ~200m radius does not hide the map underneath. Editorial value, not
        // sourceable (the user wants it fainter than the default on purpose).
        var FOCUS_FILL_OPACITY = 0.12;
        // FADE_SPAN: marker-DOT recency fade, mirrors Home Assistant's
        // gradualOpacity (hui-map-card.ts == 0.8): newest dot 1.0, oldest
        // 1 - FADE_SPAN == 0.2. Rank-based (index) like ha-map.ts, not time.
        var FADE_SPAN = 0.8;
        // Exactly one filled focus disc may exist at a time (one-FILL invariant),
        // coupled to Leaflet's one-popup-open invariant.
        var focusCircle = null;

        function toLocalInputValue(date) {{
            var year = date.getFullYear();
            var month = String(date.getMonth() + 1).padStart(2, '0');
            var day = String(date.getDate()).padStart(2, '0');
            var hours = String(date.getHours()).padStart(2, '0');
            var minutes = String(date.getMinutes()).padStart(2, '0');
            return year + '-' + month + '-' + day + 'T' + hours + ':' + minutes;
        }}

        // LAYER 3: the single filled focus disc (one-FILL invariant). Draws a
        // metric accuracy disc only for real measurements; estimated
        // (fallback-radius) points get no misleading precision circle.
        function setFocus(loc, latlng) {{
            if (focusCircle) {{ map.removeLayer(focusCircle); focusCircle = null; }}
            if (!loc.accuracy_estimated) {{
                focusCircle = L.circle(latlng, {{
                    radius: loc.accuracy,
                    color: loc.is_own_report ? '#28a745' : '#007bff',
                    weight: 2,
                    fillOpacity: FOCUS_FILL_OPACITY,
                    opacity: 1.0,
                    interactive: false
                }}).addTo(map);
            }}
        }}

        function drawMap() {{
            markers.clearLayers();
            if (focusCircle) {{ map.removeLayer(focusCircle); focusCircle = null; }}
            var bounds = L.latLngBounds();
            var n = locations.length;
            var autoFocusMarker = null;

            locations.forEach(function(loc, idx) {{
                var color = loc.is_own_report ? '#28a745' : '#007bff';
                // LAYER 1: marker-dot recency fade (HA model, rank-based).
                // Newest (idx == n-1) full 1.0, oldest (idx 0) floor 1 - FADE_SPAN.
                var markerFill = n <= 1 ? 1.0 : (1 - FADE_SPAN) + (idx / (n - 1)) * FADE_SPAN;

                // LAYER 2: ambient accuracy ring (stroke-only) per real point.
                // Stroke opacity rank-faded with floor 0 so old rings dissolve
                // into "fog" instead of covering the map; fills never stack
                // (stroke overlaps only at line crossings, not over areas).
                if (!loc.accuracy_estimated) {{
                    var ringOpacity = n <= 1 ? 1.0 : idx / (n - 1);
                    markers.addLayer(L.circle([loc.lat, loc.lon], {{
                        radius: loc.accuracy,
                        color: color,
                        weight: 1,
                        fill: false,
                        opacity: ringOpacity,
                        interactive: false
                    }}));
                }}

                var marker = L.circleMarker([loc.lat, loc.lon], {{
                    radius: 6,
                    color: '#fff',
                    weight: 1,
                    fillColor: color,
                    fillOpacity: markerFill
                }});

                var date = new Date(loc.timestamp).toLocaleString();
                var source = loc.is_own_report ? GFMY_LABELS.own_device : GFMY_LABELS.crowdsourced;

                // Google-Maps-ready decimal degrees: dot decimal separator (JS
                // default) and "lat, lon" order with a comma+space separator.
                var coordStr = loc.lat + ", " + loc.lon;
                // Display-only: round to N decimals, then strip padding zeros so a
                // shorter source value stays short. Copy path keeps coordStr raw.
                var coordDisplay = Number(Number(loc.lat).toFixed(GFMY_COORD_DISPLAY_DECIMALS)) + ", " + Number(Number(loc.lon).toFixed(GFMY_COORD_DISPLAY_DECIMALS));
                var popupHtml =
                    "<b>" + GFMY_LABELS.time + "</b> " + date + "<br>" +
                    "<b>" + GFMY_LABELS.accuracy + "</b> " + loc.accuracy.toFixed(1) + "m<br>" +
                    "<b>" + GFMY_LABELS.source + "</b> " + source + "<br>";
                if (loc.semantic_location) {{
                    popupHtml += "<b>" + GFMY_LABELS.location + "</b> " + loc.semantic_location + "<br>";
                }}
                if (loc.plus_code) {{
                    popupHtml += "<b>Plus Code:</b> " + loc.plus_code +
                        " <span class='gfmy-copy gfmy-copy-plus' role='button' tabindex='0'" +
                        " title='" + GFMY_LABELS.copy_to_clipboard + "'" +
                        " aria-label='" + GFMY_LABELS.copy_plus_code + "'>" +
                        GFMY_COPY_ICON + "</span><br>";
                }}
                popupHtml += "<b>" + GFMY_LABELS.coordinates + "</b> " + coordDisplay +
                    " <span class='gfmy-copy gfmy-copy-coords' role='button' tabindex='0'" +
                    " title='" + GFMY_LABELS.copy_to_clipboard + "'" +
                    " aria-label='" + GFMY_LABELS.copy_coordinates + "'>" +
                    GFMY_COPY_ICON + "</span>";

                // autoPan: false keeps the auto-opened popup from panning the map
                // away from fitBounds (see plan risk: openPopup autoPan jump).
                marker.bindPopup(popupHtml, {{autoPan: false}});

                // LAYER 3 lifecycle: the single focus disc follows the popup.
                // Copy icons are wired here (popupopen): the handlers read the
                // raw value from the ``loc`` closure, never from the rendered
                // DOM, so no value is round-tripped through markup (OWASP H1).
                marker.on('popupopen', function(e) {{
                    setFocus(loc, [loc.lat, loc.lon]);
                    var popupEl = e.popup.getElement();
                    if (!popupEl) {{ return; }}
                    var pcBtn = popupEl.querySelector('.gfmy-copy-plus');
                    if (pcBtn && loc.plus_code) {{
                        pcBtn.onclick = function(ev) {{
                            ev.stopPropagation();
                            ev.preventDefault();
                            gfmyCopyToClipboard(loc.plus_code, pcBtn);
                        }};
                    }}
                    var coBtn = popupEl.querySelector('.gfmy-copy-coords');
                    if (coBtn) {{
                        coBtn.onclick = function(ev) {{
                            ev.stopPropagation();
                            ev.preventDefault();
                            gfmyCopyToClipboard(loc.lat + ", " + loc.lon, coBtn);
                        }};
                    }}
                }});
                marker.on('popupclose', function() {{
                    if (focusCircle) {{ map.removeLayer(focusCircle); focusCircle = null; }}
                }});

                markers.addLayer(marker);
                bounds.extend([loc.lat, loc.lon]);
                // Focus every real-accuracy point; because locations are sorted
                // oldest->newest, the last assignment wins and selects the newest
                // non-estimated point even when the newest point overall is an
                // estimated fallback.
                if (!loc.accuracy_estimated) {{ autoFocusMarker = marker; }}
            }});

            if (locations.length > 0) {{
                map.fitBounds(bounds, {{padding: [50, 50]}});
            }}
            // Auto-focus the newest real-accuracy point (last non-estimated wins,
            // oldest->newest sort), as if it were clicked.
            if (autoFocusMarker) {{ autoFocusMarker.openPopup(); }}
        }}

        function applyFilters() {{
            var parsed = new Date(document.getElementById('start').value);
            var endParsed = new Date(document.getElementById('end').value);
            var start = parsed.toISOString();
            var end = endParsed.toISOString();
            var acc = document.getElementById('accuracy').value;

            var url = new URL(window.location);
            url.searchParams.set('start', start);
            url.searchParams.set('end', end);
            url.searchParams.set('accuracy', acc);
            window.location = url;
        }}

        drawMap();
    </script>
</body>
</html>"""


# ------------------------------ Redirect View -------------------------------


class GoogleFindMyMapRedirectView(HomeAssistantView):
    """View to redirect to appropriate map URL based on request origin."""

    url = "/api/googlefindmy/redirect_map/{device_id}"
    name = "api:googlefindmy:redirect_map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the Home Assistant instance to the redirect view."""
        super().__init__()
        self.hass = hass

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Redirect to the map path using a **relative** Location header.

        Why relative?
        - Browser resolves against the current origin (proxy/cloud friendly).
        - Avoids computing or persisting absolute base URLs.
        - RFC 9110 allows a URI reference in Location (relative is valid).
        """
        # Require token but do not echo it back in logs.
        auth_token = request.query.get("token")
        if not auth_token:
            return _html_response(
                "Bad Request", "Missing authentication token.", status=400
            )

        # Preserve all query parameters (incl. start/end/accuracy/token) in the redirect.
        # Build a relative URL so the browser keeps the current origin automatically.
        query_dict = dict(request.query.items())
        redirect_url = f"/api/googlefindmy/map/{device_id}?{urlencode(query_dict)}"
        _LOGGER.debug("Relative redirect prepared for device_id=%s", device_id)

        raise web.HTTPFound(location=redirect_url)

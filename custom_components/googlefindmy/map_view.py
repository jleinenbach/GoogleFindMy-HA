# custom_components/googlefindmy/map_view.py
"""Map view for Google Find My Device locations."""

from __future__ import annotations

import json
import logging
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

_LOGGER = logging.getLogger(__name__)

_COORDINATOR_CLASS: type[Any] | None = None


def _resolve_coordinator_class() -> type[Any]:
    """Import the coordinator lazily to avoid pulling in HTTP at import time."""

    global _COORDINATOR_CLASS
    if _COORDINATOR_CLASS is None:
        from .coordinator import GoogleFindMyCoordinator as _GoogleFindMyCoordinator

        _COORDINATOR_CLASS = _GoogleFindMyCoordinator
    return _COORDINATOR_CLASS


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
            return _html_response("Unauthorized", "Missing authentication token.", status=401)

        entry, _accepted = _resolve_entry_by_token(self.hass, auth_token)
        if not entry:
            _LOGGER.debug("Map token mismatch for device_id=%s", device_id)
            return _html_response("Unauthorized", "Invalid authentication token.", status=401)

        # 2. Resolve Device Name (Best effort from Coordinator)
        # We lazily resolve the coordinator to get the friendly name
        coordinator_cls = _resolve_coordinator_class()
        runtime = getattr(entry, "runtime_data", None)
        device_name: str | None = None

        # Try to find device in the entry's coordinator data
        if runtime:
            coordinator = runtime if isinstance(runtime, coordinator_cls) else getattr(runtime, "coordinator", None)
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
            f"{DOMAIN}_{device_id}"
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
                    if entity.platform == DOMAIN and entity.config_entry_id == entry.entry_id:
                        if entity.unique_id.endswith(f":{device_id}") or entity.unique_id.endswith(f"_{device_id}"):
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
            device_name = "Unknown Device"

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
        if entity_id:
            from homeassistant.components.recorder.history import get_significant_states

            try:
                # Run heavy DB query in executor
                history = await self.hass.async_add_executor_job(
                    get_significant_states, self.hass, start_time, end_time, [entity_id]
                )

                if entity_id in history:
                    for state in history[entity_id]:
                        try:
                            lat = float(state.attributes.get("latitude"))
                            lon = float(state.attributes.get("longitude"))
                            acc = float(state.attributes.get("gps_accuracy", 0))

                            # Determine timestamp (prefer last_seen attribute if available for precision)
                            ts = state.last_updated.timestamp()
                            raw_last_seen = state.attributes.get("last_seen")
                            if raw_last_seen is not None:
                                try:
                                    ts = float(raw_last_seen)
                                except (ValueError, TypeError):
                                    if isinstance(raw_last_seen, str):
                                        try:
                                            ts = datetime.fromisoformat(
                                                raw_last_seen.replace("Z", "+00:00")
                                            ).timestamp()
                                        except ValueError:
                                            pass

                            if ts in seen_timestamps:
                                continue

                            seen_timestamps.add(ts)
                            locations.append({
                                "lat": lat,
                                "lon": lon,
                                "accuracy": acc,
                                "timestamp": datetime.fromtimestamp(ts, tz=dt_util.UTC).isoformat(),
                                "last_seen": ts,
                                "is_own_report": state.attributes.get("is_own_report"),
                                "semantic_location": state.attributes.get("semantic_name"),
                            })
                        except (ValueError, TypeError, KeyError):
                            continue  # Skip invalid states
            except Exception as err:  # pragma: no cover - log only
                _LOGGER.warning("Failed to fetch history for map: %s", err)

        locations.sort(key=lambda location: location.get("last_seen", 0))

        # 6. Render
        html = self._generate_map_html(device_name, locations, device_id, start_time, end_time, accuracy_filter)
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

        return f"""<!DOCTYPE html>
<html>
<head>
    <title>{escape(device_name)} - Location History</title>
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
        .control-group {{ margin-bottom: 10px; }}
        label {{ display: block; font-size: 12px; font-weight: bold; color: #333; margin-bottom: 4px; }}
        input[type="datetime-local"], input[type="range"] {{ width: 100%; padding: 5px; border: 1px solid #ddd; border-radius: 4px; }}
        button {{
            background: #007bff; color: white; border: none; padding: 8px 15px;
            border-radius: 4px; cursor: pointer; width: 100%; font-weight: bold;
        }}
        button:hover {{ background: #0056b3; }}
        .stats {{ font-size: 12px; color: #666; text-align: center; margin-top: 10px; border-top: 1px solid #eee; padding-top: 5px; }}
    </style>
</head>
<body>
    <div class="controls">
        <h3>{escape(device_name)}</h3>
        <div class="control-group">
            <label>Start Time</label>
            <input type="datetime-local" id="start" value="{start_local}">
        </div>
        <div class="control-group">
            <label>End Time</label>
            <input type="datetime-local" id="end" value="{end_local}">
        </div>
        <div class="control-group">
            <label>Min Accuracy (meters): <span id="acc-val">{accuracy_filter}</span></label>
            <input type="range" id="accuracy" min="0" max="500" step="10" value="{accuracy_filter}" oninput="document.getElementById('acc-val').innerText = this.value">
        </div>
        <button onclick="applyFilters()">Apply Filters</button>
        <div class="stats">
            Showing {len(locations)} points
        </div>
    </div>
    <div id="map"></div>

    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        var map = L.map('map').setView([{center_lat}, {center_lon}], 13);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '© OpenStreetMap contributors'
        }}).addTo(map);

        var locations = {locations_json};
        var markers = L.layerGroup().addTo(map);

        function toLocalInputValue(date) {{
            var year = date.getFullYear();
            var month = String(date.getMonth() + 1).padStart(2, '0');
            var day = String(date.getDate()).padStart(2, '0');
            var hours = String(date.getHours()).padStart(2, '0');
            var minutes = String(date.getMinutes()).padStart(2, '0');
            return year + '-' + month + '-' + day + 'T' + hours + ':' + minutes;
        }}

        function drawMap() {{
            markers.clearLayers();
            var bounds = L.latLngBounds();

            locations.forEach(function(loc, idx) {{
                var color = loc.is_own_report ? '#28a745' : '#007bff';
                var opacity = 1.0 - (idx / locations.length * 0.5); // Fade older points

                var marker = L.circleMarker([loc.lat, loc.lon], {{
                    radius: 6,
                    color: '#fff',
                    weight: 1,
                    fillColor: color,
                    fillOpacity: 0.8
                }});

                var date = new Date(loc.timestamp).toLocaleString();
                var source = loc.is_own_report ? "Own Device" : "Crowdsourced";

                marker.bindPopup(
                    "<b>Time:</b> " + date + "<br>" +
                    "<b>Accuracy:</b> " + loc.accuracy.toFixed(1) + "m<br>" +
                    "<b>Source:</b> " + source + "<br>" +
                    (loc.semantic_location ? "<b>Location:</b> " + loc.semantic_location : "")
                );

                markers.addLayer(marker);
                bounds.extend([loc.lat, loc.lon]);
            }});

            if (locations.length > 0) {{
                map.fitBounds(bounds, {{padding: [50, 50]}});
            }}
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

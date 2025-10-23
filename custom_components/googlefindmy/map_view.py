# custom_components/googlefindmy/map_view.py
"""Map view for Google Find My Device locations."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .coordinator import GoogleFindMyCoordinator

from .const import (
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    WEEK_SECONDS,
    map_token_hex_digest,
    map_token_secret_seed,
)

_LOGGER = logging.getLogger(__name__)


# ------------------------------- HTML Helpers -------------------------------


def _html_response(title: str, body: str, status: int = 200) -> web.Response:
    """Return a minimal HTML response (no secrets, no stacktraces)."""
    return web.Response(
        text=f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
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


def _resolve_entry_by_token(hass: HomeAssistant, auth_token: str):
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
    """View to serve device location maps."""

    url = "/api/googlefindmy/map/{device_id}"
    name = "api:googlefindmy:map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the map view."""
        self.hass = hass

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Generate and serve a map for the device.

        Security notes:
        - Token validation is **entry-scoped** (includes entry_id in the token).
        - Weekly tokens accept current and previous bucket to survive boundary flips.
        - We never log tokens or full URLs that include tokens.
        """
        # ---- 1) Token check & entry resolution (401 on missing/invalid) ----
        auth_token = request.query.get("token")
        if not auth_token:
            return _html_response(
                "Unauthorized", "Missing authentication token.", status=401
            )

        entry, _accepted = _resolve_entry_by_token(self.hass, auth_token)
        if not entry:
            _LOGGER.debug(
                "Map token mismatch (no entry resolved) for device_id=%s", device_id
            )
            return _html_response(
                "Unauthorized", "Invalid authentication token.", status=401
            )

        # ---- 2) Coordinator + device membership check (404 if unknown in this entry) ----
        runtime = getattr(entry, "runtime_data", None)
        coordinator: GoogleFindMyCoordinator | None = None
        if isinstance(runtime, GoogleFindMyCoordinator):
            coordinator = runtime
        elif runtime is not None:
            coordinator = getattr(runtime, "coordinator", None)

        if not isinstance(coordinator, GoogleFindMyCoordinator):
            _LOGGER.debug("Coordinator not found for entry_id=%s", entry.entry_id)
            return _html_response("Server Error", "Integration not ready.", status=503)

        data_list = getattr(coordinator, "data", None) or []
        device_known = any(dev.get("id") == device_id for dev in data_list)
        if not device_known:
            _LOGGER.debug(
                "Map requested for unknown device_id=%s in entry_id=%s",
                device_id,
                entry.entry_id,
            )
            return _html_response("Not Found", "Device not found.", status=404)

        try:
            # ---- 3) Resolve a human-readable device name from THIS coordinator snapshot ----
            device_name = (
                next(
                    (d.get("name") for d in data_list if d.get("id") == device_id), None
                )
                or "Unknown Device"
            )

            # ---- 4) Find the device_tracker entity (entity registry, scoped to this entry) ----
            entity_registry = er.async_get(self.hass)
            entity_id: str | None = None
            entry_unique_id_candidates: list[str] = []

            entry_id = entry.entry_id
            if entry_id:
                entry_unique_id_candidates.append(f"{entry_id}:{device_id}")
                entry_unique_id_candidates.append(f"{DOMAIN}_{entry_id}_{device_id}")
            entry_unique_id_candidates.append(f"{DOMAIN}_{device_id}")

            for unique_id in entry_unique_id_candidates:
                candidate_entity_id = entity_registry.async_get_entity_id(
                    "device_tracker", DOMAIN, unique_id
                )
                if not candidate_entity_id:
                    continue

                registry_entry = entity_registry.async_get(candidate_entity_id)
                if registry_entry and registry_entry.config_entry_id == entry.entry_id:
                    entity_id = candidate_entity_id
                    break

            if not entity_id:
                # Fallback to a guess; page will render "no history" if none found, which is acceptable UX.
                entity_id = f"device_tracker.{device_id.replace('-', '_').lower()}"
                _LOGGER.debug(
                    "No explicit tracker entity found for %s in entry_id=%s, using guess %s",
                    device_id,
                    entry.entry_id,
                    entity_id,
                )

            # ---- 5) Parse time range and accuracy filter from query ----
            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(days=7)  # default: 7 days

            start_param = request.query.get("start")
            end_param = request.query.get("end")
            accuracy_param = request.query.get("accuracy", "0")

            if start_param:
                try:
                    start_time = datetime.fromisoformat(
                        start_param.replace("Z", "+00:00")
                    )
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=dt_util.UTC)
                except ValueError:
                    pass  # keep default

            if end_param:
                try:
                    end_time = datetime.fromisoformat(end_param.replace("Z", "+00:00"))
                    if end_time.tzinfo is None:
                        end_time = end_time.replace(tzinfo=dt_util.UTC)
                except ValueError:
                    pass  # keep default

            # If user swapped values (or end < start), clamp to a sane 7-day window ending at end_time.
            if end_time < start_time:
                start_time = end_time - timedelta(days=7)

            try:
                accuracy_filter = max(0, min(300, int(accuracy_param)))
            except (ValueError, TypeError):
                accuracy_filter = 0

            # ---- 6) Query Recorder history for location points (single entity) ----
            from homeassistant.components.recorder.history import get_significant_states

            history = await self.hass.async_add_executor_job(
                get_significant_states, self.hass, start_time, end_time, [entity_id]
            )

            locations: list[dict[str, Any]] = []
            if entity_id in history:
                last_seen_epoch: float | None = None
                for state in history[entity_id]:
                    lat_raw = state.attributes.get("latitude")
                    lon_raw = state.attributes.get("longitude")
                    if lat_raw is None or lon_raw is None:
                        continue
                    try:
                        lat = float(lat_raw)
                        lon = float(lon_raw)
                    except (TypeError, ValueError):
                        continue

                    last_seen_attr = state.attributes.get("last_seen")
                    last_seen_utc_attr = state.attributes.get("last_seen_utc")
                    current_last_seen_dt: datetime | None = None

                    for candidate in (last_seen_utc_attr, last_seen_attr):
                        if candidate is None:
                            continue
                        if isinstance(candidate, datetime):
                            current_last_seen_dt = candidate
                        elif isinstance(candidate, (int, float)):
                            try:
                                current_last_seen_dt = datetime.fromtimestamp(
                                    float(candidate), tz=dt_util.UTC
                                )
                            except (OSError, OverflowError, ValueError, TypeError):
                                current_last_seen_dt = None
                        elif isinstance(candidate, str):
                            parsed_candidate = dt_util.parse_datetime(candidate)
                            if parsed_candidate is not None:
                                current_last_seen_dt = parsed_candidate
                        if current_last_seen_dt is not None:
                            break

                    if current_last_seen_dt is None:
                        current_last_seen_dt = state.last_updated
                    elif current_last_seen_dt.tzinfo is None:
                        current_last_seen_dt = current_last_seen_dt.replace(
                            tzinfo=dt_util.UTC
                        )

                    current_last_seen_dt = dt_util.as_utc(current_last_seen_dt)
                    current_last_seen_epoch = current_last_seen_dt.timestamp()

                    if (
                        last_seen_epoch is not None
                        and current_last_seen_epoch == last_seen_epoch
                    ):
                        # de-dupe by identical last_seen
                        continue
                    last_seen_epoch = current_last_seen_epoch

                    acc_raw = state.attributes.get("gps_accuracy", 0)
                    try:
                        acc = max(0.0, float(acc_raw))
                    except (TypeError, ValueError):
                        acc = 0.0

                    locations.append(
                        {
                            "lat": lat,
                            "lon": lon,
                            "accuracy": acc,
                            "timestamp": state.last_updated.isoformat(),
                            "last_seen": current_last_seen_epoch,
                            "entity_id": entity_id,
                            "state": state.state,
                            "is_own_report": state.attributes.get("is_own_report"),
                            # Harmonize with coordinator attributes: use 'semantic_name'
                            "semantic_location": state.attributes.get("semantic_name"),
                        }
                    )

            # ---- 7) Render HTML (no secrets) ----
            html_content = self._generate_map_html(
                device_name, locations, device_id, start_time, end_time, accuracy_filter
            )
            return web.Response(
                text=html_content, content_type="text/html", charset="utf-8"
            )

        except Exception as err:  # defensive: HTML error page instead of raw tracebacks
            _LOGGER.error("Error generating map for device %s: %s", device_id, err)
            return _html_response("Server Error", "Error generating map.", status=500)

    # ---------------------------- HTML builder ----------------------------

    def _generate_map_html(
        self,
        device_name: str,
        locations: list[dict[str, Any]],
        device_id: str,
        start_time: datetime,
        end_time: datetime,
        accuracy_filter: int = 0,
    ) -> str:
        """Generate HTML content for the map."""
        # Format times for display - convert to Home Assistant's local timezone
        start_local_tz = dt_util.as_local(start_time)
        end_local_tz = dt_util.as_local(end_time)
        start_local = start_local_tz.strftime("%Y-%m-%dT%H:%M")
        end_local = end_local_tz.strftime("%Y-%m-%dT%H:%M")

        device_name_html = escape(device_name, quote=True)
        start_local_attr = escape(start_local, quote=True)
        end_local_attr = escape(end_local, quote=True)
        accuracy_attr = escape(str(accuracy_filter), quote=True)

        start_query_encoded = escape(quote(start_time.isoformat()), quote=True)
        end_query_encoded = escape(quote(end_time.isoformat()), quote=True)
        accuracy_query_encoded = escape(quote(str(accuracy_filter)), quote=True)

        if not locations:
            # Empty state page with controls for time range selection
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{device_name_html} - Location Map</title>
                <meta charset="utf-8" />
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .controls {{ background: #f5f5f5; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
                    .time-control {{ margin: 10px 0; }}
                    label {{ display: inline-block; width: 120px; font-weight: bold; }}
                    input[type="datetime-local"] {{ padding: 8px; border: 1px solid #ccc; border-radius: 4px; width: 200px; }}
                    button {{ padding: 10px 20px; background: #007cba; color: white; border: none; border-radius: 4px; cursor: pointer; margin: 0 5px; }}
                    button:hover {{ background: #005a8b; }}
                    .quick-buttons {{ margin: 10px 0; }}
                    .quick-buttons button {{ background: #6c757d; }}
                    .quick-buttons button:hover {{ background: #5a6268; }}
                    .message {{ text-align: center; margin-top: 20px; color: #666; }}
                </style>
            </head>
            <body>
                <h1>{device_name_html}</h1>
                <div class="controls" id="mapControls" data-initial-start="{start_query_encoded}" data-initial-end="{end_query_encoded}">
                    <h3>Select Time Range</h3>
                    <div class="time-control">
                        <label for="startTime">Start:</label>
                        <input type="datetime-local" id="startTime" value="{start_local_attr}">
                    </div>
                    <div class="time-control">
                        <label for="endTime">End:</label>
                        <input type="datetime-local" id="endTime" value="{end_local_attr}">
                    </div>
                    <div class="quick-buttons">
                        <button onclick="setQuickRange(1)">Last 1 Day</button>
                        <button onclick="setQuickRange(3)">Last 3 Days</button>
                        <button onclick="setQuickRange(7)">Last 7 Days</button>
                        <button onclick="setQuickRange(14)">Last 14 Days</button>
                        <button onclick="setQuickRange(30)">Last 30 Days</button>
                    </div>
                    <button onclick="updateMap()">Update Map</button>
                </div>
                <div class="message">
                    <p>No location history available for the selected time range.</p>
                    <p>Try expanding the date range or check if the device has been active.</p>
                </div>

                <script>
                function updateLocationWithParams(updates) {{
                    const existing = window.location.search.slice(1);
                    const keys = Object.keys(updates);
                    const preserved = existing
                        ? existing.split('&').filter(Boolean).filter((pair) => {{
                            const [rawKey] = pair.split('=');
                            if (!rawKey) {{
                                return false;
                            }}
                            let decodedKey = rawKey;
                            try {{
                                decodedKey = decodeURIComponent(rawKey);
                            }} catch (err) {{
                                decodedKey = rawKey;
                            }}
                            return !keys.includes(decodedKey);
                        }})
                        : [];

                    keys.forEach((key) => {{
                        const value = updates[key];
                        if (value === null || value === undefined) {{
                            return;
                        }}
                        preserved.push(`${{encodeURIComponent(key)}}=${{encodeURIComponent(value)}}`);
                    }});

                    const query = preserved.join('&');
                    const path = window.location.pathname;
                    const hash = window.location.hash || '';
                    const nextUrl = query ? `${{path}}?${{query}}${{hash}}` : `${{path}}{{hash}}`;
                    window.location.href = nextUrl;
                }}

                function setQuickRange(days) {{
                    const end = new Date();
                    const start = new Date(end.getTime() - (days * 24 * 60 * 60 * 1000));

                    document.getElementById('endTime').value = formatDateTime(end);
                    document.getElementById('startTime').value = formatDateTime(start);
                }}

                function formatDateTime(date) {{
                    const year = date.getFullYear();
                    const month = String(date.getMonth() + 1).padStart(2, '0');
                    const day = String(date.getDate()).padStart(2, '0');
                    const hours = String(date.getHours()).padStart(2, '0');
                    const minutes = String(date.getMinutes()).padStart(2, '0');
                    return `${{year}}-${{month}}-${{day}}T${{hours}}:${{minutes}}`;
                }}

                function updateMap() {{
                    const startField = document.getElementById('startTime');
                    const endField = document.getElementById('endTime');
                    const startValue = startField ? startField.value : '';
                    const endValue = endField ? endField.value : '';

                    if (!startValue || !endValue) {{
                        alert('Please select both start and end times');
                        return;
                    }}

                    const startDate = new Date(startValue);
                    const endDate = new Date(endValue);

                    if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) {{
                        alert('Please provide valid start and end times');
                        return;
                    }}

                    updateLocationWithParams({{
                        start: startDate.toISOString(),
                        end: endDate.toISOString(),
                    }});
                }}

                document.addEventListener('DOMContentLoaded', function() {{
                    const controls = document.getElementById('mapControls');
                    if (controls) {{
                        const startField = document.getElementById('startTime');
                        const endField = document.getElementById('endTime');
                        const startAttr = controls.dataset.initialStart;
                        const endAttr = controls.dataset.initialEnd;

                        if (startAttr && startField && !startField.value) {{
                            const decodedStart = decodeURIComponent(startAttr);
                            const parsedStart = new Date(decodedStart);
                            if (!Number.isNaN(parsedStart.getTime())) {{
                                startField.value = formatDateTime(parsedStart);
                            }} else {{
                                startField.value = decodedStart.slice(0, 16);
                            }}
                        }}

                        if (endAttr && endField && !endField.value) {{
                            const decodedEnd = decodeURIComponent(endAttr);
                            const parsedEnd = new Date(decodedEnd);
                            if (!Number.isNaN(parsedEnd.getTime())) {{
                                endField.value = formatDateTime(parsedEnd);
                            }} else {{
                                endField.value = decodedEnd.slice(0, 16);
                            }}
                        }}
                    }}
                }});
                </script>
            </body>
            </html>
            """

        # Calculate center point
        center_lat = sum(loc["lat"] for loc in locations) / len(locations)
        center_lon = sum(loc["lon"] for loc in locations) / len(locations)

        # Generate markers JavaScript
        markers_js: list[str] = []
        for i, loc in enumerate(locations):
            accuracy = float(loc.get("accuracy", 0.0))
            lat = float(loc.get("lat", 0.0))
            lon = float(loc.get("lon", 0.0))

            # Color based on accuracy
            if accuracy <= 5:
                color = "green"
            elif accuracy <= 20:
                color = "orange"
            else:
                color = "red"

            # Convert UTC timestamp to Home Assistant timezone
            timestamp_utc = datetime.fromisoformat(
                str(loc["timestamp"]).replace("Z", "+00:00")
            )
            timestamp_local = dt_util.as_local(timestamp_utc)

            # Determine report source
            is_own_report = loc.get("is_own_report")
            if is_own_report is True:
                report_source = "📱 Own Device"
                report_color = "#28a745"  # Green
            elif is_own_report is False:
                report_source = "🌐 Network/Crowd-sourced"
                report_color = "#007cba"  # Blue
            else:
                report_source = "❓ Unknown"
                report_color = "#6c757d"  # Gray

            timestamp_display = escape(
                timestamp_local.strftime("%Y-%m-%d %H:%M:%S %Z"), quote=True
            )
            report_source_html = escape(report_source, quote=True)
            semantic_location = loc.get("semantic_location")
            entity_id_text = escape(str(loc.get("entity_id", "Unknown")), quote=True)
            state_text = escape(str(loc.get("state", "Unknown")), quote=True)

            popup_parts = [
                f"<b>Location {i + 1}</b><br>",
                f"<b>Coordinates:</b> {lat:.6f}, {lon:.6f}<br>",
                f"<b>GPS Accuracy:</b> {accuracy:.1f} meters<br>",
                f"<b>Timestamp:</b> {timestamp_display}<br>",
                (
                    f'<b style="color: {report_color}">Report Source:</b> '
                    f'<span style="color: {report_color}">{report_source_html}</span><br>'
                ),
            ]
            if semantic_location:
                popup_parts.append(
                    f"<b>Location Name:</b> {escape(str(semantic_location), quote=True)}<br>"
                )
            popup_parts.append(f"<b>Entity ID:</b> {entity_id_text}<br>")
            popup_parts.append(f"<b>Entity State:</b> {state_text}<br>")

            popup_html = "".join(popup_parts)
            popup_js = json.dumps(popup_html)
            tooltip_js = json.dumps(f"Accuracy: {accuracy:.1f}m")

            markers_js.append(
                f"""
                var marker_{i} = L.marker([{lat}, {lon}]);
                marker_{i}.accuracy = {accuracy};
                marker_{i}.bindPopup({popup_js});
                marker_{i}.bindTooltip({tooltip_js});
                marker_{i}.addTo(map);

                var circle_{i} = L.circle([{lat}, {lon}], {{
                    radius: {accuracy},
                    color: '{color}',
                    fillColor: '{color}',
                    fillOpacity: 0.1
                }});
                circle_{i}.accuracy = {accuracy};
                circle_{i}.addTo(map);
            """
            )

        markers_code = "\n".join(markers_js)

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{device_name_html} - Location Map</title>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <style>
                body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
                #map {{ height: 100vh; width: 100%; }}
                .filter-panel {{
                    position: absolute; top: 10px; right: 10px; z-index: 1000;
                    background: white; padding: 15px; border-radius: 8px;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.4);
                    max-width: 380px; font-size: 13px;
                }}
                .filter-panel.collapsed {{ padding: 8px 12px; max-width: 120px; }}
                .filter-panel.collapsed .filter-content {{ display: none; }}
                .filter-content {{ margin-top: 10px; }}
                .filter-section {{ margin: 12px 0; padding: 8px 0; border-bottom: 1px solid #eee; }}
                .filter-section:last-child {{ border-bottom: none; }}
                .filter-control {{ margin: 8px 0; display: flex; align-items: center; }}
                .filter-control label {{
                    display: inline-block; width: 70px; font-size: 12px;
                    font-weight: bold; margin-right: 8px;
                }}
                .filter-control input {{
                    padding: 4px; border: 1px solid #ccc; border-radius: 3px;
                    width: 150px; font-size: 11px;
                }}
                .accuracy-control {{ margin: 10px 0; }}
                .accuracy-control label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .slider-container {{ display: flex; align-items: center; gap: 8px; }}
                .accuracy-slider {{
                    flex: 1; height: 6px; background: #ddd; border-radius: 3px;
                    outline: none; cursor: pointer;
                }}
                .accuracy-value {{
                    min-width: 60px; font-size: 11px; font-weight: bold;
                    color: #007cba;
                }}
                .filter-panel button {{
                    padding: 6px 12px; background: #007cba; color: white;
                    border: none; border-radius: 4px; cursor: pointer;
                    margin: 2px; font-size: 11px;
                }}
                .filter-panel button:hover {{ background: #005a8b; }}
                .toggle-btn {{ background: #28a745 !important; }}
                .toggle-btn:hover {{ background: #218838 !important; }}
                .update-btn {{ background: #dc3545 !important; width: 100%; margin-top: 8px; }}
                .update-btn:hover {{ background: #c82333 !important; }}
                h2 {{ margin: 0 0 8px 0; font-size: 16px; }}
                .info {{ margin: 5px 0; font-size: 12px; color: #666; }}
                .current-time {{
                    margin: 8px 0; font-size: 11px; color: #007cba;
                    font-weight: bold; padding: 4px 8px;
                    background: #f8f9fa; border-radius: 4px;
                    border-left: 3px solid #007cba;
                }}
                .leaflet-control-zoom {{ z-index: 1500 !important; }}
            </style>
        </head>
        <body>
            <div class="filter-panel collapsed" id="filterPanel" data-initial-start="{start_query_encoded}" data-initial-end="{end_query_encoded}" data-initial-accuracy="{accuracy_query_encoded}">
                <button class="toggle-btn" onclick="toggleFilters()">📅 Filters</button>

                <div class="filter-content" id="filterContent">
                    <h2>{device_name_html}</h2>
                    <div class="info">{len(locations)} locations shown</div>
                    <div class="current-time" id="currentTime">🕐 Loading current time...</div>

                    <div class="filter-section">
                        <div class="filter-control">
                            <label for="startTime">Start:</label>
                            <input type="datetime-local" id="startTime" value="{start_local_attr}">
                        </div>
                        <div class="filter-control">
                            <label for="endTime">End:</label>
                            <input type="datetime-local" id="endTime" value="{end_local_attr}">
                        </div>
                    </div>

                    <div class="filter-section">
                        <div class="accuracy-control">
                            <label for="accuracySlider">Accuracy Filter:</label>
                            <div class="slider-container">
                                <input type="range" id="accuracySlider" class="accuracy-slider"
                                       min="0" max="300" value="{accuracy_attr}" oninput="updateAccuracyFilter()">
                                <span class="accuracy-value" id="accuracyValue">Disabled</span>
                            </div>
                        </div>
                    </div>

                    <button class="update-btn" onclick="updateMap()">🔄 Update Map</button>
                </div>
            </div>
            <div id="map"></div>

            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script>
                function updateLocationWithParams(updates) {{
                    const queryString = window.location.search.slice(1);
                    const keys = Object.keys(updates);
                    const preserved = queryString
                        ? queryString.split('&').filter(Boolean).filter((pair) => {{
                            const [rawKey] = pair.split('=');
                            if (!rawKey) {{
                                return false;
                            }}
                            let decodedKey = rawKey;
                            try {{
                                decodedKey = decodeURIComponent(rawKey);
                            }} catch (err) {{
                                decodedKey = rawKey;
                            }}
                            return !keys.includes(decodedKey);
                        }})
                        : [];

                    keys.forEach((key) => {{
                        const value = updates[key];
                        if (value === null || value === undefined) {{
                            return;
                        }}
                        preserved.push(`${{encodeURIComponent(key)}}=${{encodeURIComponent(value)}}`);
                    }});

                    const query = preserved.join('&');
                    const basePath = window.location.pathname;
                    const hash = window.location.hash || '';
                    const destination = query ? `${{basePath}}?${{query}}${{hash}}` : `${{basePath}}{{hash}}`;
                    window.location.href = destination;
                }}

                var map = L.map('map').setView([{center_lat}, {center_lon}], 13);

                L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                    attribution: '© OpenStreetMap contributors'
                }}).addTo(map);

                var allMarkers = [];
                var allCircles = [];

                {markers_code}

                map.eachLayer(function(layer) {{
                    if (layer instanceof L.Marker && layer.accuracy !== undefined) {{
                        allMarkers.push(layer);
                    }} else if (layer instanceof L.Circle && layer.accuracy !== undefined) {{
                        allCircles.push(layer);
                    }}
                }});

                var group = new L.featureGroup();
                allMarkers.forEach(function(marker) {{
                    if (map.hasLayer(marker)) {{
                        group.addLayer(marker);
                    }}
                }});
                if (group.getLayers().length > 0) {{
                    map.fitBounds(group.getBounds().pad(0.1));
                }}

                function toggleFilters() {{
                    const panel = document.getElementById('filterPanel');
                    panel.classList.toggle('collapsed');
                }}

                function updateAccuracyFilter() {{
                    const slider = document.getElementById('accuracySlider');
                    const valueSpan = document.getElementById('accuracyValue');
                    const value = parseInt(slider.value);

                    if (value === 0) {{
                        valueSpan.textContent = 'Disabled';
                        valueSpan.style.color = '#6c757d';
                    }} else {{
                        valueSpan.textContent = value + 'm';
                        valueSpan.style.color = '#007cba';
                    }}

                    filterMarkersByAccuracy(value);
                }}

                function filterMarkersByAccuracy(maxAccuracy) {{
                    if (allMarkers.length === 0 || allCircles.length === 0) {{
                        map.eachLayer(function(layer) {{
                            if (layer instanceof L.Marker && layer.accuracy !== undefined) {{
                                allMarkers.push(layer);
                            }} else if (layer instanceof L.Circle && layer.accuracy !== undefined) {{
                                allCircles.push(layer);
                            }}
                        }});
                    }}

                    allMarkers.forEach(function(marker) {{
                        if (maxAccuracy === 0 || marker.accuracy <= maxAccuracy) {{
                            if (!map.hasLayer(marker)) {{ marker.addTo(map); }}
                        }} else {{
                            if (map.hasLayer(marker)) {{ map.removeLayer(marker); }}
                        }}
                    }});

                    allCircles.forEach(function(circle) {{
                        if (maxAccuracy === 0 || circle.accuracy <= maxAccuracy) {{
                            if (map.hasLayer(circle)) {{ circle.addTo(map); }}
                        }} else {{
                            if (map.hasLayer(circle)) {{ map.removeLayer(circle); }}
                        }}
                    }});

                    var visibleCount = allMarkers.filter(function(m) {{ return map.hasLayer(m); }}).length;
                    var infoElement = document.querySelector('.info');
                    if (infoElement) {{ infoElement.textContent = visibleCount + ' locations shown'; }}
                }}

                function setQuickRange(days) {{
                    const end = new Date();
                    const start = new Date(end.getTime() - (days * 24 * 60 * 60 * 1000));
                    document.getElementById('endTime').value = formatDateTime(end);
                    document.getElementById('startTime').value = formatDateTime(start);
                }}

                function formatDateTime(date) {{
                    const year = date.getFullYear();
                    const month = String(date.getMonth() + 1).padStart(2, '0');
                    const day = String(date.getDate()).padStart(2, '0');
                    const hours = String(date.getHours()).padStart(2, '0');
                    const minutes = String(date.getMinutes()).padStart(2, '0');
                    return `${{year}}-${{month}}-${{day}}T${{hours}}:${{minutes}}`;
                }}

                function updateMap() {{
                    const startField = document.getElementById('startTime');
                    const endField = document.getElementById('endTime');
                    const accuracyField = document.getElementById('accuracySlider');
                    const startValue = startField ? startField.value : '';
                    const endValue = endField ? endField.value : '';
                    const accuracyFilter = accuracyField ? accuracyField.value : '';

                    if (!startValue || !endValue) {{
                        alert('Please select both start and end times');
                        return;
                    }}

                    const startDate = new Date(startValue);
                    const endDate = new Date(endValue);

                    if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) {{
                        alert('Please provide valid start and end times');
                        return;
                    }}

                    const updates = {{
                        start: startDate.toISOString(),
                        end: endDate.toISOString(),
                    }};
                    if (parseInt(accuracyFilter, 10) > 0) {{
                        updates.accuracy = accuracyFilter;
                    }} else {{
                        updates.accuracy = null;
                    }}

                    updateLocationWithParams(updates);
                }}

                document.addEventListener('DOMContentLoaded', function() {{
                    const filterPanel = document.getElementById('filterPanel');
                    if (filterPanel) {{
                        const startField = document.getElementById('startTime');
                        const endField = document.getElementById('endTime');
                        const startAttr = filterPanel.dataset.initialStart;
                        const endAttr = filterPanel.dataset.initialEnd;
                        const accuracyAttr = filterPanel.dataset.initialAccuracy;

                        if (startAttr && startField && !startField.value) {{
                            const decodedStart = decodeURIComponent(startAttr);
                            const parsedStart = new Date(decodedStart);
                            if (!Number.isNaN(parsedStart.getTime())) {{
                                startField.value = formatDateTime(parsedStart);
                            }} else {{
                                startField.value = decodedStart.slice(0, 16);
                            }}
                        }}
                        if (endAttr && endField && !endField.value) {{
                            const decodedEnd = decodeURIComponent(endAttr);
                            const parsedEnd = new Date(decodedEnd);
                            if (!Number.isNaN(parsedEnd.getTime())) {{
                                endField.value = formatDateTime(parsedEnd);
                            }} else {{
                                endField.value = decodedEnd.slice(0, 16);
                            }}
                        }}
                        if (accuracyAttr) {{
                            const decodedAccuracy = decodeURIComponent(accuracyAttr);
                            const slider = document.getElementById('accuracySlider');
                            if (slider && decodedAccuracy !== '') {{
                                slider.value = decodedAccuracy;
                            }}
                        }}
                    }}

                    updateAccuracyFilter();
                    const sliderValue = parseInt(document.getElementById('accuracySlider').value, 10);
                    if (sliderValue > 0) {{ filterMarkersByAccuracy(sliderValue); }}

                    function updateCurrentTime() {{
                        const now = new Date();
                        const options = {{
                            year: 'numeric', month: 'short', day: 'numeric',
                            hour: '2-digit', minute: '2-digit', second: '2-digit',
                            timeZoneName: 'short'
                        }};
                        document.getElementById('currentTime').textContent = '🕐 ' + now.toLocaleString('en-US', options);
                    }}
                    updateCurrentTime();
                    setInterval(updateCurrentTime, 1000);
                }});
            </script>
        </body>
        </html>
        """


# ------------------------------ Redirect View -------------------------------


class GoogleFindMyMapRedirectView(HomeAssistantView):
    """View to redirect to appropriate map URL based on request origin."""

    url = "/api/googlefindmy/redirect_map/{device_id}"
    name = "api:googlefindmy:redirect_map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the redirect view."""
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
        from urllib.parse import urlencode

        query_dict = dict(request.query.items())
        redirect_url = f"/api/googlefindmy/map/{device_id}?{urlencode(query_dict)}"
        _LOGGER.debug("Relative redirect prepared for device_id=%s", device_id)

        raise web.HTTPFound(location=redirect_url)

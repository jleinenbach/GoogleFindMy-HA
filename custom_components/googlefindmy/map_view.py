"""Map view for Google Find My Device locations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class GoogleFindMyMapView(HomeAssistantView):
    """View to serve device location maps."""

    url = "/api/googlefindmy/map/{device_id}"
    name = "api:googlefindmy:map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the map view."""
        self.hass = hass

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Generate and serve a map for the device."""
        # Simple authentication check via query parameter
        auth_token = request.query.get('token')
        if not auth_token or auth_token != self._get_simple_token():
            return web.Response(
                text="""
                <html>
                <head><title>Access Denied</title></head>
                <body>
                    <h1>Access Denied</h1>
                    <p>This map requires proper authentication.</p>
                    <p>Please access the map through the Home Assistant device page.</p>
                </body>
                </html>
                """,
                content_type='text/html',
                status=403
            )

        try:
            # Get device name from coordinator
            coordinator_data = self.hass.data.get(DOMAIN, {})
            device_name = "Unknown Device"
            _LOGGER.debug(f"Looking for device_id '{device_id}' in coordinator data")

            for entry_id, coordinator in coordinator_data.items():
                if entry_id == "config_data":
                    continue
                if hasattr(coordinator, 'data') and coordinator.data:
                    _LOGGER.debug(f"Coordinator {entry_id} has {len(coordinator.data)} devices")
                    for device in coordinator.data:
                        device_id_in_data = device.get('id')
                        device_name_in_data = device.get('name')
                        _LOGGER.debug(f"Found device id='{device_id_in_data}' name='{device_name_in_data}'")
                        if device_id_in_data == device_id:
                            device_name = device.get('name', 'Unknown Device')
                            _LOGGER.debug(f"Matched device '{device_name}' for id '{device_id}'")
                            break
                else:
                    _LOGGER.debug(f"Coordinator {entry_id} has no data")

            # Get location history from Home Assistant
            entity_id = f"device_tracker.{device_id.replace('-', '_').lower()}"

            # Try to find the actual entity ID
            entity_registry = async_get_entity_registry(self.hass)
            _LOGGER.debug(f"Looking for device_tracker entity for device {device_id}")
            _LOGGER.debug(f"Initial entity_id guess: {entity_id}")

            found_entity = False
            for entity in entity_registry.entities.values():
                if entity.unique_id and device_id in entity.unique_id and entity.platform == "googlefindmy" and entity.entity_id.startswith("device_tracker."):
                    _LOGGER.debug(f"Found matching entity: {entity.entity_id} with unique_id: {entity.unique_id}")
                    entity_id = entity.entity_id
                    found_entity = True
                    break

            if not found_entity:
                _LOGGER.warning(f"No GoogleFindMy device_tracker entity found for device {device_id}")
            else:
                _LOGGER.debug(f"Using entity_id: {entity_id}")

            # Get time range from URL parameters
            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(days=7)  # default 7 days

            # Parse custom start/end times if provided
            start_param = request.query.get('start')
            end_param = request.query.get('end')

            if start_param:
                try:
                    start_time = datetime.fromisoformat(start_param.replace('Z', '+00:00'))
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=dt_util.UTC)
                except ValueError:
                    pass  # Use default if invalid

            if end_param:
                try:
                    end_time = datetime.fromisoformat(end_param.replace('Z', '+00:00'))
                    if end_time.tzinfo is None:
                        end_time = end_time.replace(tzinfo=dt_util.UTC)
                except ValueError:
                    pass  # Use default if invalid

            # Query state history
            from homeassistant.components.recorder.history import get_significant_states

            history = await self.hass.async_add_executor_job(
                get_significant_states,
                self.hass,
                start_time,
                end_time,
                [entity_id]
            )

            locations = []
            if entity_id in history:
                last_seen = None
                for state in history[entity_id]:
                    if (state.attributes.get("latitude") is not None and
                        state.attributes.get("longitude") is not None):

                        # Skip duplicates based on last_seen attribute
                        current_last_seen = state.attributes.get("last_seen")
                        if current_last_seen and current_last_seen == last_seen:
                            continue
                        last_seen = current_last_seen

                        locations.append({
                            "lat": state.attributes["latitude"],
                            "lon": state.attributes["longitude"],
                            "accuracy": state.attributes.get("gps_accuracy", 0),
                            "timestamp": state.last_updated.isoformat(),
                            "last_seen": current_last_seen,
                            "entity_id": entity_id,
                            "state": state.state
                        })

            # Generate HTML map
            html_content = self._generate_map_html(device_name, locations, device_id, start_time, end_time)

            return web.Response(
                text=html_content,
                content_type="text/html",
                charset="utf-8"
            )

        except Exception as e:
            _LOGGER.error(f"Error generating map for device {device_id}: {e}")
            return web.Response(
                text=f"Error generating map: {e}",
                status=500
            )

    def _generate_map_html(self, device_name: str, locations: list[dict[str, Any]], device_id: str, start_time: datetime, end_time: datetime) -> str:
        """Generate HTML content for the map."""
        # Format times for display
        start_local = start_time.strftime('%Y-%m-%dT%H:%M')
        end_local = end_time.strftime('%Y-%m-%dT%H:%M')

        if not locations:
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{device_name} - Location Map</title>
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
                <h1>{device_name}</h1>
                <div class="controls">
                    <h3>Select Time Range</h3>
                    <div class="time-control">
                        <label for="startTime">Start:</label>
                        <input type="datetime-local" id="startTime" value="{start_local}">
                    </div>
                    <div class="time-control">
                        <label for="endTime">End:</label>
                        <input type="datetime-local" id="endTime" value="{end_local}">
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
                function setQuickRange(days) {{
                    const end = new Date();
                    const start = new Date(end.getTime() - (days * 24 * 60 * 60 * 1000));

                    document.getElementById('endTime').value = formatDateTime(end);
                    document.getElementById('startTime').value = formatDateTime(start);
                }}

                function formatDateTime(date) {{
                    return date.toISOString().slice(0, 16);
                }}

                function updateMap() {{
                    const startTime = document.getElementById('startTime').value;
                    const endTime = document.getElementById('endTime').value;

                    if (!startTime || !endTime) {{
                        alert('Please select both start and end times');
                        return;
                    }}

                    const url = new URL(window.location.href);
                    url.searchParams.set('start', startTime + ':00Z');
                    url.searchParams.set('end', endTime + ':00Z');
                    window.location.href = url.toString();
                }}
                </script>
            </body>
            </html>
            """

        # Calculate center point
        center_lat = sum(loc["lat"] for loc in locations) / len(locations)
        center_lon = sum(loc["lon"] for loc in locations) / len(locations)

        # Generate markers JavaScript
        markers_js = []
        for i, loc in enumerate(locations):
            accuracy = loc.get("accuracy", 0)

            # Color based on accuracy
            if accuracy <= 5:
                color = "green"
            elif accuracy <= 20:
                color = "orange"
            else:
                color = "red"

            # Convert UTC timestamp to Home Assistant timezone
            timestamp_utc = datetime.fromisoformat(loc["timestamp"].replace("Z", "+00:00"))
            # Convert to Home Assistant's configured timezone
            timestamp_local = dt_util.as_local(timestamp_utc)

            popup_text = f"""
            <b>Location {i+1}</b><br>
            <b>Coordinates:</b> {loc['lat']:.6f}, {loc['lon']:.6f}<br>
            <b>GPS Accuracy:</b> {accuracy:.1f} meters<br>
            <b>Timestamp:</b> {timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z')}<br>
            <b>Entity ID:</b> {loc.get('entity_id', 'Unknown')}<br>
            <b>Entity State:</b> {loc.get('state', 'Unknown')}<br>
            """

            markers_js.append(f"""
                L.marker([{loc['lat']}, {loc['lon']}])
                    .addTo(map)
                    .bindPopup(`{popup_text}`)
                    .bindTooltip('Accuracy: {accuracy:.1f}m');

                L.circle([{loc['lat']}, {loc['lon']}], {{
                    radius: {accuracy},
                    color: '{color}',
                    fillColor: '{color}',
                    fillOpacity: 0.1
                }}).addTo(map);
            """)

        markers_code = "\n".join(markers_js)

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{device_name} - Location Map</title>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <style>
                body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
                #map {{ height: 100vh; width: 100%; }}
                .header {{ position: absolute; top: 10px; left: 10px; z-index: 1000;
                          background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.3);
                          max-width: 350px; }}
                .time-control {{ margin: 8px 0; }}
                .time-control label {{ display: inline-block; width: 60px; font-size: 12px; font-weight: bold; }}
                .time-control input {{ padding: 4px; border: 1px solid #ccc; border-radius: 3px; width: 140px; font-size: 11px; }}
                .header button {{ padding: 6px 12px; background: #007cba; color: white; border: none; border-radius: 4px; cursor: pointer; margin: 2px; font-size: 11px; }}
                .header button:hover {{ background: #005a8b; }}
                .quick-buttons {{ margin: 8px 0; }}
                .quick-buttons button {{ background: #6c757d; padding: 4px 8px; }}
                .quick-buttons button:hover {{ background: #5a6268; }}
                .toggle-controls {{ background: #28a745; margin-bottom: 10px; }}
                .controls-panel {{ display: none; }}
                .controls-panel.show {{ display: block; }}
                h2 {{ margin: 0 0 10px 0; font-size: 16px; }}
                p {{ margin: 5px 0; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h2>{device_name}</h2>
                <p>{len(locations)} locations</p>
                <button class="toggle-controls" onclick="toggleControls()">Time Filter</button>
                <div class="controls-panel" id="controlsPanel">
                    <div class="time-control">
                        <label for="startTime">Start:</label>
                        <input type="datetime-local" id="startTime" value="{start_local}">
                    </div>
                    <div class="time-control">
                        <label for="endTime">End:</label>
                        <input type="datetime-local" id="endTime" value="{end_local}">
                    </div>
                    <div class="quick-buttons">
                        <button onclick="setQuickRange(1)">1D</button>
                        <button onclick="setQuickRange(3)">3D</button>
                        <button onclick="setQuickRange(7)">7D</button>
                        <button onclick="setQuickRange(14)">14D</button>
                        <button onclick="setQuickRange(30)">30D</button>
                    </div>
                    <button onclick="updateMap()">Update Map</button>
                </div>
            </div>
            <div id="map"></div>

            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script>
                var map = L.map('map').setView([{center_lat}, {center_lon}], 13);

                L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                    attribution: '© OpenStreetMap contributors'
                }}).addTo(map);

                {markers_code}

                // Fit map to show all markers
                var group = new L.featureGroup();
                map.eachLayer(function(layer) {{
                    if (layer instanceof L.Marker) {{
                        group.addLayer(layer);
                    }}
                }});
                if (group.getLayers().length > 0) {{
                    map.fitBounds(group.getBounds().pad(0.1));
                }}

                // Time filter functions
                function toggleControls() {{
                    const panel = document.getElementById('controlsPanel');
                    panel.classList.toggle('show');
                }}

                function setQuickRange(days) {{
                    const end = new Date();
                    const start = new Date(end.getTime() - (days * 24 * 60 * 60 * 1000));

                    document.getElementById('endTime').value = formatDateTime(end);
                    document.getElementById('startTime').value = formatDateTime(start);
                }}

                function formatDateTime(date) {{
                    return date.toISOString().slice(0, 16);
                }}

                function updateMap() {{
                    const startTime = document.getElementById('startTime').value;
                    const endTime = document.getElementById('endTime').value;

                    if (!startTime || !endTime) {{
                        alert('Please select both start and end times');
                        return;
                    }}

                    const url = new URL(window.location.href);
                    url.searchParams.set('start', startTime + ':00Z');
                    url.searchParams.set('end', endTime + ':00Z');
                    window.location.href = url.toString();
                }}
            </script>
        </body>
        </html>
        """

    def _get_simple_token(self) -> str:
        """Generate a simple token for basic authentication."""
        import hashlib
        import time
        # Use HA's UUID and current day to create a simple token
        day = str(int(time.time() // 86400))  # Current day since epoch
        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))
        return hashlib.md5(f"{ha_uuid}:{day}".encode()).hexdigest()[:16]
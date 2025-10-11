# tests/test_diagnostics.py
"""Diagnostics tests for Google Find My Device.

Validates:
- No secrets/PII are exposed (tokens/emails/IDs redacted or omitted)
- Options snapshot contains only counts and safe booleans/ints
- Integration metadata present
- Registry counts are computed
- Coordinator snapshot shape is stable and safe
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from homeassistant.core import HomeAssistant

try:
    from tests.common import MockConfigEntry  # type: ignore
except Exception:
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # type: ignore

DOMAIN = "googlefindmy"
CONF_OAUTH_TOKEN = "oauth_token"
CONF_GOOGLE_EMAIL = "google_email"

# Import the function under test
from custom_components.googlefindmy.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_basic_shape_and_privacy(hass: HomeAssistant, device_registry, entity_registry) -> None:
    """Diagnostics should include safe metadata, config snapshot, registry + coordinator counters."""
    # Create a config entry with options set; data contains secrets (must not leak)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GOOGLE_EMAIL: "user@example.com", CONF_OAUTH_TOKEN: "S" * 64},
        options={
            "tracked_devices": ["dev1", "dev2"],
            "location_poll_interval": 120,
            "device_poll_delay": 3,
            "min_accuracy_threshold": 80,
            "movement_threshold": 25,
            "google_home_filter_enabled": True,
            "google_home_filter_keywords": "nest, mini , speaker",
            "enable_stats_entities": True,
            "map_view_token_expiration": True,
        },
        title="Google Find My Device",
        unique_id=f"{DOMAIN}:user@example.com",
    )
    entry.add_to_hass(hass)

    # Simulate a minimal coordinator object stored in hass.data
    class _Coordinator:
        _is_polling = True
        _last_poll_mono = 1.0  # some monotonic origin
        stats = {"polled": 5, "timeouts": 0}
        _device_names = {"dev1": "Phone", "dev2": "Watch"}
        _device_location_data = {"dev1": object(), "dev2": object(), "dev3": object()}

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _Coordinator()

    # Add devices/entities linked to this entry to test registry counts
    dev = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    entity_registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="sensor.dev1.last_seen",
        config_entry=entry,
        device_id=dev.id,
    )

    result: Dict[str, Any] = await async_get_config_entry_diagnostics(hass, entry)

    # Top-level structure
    assert "entry" in result and "config" in result and "integration" in result
    assert "registry" in result and "coordinator" in result

    # Entry section must not include unique_id/email/token
    assert result["entry"]["domain"] == DOMAIN
    assert "unique_id" not in result["entry"]
    assert CONF_GOOGLE_EMAIL not in str(result)
    assert CONF_OAUTH_TOKEN not in str(result)

    # Config snapshot: counts and booleans only, keywords as count
    cfg = result["config"]
    assert cfg["tracked_devices_count"] == 2
    assert cfg["google_home_filter_keywords_count"] == 3
    assert isinstance(cfg["location_poll_interval"], int)
    assert isinstance(cfg["enable_stats_entities"], bool)

    # Registry section contains counts
    reg = result["registry"]
    assert reg["device_count"] >= 1
    assert reg["entity_count"] >= 1

    # Coordinator section has safe numeric stats and flags
    coord = result["coordinator"]
    assert coord["is_polling"] is True
    assert "known_devices_count" in coord and "cache_items_count" in coord
    assert isinstance(coord.get("last_poll_wall_ts"), (int, float, type(None)))
    # stats are numeric-like or sanitized
    assert isinstance(coord["stats"].get("polled"), int)

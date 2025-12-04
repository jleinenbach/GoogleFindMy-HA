from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.googlefindmy.button import (
    GoogleFindMyLocateButton,
    GoogleFindMyPlaySoundButton,
)
from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_PLAY_SOUND,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)


@pytest.mark.asyncio
async def test_button_restores_last_pressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoring last state populates ``last_pressed`` when available."""

    hass = SimpleNamespace(data={DOMAIN: {}}, services=SimpleNamespace(), loop=None)
    coordinator = SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(entry_id="entry-id"),
        is_device_visible_in_subentry=lambda *_args: True,
        can_request_location=lambda _dev_id: True,
        async_request_refresh=AsyncMock(),
    )
    button = GoogleFindMyLocateButton(
        coordinator,
        {"id": "device-1", "name": "Tracker"},
        "Tracker",
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=f"{SERVICE_SUBENTRY_KEY}:tracker",
    )
    button._handle_coordinator_update = lambda: None
    restored_state = SimpleNamespace(state="2025-01-02T03:04:05+00:00")
    monkeypatch.setattr(
        button, "async_get_last_state", AsyncMock(return_value=restored_state)
    )

    await button.async_added_to_hass()

    assert button._attr_last_pressed == datetime(2025, 1, 2, 3, 4, 5, tzinfo=dt_util.UTC)


@pytest.mark.asyncio
async def test_button_records_last_pressed_on_press() -> None:
    """Button presses update ``last_pressed`` so availability recovers cleanly."""

    service_call = AsyncMock()
    hass = SimpleNamespace(
        services=SimpleNamespace(async_call=service_call),
        data={DOMAIN: {}},
        loop=None,
    )
    coordinator = SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(entry_id="entry-id"),
        is_device_visible_in_subentry=lambda *_args: True,
        can_play_sound=lambda _dev_id: True,
        async_request_refresh=AsyncMock(),
    )
    button = GoogleFindMyPlaySoundButton(
        coordinator,
        {"id": "device-1", "name": "Tracker"},
        "Tracker",
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=f"{SERVICE_SUBENTRY_KEY}:tracker",
    )

    await button.async_press()

    assert button._attr_last_pressed is not None
    service_call.assert_awaited_once_with(
        DOMAIN, SERVICE_PLAY_SOUND, {"device_id": "device-1"}, blocking=True
    )

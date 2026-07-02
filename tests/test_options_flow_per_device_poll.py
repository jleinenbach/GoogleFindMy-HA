# tests/test_options_flow_per_device_poll.py
"""Options-flow tests for the per-device poll interval feature (Direction A).

Cover the two-step management flow: pick a device (labelled by name), then set
or clear its interval. Mirrors the semantic-locations options-flow scaffold.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    DEVICE_POLL_INTERVAL_MAX_S,
    DEVICE_POLL_INTERVAL_MIN_S,
    OPT_DEVICE_POLL_INTERVAL,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import prepare_flow_hass_config_entries


class _RecordingConfigEntries:
    """Record options updates and reloads for verification."""

    def __init__(self, entry: SimpleNamespace) -> None:
        self._entry = entry
        self.updated_options: list[dict[str, Any]] = []
        self.reloaded: list[str] = []

    def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
        return self._entry if entry_id == self._entry.entry_id else None

    def async_update_entry(
        self, entry: SimpleNamespace, *, options: dict[str, Any] | None = None
    ) -> None:
        assert entry is self._entry
        if options is not None:
            self.updated_options.append(options)
            entry.options = options

    async def async_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


class _HassStub:
    """Minimal Home Assistant stub for per-device poll options flows."""

    def __init__(self, entry: SimpleNamespace) -> None:
        self.config_entries = _RecordingConfigEntries(entry)
        prepare_flow_hass_config_entries(
            self, lambda: self.config_entries, frame_module=frame
        )
        self.config = SimpleNamespace(latitude=12.5, longitude=34.5)
        self.data: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(
        self, coro: Awaitable[Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def drain_tasks(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)


def _entry_with_devices(
    devices: list[dict[str, str]], options: dict[str, Any] | None = None
) -> SimpleNamespace:
    """Build a config entry whose runtime coordinator exposes ``devices``."""
    coordinator = SimpleNamespace(data=devices)
    return make_config_entry(
        entry_id="entry-per-device",
        title="Per Device",
        options=options or {},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )


def _make_flow(entry: SimpleNamespace) -> tuple[Any, _HassStub]:
    hass = _HassStub(entry)
    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow, hass


@pytest.mark.asyncio
async def test_menu_lists_per_device_poll() -> None:
    """The options menu exposes the per-device poll entry."""
    entry = _entry_with_devices([{"id": "dev-a", "name": "Phone"}])
    flow, _hass = _make_flow(entry)

    result = await flow.async_step_init()

    assert result["type"] == "menu"
    assert "per_device_poll" in result["menu_options"]


@pytest.mark.asyncio
async def test_no_devices_aborts() -> None:
    """With no devices the flow aborts instead of showing an empty picker."""
    entry = _entry_with_devices([])
    flow, _hass = _make_flow(entry)

    result = await flow.async_step_per_device_poll(None)

    assert result["type"] == "abort"
    assert result["reason"] == "no_devices"


@pytest.mark.asyncio
async def test_set_interval_persists_and_reloads() -> None:
    """Selecting a device and submitting an interval stores and reloads it."""
    entry = _entry_with_devices([{"id": "dev-a", "name": "Phone"}])
    flow, hass = _make_flow(entry)

    picker = await flow.async_step_per_device_poll(None)
    assert picker["type"] == "form"
    assert picker["step_id"] == "per_device_poll"

    set_form = await flow.async_step_per_device_poll({"device": "dev-a"})
    assert set_form["type"] == "form"
    assert set_form["step_id"] == "per_device_poll_set"

    result = await flow.async_step_per_device_poll_set({"interval": 600})
    await hass.drain_tasks()

    assert result["type"] == "menu"
    assert hass.config_entries.updated_options[-1][OPT_DEVICE_POLL_INTERVAL] == {
        "dev-a": 600
    }
    assert hass.config_entries.reloaded == [entry.entry_id]


@pytest.mark.asyncio
async def test_empty_submission_clears_override() -> None:
    """Leaving the interval empty removes the device's override."""
    entry = _entry_with_devices(
        [{"id": "dev-a", "name": "Phone"}],
        options={OPT_DEVICE_POLL_INTERVAL: {"dev-a": 600}},
    )
    flow, hass = _make_flow(entry)

    await flow.async_step_per_device_poll({"device": "dev-a"})
    result = await flow.async_step_per_device_poll_set({})
    await hass.drain_tasks()

    assert result["type"] == "menu"
    assert hass.config_entries.updated_options[-1][OPT_DEVICE_POLL_INTERVAL] == {}


@pytest.mark.asyncio
async def test_invalid_device_shows_error() -> None:
    """An unknown device id is rejected on the picker step."""
    entry = _entry_with_devices([{"id": "dev-a", "name": "Phone"}])
    flow, _hass = _make_flow(entry)

    result = await flow.async_step_per_device_poll({"device": "ghost"})

    assert result["type"] == "form"
    assert result["errors"] == {"device": "invalid_device"}


@pytest.mark.asyncio
async def test_set_step_without_selection_returns_to_picker() -> None:
    """Entering the set step without a valid selection returns to the picker."""
    entry = _entry_with_devices([{"id": "dev-a", "name": "Phone"}])
    flow, _hass = _make_flow(entry)

    result = await flow.async_step_per_device_poll_set(None)

    assert result["type"] == "form"
    assert result["step_id"] == "per_device_poll"


@pytest.mark.asyncio
async def test_set_form_prefills_and_bounds() -> None:
    """The interval field is bounded and prefilled from the current override."""
    entry = _entry_with_devices(
        [{"id": "dev-a", "name": "Phone"}],
        options={OPT_DEVICE_POLL_INTERVAL: {"dev-a": 900}},
    )
    flow, _hass = _make_flow(entry)

    form = await flow.async_step_per_device_poll({"device": "dev-a"})

    # Suggested (prefilled) value reflects the stored override.
    markers = {marker.schema: marker for marker in form["data_schema"].schema}
    assert "interval" in markers
    suggested = markers["interval"].description
    assert isinstance(suggested, dict)
    assert suggested.get("suggested_value") == 900
    # Out-of-range submissions are rejected by the schema validator.
    with pytest.raises(Exception):
        form["data_schema"]({"interval": DEVICE_POLL_INTERVAL_MIN_S - 1})
    with pytest.raises(Exception):
        form["data_schema"]({"interval": DEVICE_POLL_INTERVAL_MAX_S + 1})

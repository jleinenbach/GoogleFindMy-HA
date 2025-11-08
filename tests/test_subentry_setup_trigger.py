# tests/test_subentry_setup_trigger.py
"""Tests for ensuring programmatically created subentries get set up."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import _async_ensure_subentries_are_setup
from custom_components.googlefindmy.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState

from tests.helpers.homeassistant import (
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeHass,
    runtime_data_with_subentries,
)


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_filters_states() -> None:
    """Only pending, enabled subentries should be set up."""

    parent_entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)
    pending_subentry = SimpleNamespace(
        entry_id="child-pending",
        subentry_id="child-pending",
        state=ConfigEntryState.NOT_LOADED,
        disabled_by=None,
    )
    active_subentry = SimpleNamespace(
        entry_id="child-active",
        subentry_id="child-active",
        state=ConfigEntryState.LOADED,
        disabled_by=None,
    )
    disabled_subentry = SimpleNamespace(
        entry_id="child-disabled",
        subentry_id="child-disabled",
        state=ConfigEntryState.NOT_LOADED,
        disabled_by="user",
    )

    parent_entry.runtime_data = runtime_data_with_subentries(
        {
            pending_subentry.entry_id: pending_subentry,
            active_subentry.entry_id: active_subentry,
            disabled_subentry.entry_id: disabled_subentry,
        }
    )

    manager = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [pending_subentry.subentry_id]

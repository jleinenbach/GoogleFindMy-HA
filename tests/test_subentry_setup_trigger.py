# tests/test_subentry_setup_trigger.py

"""Tests for ensuring programmatically created subentries get set up."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.googlefindmy import _async_ensure_subentries_are_setup
from custom_components.googlefindmy.const import DOMAIN

from tests.helpers.homeassistant import (
    FakeConfigEntriesManager,
    FakeHass,
    config_entry_with_runtime_managed_subentries,
)


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_schedules_all_children() -> None:
    """All discovered subentries should be scheduled for setup."""

    pending_subentry = SimpleNamespace(
        entry_id="child-pending",
        subentry_id="child-pending",
    )
    active_subentry = SimpleNamespace(
        entry_id="child-active",
        subentry_id="child-active",
    )
    disabled_subentry = SimpleNamespace(
        entry_id="child-disabled",
        subentry_id="child-disabled",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[pending_subentry, active_subentry, disabled_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [
        pending_subentry.entry_id,
        active_subentry.entry_id,
        disabled_subentry.entry_id,
    ]


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_warns_and_raises_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log a warning and raise ConfigEntryNotReady when subentry setup fails."""

    successful_subentry = SimpleNamespace(
        entry_id="child-success",
        subentry_id="child-success",
    )
    failing_subentry = SimpleNamespace(
        entry_id="child-failure",
        subentry_id="child-failure",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[successful_subentry, failing_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])

    async def failing_setup(entry_id: str) -> bool:
        manager.setup_calls.append(entry_id)
        return entry_id != failing_subentry.entry_id

    manager.async_setup = failing_setup  # type: ignore[assignment]
    hass = FakeHass(config_entries=manager)

    with caplog.at_level(logging.WARNING), pytest.raises(ConfigEntryNotReady):
        await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [
        successful_subentry.entry_id,
        failing_subentry.entry_id,
    ]
    assert any(
        "setup returned False" in record.getMessage()
        and failing_subentry.entry_id in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_id_value", [None, "", object()])
async def test_async_ensure_subentries_are_setup_falls_back_to_subentry_id(
    entry_id_value: object,
) -> None:
    """Fresh subentries resolve identifiers when entry_id is missing or invalid."""

    pending_subentry = SimpleNamespace(
        entry_id=entry_id_value,
        subentry_id="child-created",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries={pending_subentry.subentry_id: pending_subentry},
    )

    manager = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [pending_subentry.subentry_id]

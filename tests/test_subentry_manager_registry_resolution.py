# tests/test_subentry_manager_registry_resolution.py

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import (
    ConfigEntrySubEntryManager,
    ConfigEntrySubentryDefinition,
    _async_ensure_subentries_are_setup,
)
from custom_components.googlefindmy.const import DOMAIN
from tests.helpers.homeassistant import (
    DeferredRegistryConfigEntriesManager,
    FakeConfigEntry,
    FakeHass,
    deferred_subentry_entry_id_assignment,
)


async def _build_runtime_manager(
    *,
    hass: FakeHass,
    parent_entry: FakeConfigEntry,
    resolved_child: SimpleNamespace,
    unique_id: str,
) -> ConfigEntrySubEntryManager:
    """Create and synchronize a runtime subentry manager for tests."""

    manager = ConfigEntrySubEntryManager(hass, parent_entry)
    definition = ConfigEntrySubentryDefinition(
        key=resolved_child.data["group_key"],
        title="Child",
        data={},
        unique_id=unique_id,
    )
    await manager.async_sync([definition])
    return manager


@pytest.mark.asyncio
async def test_async_ensure_subentries_setup_handles_placeholder_objects() -> None:
    """Retry with the resolved ULID when runtime data still holds a provisional child."""

    child_entry_id = "child-resolved-ulid"
    parent_entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    resolved_child = SimpleNamespace(
        entry_id=child_entry_id,
        subentry_id="child-subentry-id",
        unique_id="unique-child",
        data={"group_key": "child-group"},
        subentry_type="tracker",
        state=None,
    )
    manager = DeferredRegistryConfigEntriesManager(parent_entry, resolved_child)
    hass = FakeHass(config_entries=manager)
    runtime_manager = await _build_runtime_manager(
        hass=hass,
        parent_entry=parent_entry,
        resolved_child=resolved_child,
        unique_id=resolved_child.unique_id,
    )

    provisional = manager.provisional_subentry
    assert provisional is not None, "Expected provisional subentry from async_add_subentry"
    provisional_id = f"provisional-{child_entry_id}"
    setattr(provisional, "entry_id", provisional_id)
    runtime_manager._managed[resolved_child.data["group_key"]] = provisional  # type: ignore[attr-defined]
    parent_entry.runtime_data = SimpleNamespace(subentry_manager=runtime_manager)

    child_entry = FakeConfigEntry(entry_id=child_entry_id, domain=DOMAIN)
    assign_task = asyncio.create_task(
        deferred_subentry_entry_id_assignment(
            provisional,
            entry_id=child_entry_id,
            manager=manager,
            delay=0.01,
            registered_entry=child_entry,
        )
    )

    try:
        await _async_ensure_subentries_are_setup(hass, parent_entry)
    finally:
        await assign_task

    assert manager.lookup_attempts[provisional_id] >= 1
    assert manager.setup_calls == [child_entry_id]


@pytest.mark.asyncio
async def test_async_sync_caches_resolved_registry_subentry() -> None:
    """Ensure async_sync stores the registry-backed child in managed state."""

    child_entry_id = "child-resolved-ulid"
    parent_entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    resolved_child = SimpleNamespace(
        entry_id=child_entry_id,
        subentry_id="child-subentry-id",
        unique_id="unique-child",
        data={"group_key": "child-group"},
        subentry_type="tracker",
        state=None,
    )
    manager = DeferredRegistryConfigEntriesManager(parent_entry, resolved_child)
    hass = FakeHass(config_entries=manager)

    runtime_manager = await _build_runtime_manager(
        hass=hass,
        parent_entry=parent_entry,
        resolved_child=resolved_child,
        unique_id=resolved_child.unique_id,
    )

    stored = runtime_manager.get("child-group")
    assert stored is not None
    assert stored.entry_id == child_entry_id
    assert stored is not manager.provisional_subentry


# tests/test_subentry_manager_registry_resolution.py

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

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
@pytest.mark.parametrize("shape", ["legacy", "dataclass"], ids=["legacy", "dataclass"])
async def test_async_sync_caches_resolved_registry_subentry(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Ensure async_sync stores the registry-backed child in managed state."""

    child_entry_id = "child-resolved-ulid"
    parent_entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)

    if shape == "legacy":
        resolved_child: Any = SimpleNamespace(
            entry_id=child_entry_id,
            subentry_id="child-subentry-id",
            unique_id="unique-child",
            data={"group_key": "child-group"},
            subentry_type="tracker",
            state=None,
        )
    else:

        @dataclass(frozen=True, kw_only=True)
        class _FrozenSubentry:
            data: Mapping[str, Any]
            subentry_type: str
            title: str
            unique_id: str | None
            subentry_id: str
            translation_key: str | None = None

            def __post_init__(self) -> None:
                object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

        monkeypatch.setattr(
            "custom_components.googlefindmy.ConfigSubentry",
            _FrozenSubentry,
        )
        resolved_child = _FrozenSubentry(
            data={"group_key": "child-group"},
            subentry_id="child-subentry-id",
            subentry_type="tracker",
            title="Child",
            unique_id="unique-child",
        )

    manager = DeferredRegistryConfigEntriesManager(parent_entry, resolved_child)
    hass = FakeHass(config_entries=manager)

    runtime_manager = await _build_runtime_manager(
        hass=hass,
        parent_entry=parent_entry,
        resolved_child=resolved_child,
        unique_id=getattr(resolved_child, "unique_id", None) or "unique-child",
    )

    stored = runtime_manager.get("child-group")
    assert stored is not None
    if shape == "legacy":
        assert getattr(stored, "entry_id", None) == child_entry_id
    else:
        assert getattr(stored, "entry_id", None) is None
    assert getattr(stored, "subentry_id", None) == resolved_child.subentry_id
    assert stored is resolved_child
    assert stored is not manager.provisional_subentry


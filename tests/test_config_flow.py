# tests/test_config_flow.py
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.config_flow import ConfigFlow
from custom_components.googlefindmy.const import DOMAIN


class _ManagerStub:
    """Record sync invocations triggered by the repair helper."""

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    async def async_sync(self, definitions: list[Any]) -> None:
        self.calls.append(list(definitions))


class _CoordinatorStub:
    """Capture coordinator interactions during core subentry repair."""

    def __init__(self) -> None:
        self.attached_managers: list[Any] = []
        self.refresh_invocations: list[bool] = []
        self.ensure_entries: list[Any] = []

    def attach_subentry_manager(self, manager: Any) -> None:
        self.attached_managers.append(manager)

    def _build_core_subentry_definitions(self) -> list[Any]:
        return [object()]

    def _refresh_subentry_index(self, *, skip_manager_update: bool = False) -> None:
        self.refresh_invocations.append(skip_manager_update)

    def _ensure_service_device_exists(self, entry: Any | None = None) -> None:
        self.ensure_entries.append(entry)


@pytest.mark.asyncio
async def test_core_subentry_repair_passes_entry_to_service_device_ensure() -> None:
    """The repair helper should pass the active entry to the service-device ensure."""

    coordinator = _CoordinatorStub()
    manager = _ManagerStub()
    runtime_data = SimpleNamespace(coordinator=coordinator, subentry_manager=manager)

    entry = SimpleNamespace(
        entry_id="repair-entry",
        data={},
        options={},
        subentries={},
        runtime_data=runtime_data,
    )

    hass = SimpleNamespace(data={DOMAIN: {"entries": {entry.entry_id: runtime_data}}})

    await ConfigFlow._async_trigger_core_subentry_repair(hass, entry)

    assert manager.calls, "core subentry definitions should be synced"
    assert coordinator.ensure_entries == [entry]


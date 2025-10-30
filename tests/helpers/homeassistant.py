# tests/helpers/homeassistant.py
"""Reusable Home Assistant-style stubs for integration tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable, Iterable
from typing import Any

from custom_components.googlefindmy.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall

__all__ = [
    "FakeConfigEntry",
    "FakeConfigEntriesManager",
    "FakeServiceRegistry",
    "FakeDeviceRegistry",
    "FakeEntityRegistry",
    "FakeHass",
]


@dataclass(slots=True)
class FakeConfigEntry:
    """Minimal config entry representation used across service tests."""

    entry_id: str
    domain: str = DOMAIN
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED
    title: str | None = None


class FakeConfigEntriesManager:
    """Provide config entry access and capture reload/update attempts."""

    def __init__(
        self,
        entries: Iterable[FakeConfigEntry] | None = None,
        *,
        migration_success: bool = True,
        supports_migrate: bool = True,
    ) -> None:
        self._entries: list[FakeConfigEntry] = list(entries or [])
        self.reload_calls: list[str] = []
        self.update_calls: list[tuple[FakeConfigEntry, dict[str, Any]]] = []
        self.migrate_calls: list[str] = []
        self.migration_success = migration_success
        if not supports_migrate:
            # Mirror Home Assistant instances that omit async_migrate helpers.
            self.async_migrate_entry = None  # type: ignore[assignment]
            self.async_migrate = None  # type: ignore[assignment]

    def add_entry(self, entry: FakeConfigEntry) -> None:
        """Register another entry for subsequent lookups."""

        self._entries.append(entry)

    def async_entries(self, domain: str | None = None) -> list[FakeConfigEntry]:
        """Return entries optionally filtered by domain."""

        if domain is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.domain == domain]

    def async_get_entry(self, entry_id: str) -> FakeConfigEntry | None:
        """Return the entry matching ``entry_id`` if available."""

        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def async_update_entry(self, entry: FakeConfigEntry, **kwargs: Any) -> None:
        """Capture entry updates in ``update_calls`` for assertions."""

        self.update_calls.append((entry, dict(kwargs)))

    async def async_reload(self, entry_id: str) -> None:
        """Record reload attempts made by the integration."""

        self.reload_calls.append(entry_id)

    async def async_migrate_entry(self, entry: FakeConfigEntry) -> bool:
        """Record migration attempts and optionally mark the entry as reloadable."""

        self.migrate_calls.append(entry.entry_id)
        if self.migration_success:
            entry.state = ConfigEntryState.NOT_LOADED
        return self.migration_success

    async def async_migrate(self, entry_id: str) -> bool:
        """Backwards-compatible alias that delegates to ``async_migrate_entry``."""

        entry = self.async_get_entry(entry_id)
        if entry is None:
            return False
        return await self.async_migrate_entry(entry)


class FakeServiceRegistry:
    """Store registered service handlers for direct invocation in tests."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Callable[[ServiceCall], Any]] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Callable[[ServiceCall], Any],
    ) -> None:
        """Register a handler keyed by ``(domain, service)``."""

        self.handlers[(domain, service)] = handler


class FakeDeviceRegistry:
    """Expose the subset of device registry behaviour required by tests."""

    def __init__(self) -> None:
        self.devices: dict[str, Any] = {}

    def async_get(self, device_id: str) -> Any | None:
        return self.devices.get(device_id)

    def async_remove_device(self, device_id: str) -> None:  # pragma: no cover - defensive
        raise AssertionError(f"Unexpected device removal for {device_id}")


class FakeEntityRegistry:
    """Expose the subset of entity registry behaviour required by tests."""

    def __init__(self) -> None:
        self.entities: dict[str, Any] = {}

    def async_remove(self, entity_id: str) -> None:  # pragma: no cover - defensive
        raise AssertionError(f"Unexpected entity removal for {entity_id}")


@dataclass(slots=True)
class FakeHass:
    """Home Assistant stub exposing the services and config entry manager."""

    config_entries: FakeConfigEntriesManager
    services: FakeServiceRegistry = field(default_factory=FakeServiceRegistry)
    data: dict[str, Any] = field(default_factory=dict)


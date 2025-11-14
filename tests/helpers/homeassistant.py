# tests/helpers/homeassistant.py
"""Reusable Home Assistant-style stubs for integration tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import FrozenInstanceError, dataclass, field
from types import SimpleNamespace
from typing import Any

from custom_components.googlefindmy import UnknownEntry
from custom_components.googlefindmy.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall

from .config_flow import attach_config_entries_flow_manager

__all__ = [
    "FakeConfigEntry",
    "FakeConfigEntriesManager",
    "DeferredRegistryConfigEntriesManager",
    "FakeServiceRegistry",
    "FakeDeviceEntry",
    "FakeDeviceRegistry",
    "device_registry_async_entries_for_config_entry",
    "FakeEntityRegistry",
    "FakeHass",
    "runtime_subentry_manager",
    "runtime_data_with_subentries",
    "config_entry_with_subentries",
    "config_entry_with_runtime_managed_subentries",
    "resolve_config_entry_lookup",
    "deferred_subentry_entry_id_assignment",
]


def _assign_if_present(target: Any, attribute: str, value: Any) -> None:
    """Assign ``value`` to ``attribute`` when the target exposes the field."""

    if not hasattr(target, attribute):
        return
    try:
        setattr(target, attribute, value)
    except (AttributeError, TypeError, FrozenInstanceError):
        object.__setattr__(target, attribute, value)


@dataclass(slots=True)
class FakeConfigEntry:
    """Minimal config entry representation used across service tests."""

    entry_id: str
    domain: str = DOMAIN
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED
    title: str | None = None
    subentries: dict[str, Any] = field(default_factory=dict)
    runtime_data: Any | None = None


@dataclass(slots=True)
class _TransientUnknownEntryConfig:
    """Configuration describing transient UnknownEntry behavior."""

    lookup_misses: int = 0
    setup_failures: int = 0


TransientUnknownConfigInput = (
    _TransientUnknownEntryConfig | Mapping[str, int] | int
)


def resolve_config_entry_lookup(
    entries: Iterable[Any], entry_id: str
) -> Any | None:
    """Return an entry or subentry matching ``entry_id``.

    This mirrors the lookup contract exercised by
    ``FakeConfigEntriesManager`` so purpose-built test stubs can reuse the
    logic without duplicating it.
    """

    for entry in entries:
        if getattr(entry, "entry_id", None) == entry_id:
            return entry

    for entry in entries:
        runtime_data = getattr(entry, "runtime_data", None)
        manager = getattr(runtime_data, "subentry_manager", None)
        managed = getattr(manager, "managed_subentries", None)
        if isinstance(managed, dict):
            candidate = managed.get(entry_id)
            if candidate is not None:
                return candidate
            for subentry in managed.values():
                candidate_entry_id = getattr(subentry, "entry_id", None)
                if isinstance(candidate_entry_id, str) and candidate_entry_id == entry_id:
                    return subentry
                candidate_subentry_id = getattr(subentry, "subentry_id", None)
                if isinstance(candidate_subentry_id, str) and candidate_subentry_id == entry_id:
                    return subentry

    for entry in entries:
        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            for subentry in subentries.values():
                candidate_entry_id = getattr(subentry, "entry_id", None)
                if isinstance(candidate_entry_id, str) and candidate_entry_id == entry_id:
                    return subentry
                candidate_subentry_id = getattr(subentry, "subentry_id", None)
                if isinstance(candidate_subentry_id, str) and candidate_subentry_id == entry_id:
                    return subentry

    return None


class FakeConfigEntriesManager:
    """Provide config entry access and capture reload/update attempts."""

    # Keep this manager aligned with `_StubConfigEntries` in
    # `tests/test_hass_data_layout.py`; see `tests/AGENTS.md` for the shared
    # synchronization guidance.

    def __init__(
        self,
        entries: Iterable[FakeConfigEntry] | None = None,
        *,
        migration_success: bool = True,
        supports_migrate: bool = True,
        transient_unknown_entries: Mapping[str, TransientUnknownConfigInput] | None = None,
    ) -> None:
        self._entries: list[FakeConfigEntry] = list(entries or [])
        self.reload_calls: list[str] = []
        self.update_calls: list[tuple[FakeConfigEntry, dict[str, Any]]] = []
        self.migrate_calls: list[str] = []
        self.migration_success = migration_success
        attach_config_entries_flow_manager(self)
        if not supports_migrate:
            # Mirror Home Assistant instances that omit async_migrate helpers.
            self.async_migrate_entry = None  # type: ignore[assignment]
            self.async_migrate = None  # type: ignore[assignment]
        self.setup_calls: list[str] = []
        self.lookup_attempts: dict[str, int] = defaultdict(int)
        self._transient_unknown: dict[str, _TransientUnknownEntryConfig] = {}
        if transient_unknown_entries:
            for entry_id, config in transient_unknown_entries.items():
                self.set_transient_unknown_entry(entry_id, config=config)

    def add_entry(self, entry: FakeConfigEntry) -> None:
        """Register another entry for subsequent lookups."""

        self._entries.append(entry)

    def set_transient_unknown_entry(
        self,
        entry_id: str,
        *,
        lookup_misses: int | None = None,
        setup_failures: int | None = None,
        config: TransientUnknownConfigInput | None = None,
    ) -> None:
        """Configure transient UnknownEntry behavior for a child entry.

        Parameters
        ----------
        entry_id:
            Identifier of the entry whose lookups or setup should simulate
            transient UnknownEntry races.
        lookup_misses:
            Number of initial ``async_get_entry`` calls that should return
            ``None`` for ``entry_id``.
        setup_failures:
            Number of initial ``async_setup`` calls that should raise
            :class:`UnknownEntry` for ``entry_id``.
        config:
            Optional aggregate configuration. When provided, ``lookup_misses``
            and ``setup_failures`` overrides still take precedence.
        """

        resolved = self._coerce_transient_unknown_config(config)
        if lookup_misses is not None:
            resolved.lookup_misses = lookup_misses
        if setup_failures is not None:
            resolved.setup_failures = setup_failures
        self._transient_unknown[entry_id] = resolved


    @staticmethod
    def _coerce_transient_unknown_config(
        config: TransientUnknownConfigInput | None,
    ) -> _TransientUnknownEntryConfig:
        if isinstance(config, _TransientUnknownEntryConfig):
            return _TransientUnknownEntryConfig(
                lookup_misses=max(0, config.lookup_misses),
                setup_failures=max(0, config.setup_failures),
            )
        if isinstance(config, Mapping):
            return _TransientUnknownEntryConfig(
                lookup_misses=max(0, int(config.get("lookup_misses", 0))),
                setup_failures=max(0, int(config.get("setup_failures", 0))),
            )
        if isinstance(config, int):
            return _TransientUnknownEntryConfig(lookup_misses=max(0, config))
        return _TransientUnknownEntryConfig()

    def async_entries(self, domain: str | None = None) -> list[FakeConfigEntry]:
        """Return entries optionally filtered by domain."""

        if domain is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.domain == domain]

    def async_get_entry(self, entry_id: str) -> Any | None:
        """Return the entry or subentry matching ``entry_id`` if available."""

        self.lookup_attempts[entry_id] += 1
        config = self._transient_unknown.get(entry_id)
        if config is not None and config.lookup_misses > 0:
            config.lookup_misses -= 1
            return None
        return resolve_config_entry_lookup(self._entries, entry_id)

    def async_get_subentries(self, entry_id: str) -> list[Any]:
        """Return child subentries registered on the provided entry."""

        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            return list(subentries.values())
        if isinstance(subentries, (list, tuple)):
            return list(subentries)
        return []

    async def async_setup(self, entry_id: str) -> bool:
        """Record setup attempts for config subentries."""

        self.setup_calls.append(entry_id)
        config = self._transient_unknown.get(entry_id)
        if config is not None and config.setup_failures > 0:
            config.setup_failures -= 1
            raise UnknownEntry(entry_id)
        return True

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


class DeferredRegistryConfigEntriesManager(FakeConfigEntriesManager):
    """Simulate delayed registry publication when ``async_create_subentry`` is absent."""

    def __init__(
        self, parent_entry: FakeConfigEntry, resolved_child: Any
    ) -> None:
        super().__init__([parent_entry])
        self._resolved_child = resolved_child
        self.provisional_subentry: Any | None = None
        self._defer_publication = False
        # Mirror Home Assistant cores that do not expose async_create_subentry.
        self.async_create_subentry = None  # type: ignore[assignment]

    def async_add_subentry(
        self, entry: FakeConfigEntry, subentry: Any
    ) -> None:
        """Stage a provisional subentry and defer registry visibility."""

        self.provisional_subentry = subentry
        _assign_if_present(subentry, "entry_id", None)
        _assign_if_present(
            subentry,
            "subentry_id",
            getattr(self._resolved_child, "subentry_id", None),
        )
        entry.subentries[self._resolved_child.subentry_id] = self._resolved_child
        self._defer_publication = True
        return None

    def async_get_entry(self, entry_id: str) -> Any | None:
        """Delay lookups until the resolved child becomes visible."""

        provisional = self.provisional_subentry
        provisional_id = (
            getattr(provisional, "entry_id", None)
            if provisional is not None
            else None
        )
        resolved_id = getattr(self._resolved_child, "entry_id", None)
        if (
            self._defer_publication
            and isinstance(provisional_id, str)
            and provisional_id
            and provisional_id != resolved_id
            and entry_id == provisional_id
        ):
            self.lookup_attempts[entry_id] += 1
            return None

        if self._defer_publication and entry_id == resolved_id:
            self.lookup_attempts[entry_id] += 1
            self._defer_publication = False
            return None

        return super().async_get_entry(entry_id)


async def deferred_subentry_entry_id_assignment(
    subentry: Any,
    *,
    entry_id: str,
    manager: FakeConfigEntriesManager,
    delay: float = 0.0,
    registered_entry: FakeConfigEntry | None = None,
) -> None:
    """Assign ``entry_id`` after ``delay`` seconds and register the child entry."""

    await asyncio.sleep(delay)
    _assign_if_present(subentry, "entry_id", entry_id)
    if registered_entry is not None:
        manager.add_entry(registered_entry)


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


def runtime_subentry_manager(
    subentries: Mapping[str, Any] | Iterable[Any],
) -> SimpleNamespace:
    """Return a runtime-style manager exposing ``managed_subentries``.

    Parameters
    ----------
    subentries:
        Either a mapping of identifiers to subentry objects or an iterable of
        objects that define an ``entry_id`` attribute.
    """

    if isinstance(subentries, Mapping):
        managed = dict(subentries)
    else:
        managed = {
            subentry.entry_id: subentry
            for subentry in subentries
            if getattr(subentry, "entry_id", None) is not None
        }

    class _RuntimeSubentryManager:
        """Expose a mutable mapping mirroring Home Assistant's manager."""

        def __init__(self, mapping: dict[str, Any]) -> None:
            self._managed = dict(mapping)

        @property
        def managed_subentries(self) -> dict[str, Any]:
            """Return a shallow copy of the managed subentry mapping."""

            return dict(self._managed)

    return _RuntimeSubentryManager(managed)


def runtime_data_with_subentries(
    subentries: Mapping[str, Any] | Iterable[Any],
) -> SimpleNamespace:
    """Create a runtime data namespace exposing the provided subentries."""

    return SimpleNamespace(subentry_manager=runtime_subentry_manager(subentries))


def config_entry_with_subentries(
    *,
    entry_id: str,
    domain: str = DOMAIN,
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED,
    title: str | None = None,
    subentries: Mapping[str, Any] | Iterable[Any] | None = None,
    runtime_data: Any | None = None,
) -> FakeConfigEntry:
    """Create a config entry that exposes normalized ``subentries``."""

    entry = FakeConfigEntry(
        entry_id=entry_id,
        domain=domain,
        state=state,
        title=title,
        runtime_data=runtime_data,
    )
    if subentries is None:
        return entry

    if isinstance(subentries, Mapping):
        entry.subentries = dict(subentries)
        return entry

    normalized: dict[str, Any] = {}
    for subentry in subentries:
        identifier = getattr(subentry, "subentry_id", None) or getattr(
            subentry, "entry_id", None
        )
        if identifier is None:
            raise ValueError("Subentries must define 'subentry_id' or 'entry_id'")
        normalized[str(identifier)] = subentry

    entry.subentries = normalized
    return entry


def config_entry_with_runtime_managed_subentries(
    *,
    entry_id: str,
    domain: str = DOMAIN,
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED,
    title: str | None = None,
    subentries: Mapping[str, Any] | Iterable[Any] | None = None,
) -> FakeConfigEntry:
    """Create a config entry with a runtime subentry manager attached."""

    entry = config_entry_with_subentries(
        entry_id=entry_id,
        domain=domain,
        state=state,
        title=title,
        subentries=subentries,
    )
    existing = getattr(entry, "subentries", None)
    if isinstance(existing, Mapping):
        mapping = dict(existing)
    elif existing is None:
        mapping = {}
    else:
        mapping = {}
        for subentry in existing:
            identifier = getattr(subentry, "subentry_id", None)
            if not identifier:
                identifier = getattr(subentry, "entry_id", None)
            if not identifier:
                continue
            mapping[str(identifier)] = subentry
    entry.runtime_data = runtime_data_with_subentries(mapping)
    return entry


@dataclass(slots=True)
class FakeDeviceEntry:
    """Lightweight device entry model mirroring Home Assistant's registry."""

    id: str
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    config_entries: set[str] = field(default_factory=set)
    config_subentry_id: str | None = None
    via_device_id: str | None = None
    name: str | None = None


class FakeDeviceRegistry:
    """Expose the subset of device registry behaviour required by tests."""

    def __init__(self, devices: Iterable[FakeDeviceEntry] | None = None) -> None:
        self.devices: dict[str, FakeDeviceEntry] = {
            device.id: device for device in devices or ()
        }
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def add_device(self, device: FakeDeviceEntry) -> None:
        """Register another device entry for subsequent lookups."""

        self.devices[device.id] = device

    def async_get(self, device_id: str) -> FakeDeviceEntry | None:
        return self.devices.get(device_id)

    def async_update_device(self, device_id: str, **changes: Any) -> None:
        """Record updates to a device entry and apply them immediately."""

        entry = self.devices[device_id]
        for attribute, value in changes.items():
            setattr(entry, attribute, value)
        self.updated.append((device_id, dict(changes)))

    def async_entries_for_config_entry(
        self, entry_id: str
    ) -> tuple[FakeDeviceEntry, ...]:
        """Return all device entries associated with ``entry_id``."""

        return tuple(
            device
            for device in self.devices.values()
            if entry_id in device.config_entries
        )

    def async_remove_device(self, device_id: str) -> None:  # pragma: no cover - defensive
        raise AssertionError(f"Unexpected device removal for {device_id}")


def device_registry_async_entries_for_config_entry(
    registry: FakeDeviceRegistry, entry_id: str
) -> tuple[FakeDeviceEntry, ...]:
    """Return devices for ``entry_id`` mirroring Home Assistant's helper."""

    return registry.async_entries_for_config_entry(entry_id)


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


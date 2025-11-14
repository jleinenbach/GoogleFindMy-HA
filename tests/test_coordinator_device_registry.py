# tests/test_coordinator_device_registry.py
"""Regression tests for coordinator device registry linkage."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping, cast
from collections.abc import Iterable

import pytest

from custom_components.googlefindmy import _async_relink_subentry_entities
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import (
    DOMAIN,
    INTEGRATION_VERSION,
    SERVICE_DEVICE_MANUFACTURER,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    SUBENTRY_TYPE_HUB,
    service_device_identifier,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr, entity_registry as er


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for tests."""

    return f"{entry_id}-{key}-subentry"


def _service_subentry_identifier(entry: SimpleNamespace) -> tuple[str, str]:
    """Build the service-device config-subentry identifier tuple for assertions."""

    return (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service")


class _FakeDeviceEntry:
    """Minimal stand-in for Home Assistant's DeviceEntry."""

    _counter = 0

    def __init__(
        self,
        *,
        identifiers: Iterable[tuple[str, str]],
        config_entry_id: str,
        name: str | None,
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        sw_version: str | None = None,
        entry_type: Any | None = None,
        configuration_url: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        config_subentry_id: str | None = None,
    ) -> None:
        self.identifiers: set[tuple[str, str]] = set(identifiers)
        self.config_entries = {config_entry_id}
        type(self)._counter += 1
        self.id = f"device-{config_entry_id}-{type(self)._counter}"
        self.name = name
        self.name_by_user = None
        self.disabled_by = None
        self.via_device_id = via_device_id
        self.via_device = via_device
        self.manufacturer = manufacturer
        self.model = model
        self.sw_version = sw_version
        self.entry_type = entry_type
        self.configuration_url = configuration_url
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders
        subentry_set: set[str | None]
        if config_subentry_id is None:
            subentry_set = {None}
        else:
            subentry_set = {config_subentry_id}
        self.config_entries_subentries: dict[str, set[str | None]] = {
            config_entry_id: set(subentry_set)
        }
        # Backwards-compat attribute used by legacy assertions; keep in sync with
        # the single-subentry view when applicable.
        self.config_subentry_id = config_subentry_id

    def _sync_config_subentry_id(self, entry_id: str) -> None:
        """Keep the legacy config_subentry_id attribute aligned with mappings."""

        subentries = self.config_entries_subentries.get(entry_id)
        if not subentries:
            self.config_subentry_id = None
            return
        # Prefer deterministic ordering for stable expectations.
        non_null = [candidate for candidate in subentries if candidate is not None]
        if len(subentries) == 1 and not non_null:
            self.config_subentry_id = None
        elif len(non_null) == 1:
            self.config_subentry_id = non_null[0]
        else:
            # When multiple tracker subentries are attached, surface None to make
            # assertions explicitly inspect the mapping instead of the shortcut.
            self.config_subentry_id = None


_UNSET_DEVICE = object()


class _FakeDeviceRegistry:
    """Fake device registry capturing `async_get_or_create` calls."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.devices: list[_FakeDeviceEntry] = []

    def async_get(self, device_id: str) -> _FakeDeviceEntry | None:
        for device in self.devices:
            if device.id == device_id:
                return device
        return None

    def async_get_device(
        self, *, identifiers: set[tuple[str, str]]
    ) -> _FakeDeviceEntry | None:
        for device in self.devices:
            if identifiers & device.identifiers:
                return device
        return None

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        manufacturer: str,
        model: str,
        name: str | None = None,
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> _FakeDeviceEntry:
        existing = self.async_get_device(identifiers=identifiers)
        if existing is not None:
            mapping = {
                entry_id: set(subentries)
                for entry_id, subentries in existing.config_entries_subentries.items()
            }
            config_entries = set(existing.config_entries)
            config_entries.add(config_entry_id)
            subentries = mapping.setdefault(config_entry_id, set())
            provided_subentry = kwargs.get("config_subentry_id")
            if provided_subentry is not None:
                subentries.add(provided_subentry)
            elif not subentries:
                subentries.add(None)
            new_entry = replace(
                existing,
                name=name if name is not None else existing.name,
                manufacturer=manufacturer,
                model=model,
                sw_version=kwargs.get("sw_version"),
                entry_type=kwargs.get("entry_type"),
                configuration_url=kwargs.get("configuration_url"),
                translation_key=kwargs.get("translation_key"),
                translation_placeholders=cast(
                    Mapping[str, str] | None,
                    kwargs.get("translation_placeholders"),
                ),
                config_entries=frozenset(config_entries),
                config_entries_subentries=self._normalize_subentries(mapping),
                config_subentry_id=self._canonical_config_subentry(mapping),
            )
            self._store(new_entry)
            return new_entry

        entry = _FakeDeviceEntry(
            identifiers=identifiers,
            config_entry_id=config_entry_id,
            name=name,
            via_device_id=via_device_id,
            via_device=via_device,
            manufacturer=manufacturer,
            model=model,
            sw_version=kwargs.get("sw_version"),
            entry_type=kwargs.get("entry_type"),
            configuration_url=kwargs.get("configuration_url"),
            translation_key=kwargs.get("translation_key"),
            translation_placeholders=kwargs.get("translation_placeholders"),
            config_subentry_id=kwargs.get("config_subentry_id"),
        )
        self.devices.append(entry)
        self.created.append(
            {
                "config_entry_id": config_entry_id,
                "identifiers": identifiers,
                "manufacturer": manufacturer,
                "model": model,
                "name": name,
                "via_device_id": via_device_id,
                "via_device": via_device,
                "sw_version": kwargs.get("sw_version"),
                "entry_type": kwargs.get("entry_type"),
                "configuration_url": kwargs.get("configuration_url"),
                "translation_key": kwargs.get("translation_key"),
                "translation_placeholders": kwargs.get("translation_placeholders"),
                "config_subentry_id": kwargs.get("config_subentry_id"),
            }
        )
        return entry

    def async_update_device(
        self,
        *,
        device_id: str,
        new_identifiers: Iterable[tuple[str, str]] | None = None,
        via_device_id: Any = _UNSET_DEVICE,
        name: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        add_config_entry_id: str | None = None,
        add_config_subentry_id: str | None = None,
        remove_config_entry_id: str | None = None,
        remove_config_subentry_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        for device in self.devices:
            if device.id == device_id:
                if new_identifiers is not None:
                    device.identifiers = set(new_identifiers)
                if via_device_id is not _UNSET_DEVICE:
                    device.via_device_id = cast(str | None, via_device_id)
                if name is not None:
                    device.name = name
                if translation_key is not None:
                    device.translation_key = translation_key
                if translation_placeholders is not None:
                    device.translation_placeholders = translation_placeholders
                if add_config_entry_id:
                    device.config_entries.add(add_config_entry_id)
                    subentries = device.config_entries_subentries.setdefault(
                        add_config_entry_id, set()
                    )
                    if add_config_subentry_id is None:
                        subentries.add(None)
                    else:
                        subentries.add(add_config_subentry_id)
                    device._sync_config_subentry_id(add_config_entry_id)
                elif add_config_subentry_id is not None:
                    # Tests cover the "missing add_config_entry_id" regression by
                    # recording the raw payload. Mimic Home Assistant's error by
                    # raising so assertions fail loudly instead of silently mutating
                    # the stub state.
                    raise AssertionError(
                        "add_config_subentry_id provided without add_config_entry_id"
                    )
                if remove_config_entry_id:
                    subentries = device.config_entries_subentries.get(
                        remove_config_entry_id
                    )
                    if remove_config_subentry_id is None:
                        if subentries is not None:
                            subentries.discard(None)
                            if not subentries:
                                device.config_entries_subentries.pop(
                                    remove_config_entry_id, None
                                )
                                device.config_entries.discard(remove_config_entry_id)
                    elif subentries is not None:
                        subentries.discard(remove_config_subentry_id)
                        if not subentries:
                            device.config_entries_subentries.pop(
                                remove_config_entry_id, None
                            )
                            device.config_entries.discard(remove_config_entry_id)
                    device._sync_config_subentry_id(remove_config_entry_id)
                if "manufacturer" in kwargs:
                    device.manufacturer = kwargs["manufacturer"]
                if "model" in kwargs:
                    device.model = kwargs["model"]
                if "sw_version" in kwargs:
                    device.sw_version = kwargs["sw_version"]
                if "entry_type" in kwargs:
                    device.entry_type = kwargs["entry_type"]
                if "configuration_url" in kwargs:
                    device.configuration_url = kwargs["configuration_url"]
                self.updated.append(
                    {
                        "device_id": device_id,
                        "new_identifiers": None
                        if new_identifiers is None
                        else set(new_identifiers),
                        "via_device_id": None
                        if via_device_id is _UNSET_DEVICE
                        else cast(str | None, via_device_id),
                        "name": name,
                        "translation_key": translation_key,
                        "translation_placeholders": translation_placeholders,
                        "config_entry_id": add_config_entry_id,
                        "config_subentry_id": add_config_subentry_id,
                        "add_config_entry_id": add_config_entry_id,
                        "add_config_subentry_id": add_config_subentry_id,
                        "remove_config_entry_id": remove_config_entry_id,
                        "remove_config_subentry_id": remove_config_subentry_id,
                        "manufacturer": kwargs.get("manufacturer"),
                        "model": kwargs.get("model"),
                        "sw_version": kwargs.get("sw_version"),
                        "entry_type": kwargs.get("entry_type"),
                        "configuration_url": kwargs.get("configuration_url"),
                    }
                )
                return
        raise AssertionError(f"Unknown device_id {device_id}")


_UNSET = object()


@dataclass(frozen=True)
class _FrozenDeviceEntry:
    """Frozen stand-in mirroring Home Assistant's DeviceEntry dataclass."""

    id: str
    identifiers: frozenset[tuple[str, str]]
    config_entries: frozenset[str]
    name: str | None = None
    name_by_user: str | None = None
    disabled_by: Any | None = None
    via_device_id: str | None = None
    via_device: tuple[str, str] | None = None
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None
    entry_type: Any | None = None
    configuration_url: str | None = None
    translation_key: str | None = None
    translation_placeholders: Mapping[str, str] | None = None
    config_subentry_id: str | None = None
    config_entries_subentries: Mapping[str, frozenset[str | None]] = MappingProxyType({})


class _FrozenDeviceRegistry:
    """Device registry stub that returns frozen entries and clones on update."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.devices: list[_FrozenDeviceEntry] = []
        self._devices: dict[str, _FrozenDeviceEntry] = {}
        self._counter = 0

    def _next_id(self, entry_id: str) -> str:
        self._counter += 1
        return f"frozen-{entry_id}-{self._counter}"

    def _store(self, entry: _FrozenDeviceEntry) -> None:
        self._devices[entry.id] = entry
        for idx, existing in enumerate(self.devices):
            if existing.id == entry.id:
                self.devices[idx] = entry
                break
        else:
            self.devices.append(entry)

    @staticmethod
    def _normalize_subentries(
        mapping: Mapping[str, set[str | None]]
    ) -> Mapping[str, frozenset[str | None]]:
        normalized: dict[str, frozenset[str | None]] = {}
        for entry_id, items in mapping.items():
            if not items:
                continue
            subset = {item for item in items if item is not None}
            if None in items:
                subset.add(None)
            normalized[entry_id] = frozenset(subset)
        return MappingProxyType(normalized)

    @staticmethod
    def _canonical_config_subentry(
        mapping: Mapping[str, set[str | None]]
    ) -> str | None:
        non_null = [
            candidate
            for subentries in mapping.values()
            for candidate in subentries
            if candidate is not None
        ]
        if len(non_null) == 1:
            return non_null[0]
        return None

    def async_get(self, device_id: str) -> _FrozenDeviceEntry | None:
        return self._devices.get(device_id)

    def async_get_device(
        self, *, identifiers: set[tuple[str, str]]
    ) -> _FrozenDeviceEntry | None:
        for entry in self._devices.values():
            if identifiers & set(entry.identifiers):
                return entry
        return None

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        manufacturer: str,
        model: str,
        name: str | None = None,
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> _FrozenDeviceEntry:
        existing = self.async_get_device(identifiers=identifiers)
        if existing is not None:
            return existing

        device_id = self._next_id(config_entry_id)
        initial_subentries: dict[str, set[str | None]] = {}
        provided_subentry = kwargs.get("config_subentry_id")
        initial_subentries[config_entry_id] = (
            {provided_subentry} if provided_subentry is not None else {None}
        )
        entry = _FrozenDeviceEntry(
            id=device_id,
            identifiers=frozenset(identifiers),
            config_entries=frozenset({config_entry_id}),
            name=name,
            via_device_id=via_device_id,
            via_device=via_device,
            manufacturer=manufacturer,
            model=model,
            sw_version=kwargs.get("sw_version"),
            entry_type=kwargs.get("entry_type"),
            configuration_url=kwargs.get("configuration_url"),
            translation_key=kwargs.get("translation_key"),
            translation_placeholders=cast(
                Mapping[str, str] | None, kwargs.get("translation_placeholders")
            ),
            config_subentry_id=self._canonical_config_subentry(initial_subentries),
            config_entries_subentries=self._normalize_subentries(initial_subentries),
        )
        self._store(entry)
        self.created.append(
            {
                "config_entry_id": config_entry_id,
                "identifiers": set(identifiers),
                "manufacturer": manufacturer,
                "model": model,
                "name": name,
                "via_device_id": via_device_id,
                "via_device": via_device,
                "sw_version": kwargs.get("sw_version"),
                "entry_type": kwargs.get("entry_type"),
                "configuration_url": kwargs.get("configuration_url"),
                "translation_key": kwargs.get("translation_key"),
                "translation_placeholders": kwargs.get("translation_placeholders"),
                "config_subentry_id": kwargs.get("config_subentry_id"),
            }
        )
        return entry

    def async_update_device(
        self,
        *,
        device_id: str,
        new_identifiers: Iterable[tuple[str, str]] | None = None,
        via_device_id: str | None = None,
        name: str | None | object = _UNSET,
        translation_key: str | None = None,
        translation_placeholders: Mapping[str, str] | None = None,
        add_config_entry_id: str | None = None,
        add_config_subentry_id: str | None = None,
        remove_config_entry_id: str | None = None,
        remove_config_subentry_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        entry = self._devices.get(device_id)
        if entry is None:
            raise AssertionError(f"Unknown device_id {device_id}")

        mapping = {
            entry_id: set(subentries)
            for entry_id, subentries in entry.config_entries_subentries.items()
        }
        config_entries = set(entry.config_entries)

        replace_kwargs: dict[str, Any] = {}
        if new_identifiers is not None:
            replace_kwargs["identifiers"] = frozenset(new_identifiers)
        if via_device_id is not None:
            replace_kwargs["via_device_id"] = via_device_id
        if name is not _UNSET:
            replace_kwargs["name"] = cast(str | None, name)
        if translation_key is not None:
            replace_kwargs["translation_key"] = translation_key
        if translation_placeholders is not None:
            replace_kwargs["translation_placeholders"] = dict(translation_placeholders)
        for field in ("manufacturer", "model", "sw_version", "entry_type", "configuration_url"):
            if field in kwargs:
                replace_kwargs[field] = kwargs[field]

        if add_config_entry_id:
            config_entries.add(add_config_entry_id)
            subentries = mapping.setdefault(add_config_entry_id, set())
            if add_config_subentry_id is None:
                subentries.add(None)
            else:
                subentries.add(add_config_subentry_id)
        elif add_config_subentry_id is not None:
            raise AssertionError(
                "add_config_subentry_id provided without add_config_entry_id"
            )

        if remove_config_entry_id:
            subentries = mapping.get(remove_config_entry_id)
            if remove_config_subentry_id is None:
                if subentries is not None:
                    subentries.discard(None)
                    if not subentries:
                        mapping.pop(remove_config_entry_id, None)
                        config_entries.discard(remove_config_entry_id)
            elif subentries is not None:
                subentries.discard(remove_config_subentry_id)
                if not subentries:
                    mapping.pop(remove_config_entry_id, None)
                    config_entries.discard(remove_config_entry_id)

        replace_kwargs["config_entries"] = frozenset(config_entries)
        replace_kwargs["config_entries_subentries"] = self._normalize_subentries(mapping)
        replace_kwargs["config_subentry_id"] = self._canonical_config_subentry(mapping)

        new_entry = replace(entry, **replace_kwargs)
        self._store(new_entry)
        self.updated.append(
            {
                "device_id": device_id,
                "new_identifiers": None
                if new_identifiers is None
                else set(new_identifiers),
                "via_device_id": via_device_id,
                "name": None if name is _UNSET else cast(str | None, name),
                "translation_key": translation_key,
                "translation_placeholders": translation_placeholders,
                "config_entry_id": add_config_entry_id,
                "config_subentry_id": add_config_subentry_id,
                "add_config_entry_id": add_config_entry_id,
                "add_config_subentry_id": add_config_subentry_id,
                "remove_config_entry_id": remove_config_entry_id,
                "remove_config_subentry_id": remove_config_subentry_id,
                "manufacturer": kwargs.get("manufacturer"),
                "model": kwargs.get("model"),
                "sw_version": kwargs.get("sw_version"),
                "entry_type": kwargs.get("entry_type"),
                "configuration_url": kwargs.get("configuration_url"),
            }
        )

    def seed_device(
        self,
        *,
        identifiers: Iterable[tuple[str, str]],
        config_entry_id: str,
        name: str | None = None,
        via_device_id: str | None = None,
        config_subentry_id: str | None = None,
    ) -> _FrozenDeviceEntry:
        device_id = self._next_id(config_entry_id)
        initial_subentries: dict[str, set[str | None]] = {
            config_entry_id: {config_subentry_id} if config_subentry_id else {None}
        }
        entry = _FrozenDeviceEntry(
            id=device_id,
            identifiers=frozenset(identifiers),
            config_entries=frozenset({config_entry_id}),
            name=name,
            via_device_id=via_device_id,
            config_subentry_id=self._canonical_config_subentry(initial_subentries),
            config_entries_subentries=self._normalize_subentries(initial_subentries),
        )
        self._store(entry)
        return entry


class _TranslationRejectingRegistry(_FakeDeviceRegistry):
    """Device registry that rejects translation metadata for service devices."""

    def __init__(self) -> None:
        super().__init__()
        self.create_attempts: list[dict[str, bool]] = []
        self.update_attempts: list[dict[str, bool]] = []

    def async_get_or_create(
        self,
        *,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        manufacturer: str,
        model: str,
        name: str | None = None,
        via_device_id: str | None = None,
        via_device: tuple[str, str] | None = None,
        **kwargs: Any,
    ) -> _FakeDeviceEntry:
        has_translation_key = "translation_key" in kwargs
        has_translation_placeholders = "translation_placeholders" in kwargs
        self.create_attempts.append(
            {
                "has_translation_key": has_translation_key,
                "has_translation_placeholders": has_translation_placeholders,
            }
        )
        if has_translation_key or has_translation_placeholders:
            raise TypeError("unexpected keyword argument 'translation_key'")
        return super().async_get_or_create(
            config_entry_id=config_entry_id,
            identifiers=identifiers,
            manufacturer=manufacturer,
            model=model,
            name=name,
            via_device_id=via_device_id,
            via_device=via_device,
            **kwargs,
        )

    def async_update_device(self, *, device_id: str, **kwargs: Any) -> None:
        has_translation_key = "translation_key" in kwargs
        has_translation_placeholders = "translation_placeholders" in kwargs
        self.update_attempts.append(
            {
                "has_translation_key": has_translation_key,
                "has_translation_placeholders": has_translation_placeholders,
            }
        )
        if has_translation_key or has_translation_placeholders:
            raise TypeError("unexpected keyword argument 'translation_key'")
        return super().async_update_device(device_id=device_id, **kwargs)

    def async_get(self, device_id: str) -> _FakeDeviceEntry | None:
        for device in self.devices:
            if device.id == device_id:
                return device
        return None


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> _FakeDeviceRegistry:
    """Patch Home Assistant's device registry helper with a lightweight stub."""

    _FakeDeviceEntry._counter = 0
    if not hasattr(dr, "DeviceEntryType"):
        monkeypatch.setattr(
            dr,
            "DeviceEntryType",
            SimpleNamespace(SERVICE="service"),
            raising=False,
        )
    registry = _FakeDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)
    return registry


@pytest.fixture
def frozen_registry(monkeypatch: pytest.MonkeyPatch) -> _FrozenDeviceRegistry:
    """Patch the device registry helper with a frozen-entry stub."""

    if not hasattr(dr, "DeviceEntryType"):
        monkeypatch.setattr(
            dr,
            "DeviceEntryType",
            SimpleNamespace(SERVICE="service"),
            raising=False,
        )
    registry = _FrozenDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)
    return registry


def _build_entry_with_subentries(entry_id: str) -> SimpleNamespace:
    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry_id}-service",
        subentry_id=_stable_subentry_id(entry_id, SERVICE_SUBENTRY_KEY),
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "visible_device_ids": []}),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-tracker",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    return SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={
            service_subentry.subentry_id: service_subentry,
            tracker_subentry.subentry_id: tracker_subentry,
        },
        runtime_data=None,
        service_subentry_id=service_subentry.subentry_id,
        tracker_subentry_id=tracker_subentry.subentry_id,
    )


def _prepare_coordinator_for_registry(
    coordinator: GoogleFindMyCoordinator, entry: SimpleNamespace
) -> None:
    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = []
    coordinator._enabled_poll_device_ids = set()
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 5
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._subentry_manager = None
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )


def test_devices_register_without_service_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Newly created tracker devices must remain standalone without service parents."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.created[0]["identifiers"] == {(DOMAIN, "entry-42:abc123")}
    assert fake_registry.created[0]["via_device"] is None
    assert fake_registry.created[0]["via_device_id"] is None
    assert fake_registry.updated == []
    assert (
        fake_registry.created[0]["config_subentry_id"] == entry.tracker_subentry_id
    )


def test_hub_entry_skips_registry_updates(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Hub config entries must not claim tracker devices in the registry."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-hub")
    entry.data = {"subentry_type": SUBENTRY_TYPE_HUB}
    entry.subentries = {}
    _prepare_coordinator_for_registry(coordinator, entry)

    devices = [{"id": "ghost", "name": "Ghost"}]
    created = coordinator._ensure_registry_for_devices(devices=devices, ignored=set())

    assert created == 0
    assert fake_registry.created == []
    assert fake_registry.updated == []


def test_legacy_device_migrates_without_service_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Legacy tracker devices keep their standalone status during migration."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    legacy = _FakeDeviceEntry(
        identifiers={(DOMAIN, "abc123")},
        config_entry_id="entry-42",
        name=None,
        via_device_id=None,
    )
    fake_registry.devices.append(legacy)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert legacy.via_device_id is None
    assert legacy.identifiers == {(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")}
    assert fake_registry.updated[0]["via_device_id"] is None
    assert fake_registry.updated[0]["add_config_subentry_id"] == entry.tracker_subentry_id
    assert fake_registry.updated[0]["add_config_entry_id"] == entry.entry_id
    assert fake_registry.updated[-1]["remove_config_entry_id"] == entry.entry_id
    assert fake_registry.updated[-1]["remove_config_subentry_id"] is None
    assert legacy.name == "Pixel"
    assert legacy.config_subentry_id == entry.tracker_subentry_id


def test_frozen_registry_updates_do_not_raise(
    frozen_registry: _FrozenDeviceRegistry,
) -> None:
    """Registry sync handles frozen DeviceEntry objects without direct mutation."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._ensure_service_device_exists()

    legacy = frozen_registry.seed_device(
        identifiers={(DOMAIN, "abc123")},
        config_entry_id="entry-42",
    )

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices

    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert frozen_registry.updated
    assert frozen_registry.async_get(legacy.id) is not None


def test_frozen_legacy_device_merges_identifiers(
    frozen_registry: _FrozenDeviceRegistry,
) -> None:
    """Legacy identifiers merge into a single frozen DeviceEntry with full metadata."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._ensure_service_device_exists()

    legacy = frozen_registry.seed_device(
        identifiers={(DOMAIN, "abc123")},
        config_entry_id="entry-42",
    )

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices

    coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    refreshed = frozen_registry.async_get(legacy.id)
    assert refreshed is not None
    assert refreshed.identifiers == frozenset(
        {(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")}
    )
    assert refreshed.via_device_id is None
    assert refreshed.config_subentry_id == entry.tracker_subentry_id


def test_existing_device_remains_standalone(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Existing namespaced devices keep operating without a service-device parent."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Pixel",
        via_device_id=None,
    )
    fake_registry.devices.append(existing)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.updated[0]["via_device_id"] is None
    assert existing.via_device_id is None
    assert fake_registry.updated[0]["add_config_subentry_id"] == entry.tracker_subentry_id
    assert fake_registry.updated[0]["add_config_entry_id"] == entry.entry_id
    assert fake_registry.updated[-1]["remove_config_entry_id"] == entry.entry_id
    assert fake_registry.updated[-1]["remove_config_subentry_id"] is None
    assert existing.config_subentry_id == entry.tracker_subentry_id


def test_existing_device_backfills_config_subentry(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Existing devices missing config_subentry_id are linked to the tracker subentry."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "abc123"), (DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Pixel",
        via_device_id="svc-device-1",
        config_subentry_id=None,
    )
    fake_registry.devices.append(existing)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices

    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert len(fake_registry.updated) >= 1
    payload = fake_registry.updated[0]
    assert payload["device_id"] == existing.id
    assert payload["add_config_subentry_id"] == entry.tracker_subentry_id
    assert payload["add_config_entry_id"] == entry.entry_id
    assert payload["via_device_id"] is None
    assert fake_registry.updated[-1]["remove_config_entry_id"] == entry.entry_id
    assert fake_registry.updated[-1]["remove_config_subentry_id"] is None
    assert existing.config_subentry_id == entry.tracker_subentry_id
    assert existing.via_device_id is None


def test_existing_device_name_refresh_does_not_readd_hub_link(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Name refreshes avoid touching config-entry linkage for tracker devices."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Old Label",
        via_device_id="svc-device-1",
        config_subentry_id=entry.tracker_subentry_id,
    )
    fake_registry.devices.append(existing)

    devices = [{"id": "abc123", "name": "Fresh Label"}]
    coordinator.data = devices

    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.updated
    payload = fake_registry.updated[-1]
    assert payload["name"] == "Fresh Label"
    assert payload["config_subentry_id"] is None
    assert payload["via_device_id"] is None
    assert payload["add_config_entry_id"] is None
    assert payload["remove_config_entry_id"] is None
    assert existing.via_device_id is None
    assert existing.config_subentry_id == entry.tracker_subentry_id


def test_existing_device_parent_clear_keeps_subentry(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Clearing the tracker parent retains the subentry association."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_id = "svc-device-1"

    existing = _FakeDeviceEntry(
        identifiers={(DOMAIN, "entry-42:abc123")},
        config_entry_id="entry-42",
        name="Pixel",
        via_device_id="svc-device-1",
        config_subentry_id=entry.tracker_subentry_id,
    )
    fake_registry.devices.append(existing)

    devices = [{"id": "abc123", "name": "Pixel"}]
    coordinator.data = devices

    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    assert fake_registry.updated
    payload = fake_registry.updated[-1]
    assert payload["device_id"] == existing.id
    assert payload["config_subentry_id"] is None
    assert payload["via_device_id"] is None
    assert payload["add_config_entry_id"] is None
    assert payload["remove_config_entry_id"] is None
    assert existing.via_device_id is None
    assert existing.config_subentry_id == entry.tracker_subentry_id


def test_service_device_creation_does_not_modify_tracker_parents(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Tracker devices created before the service device stay standalone after creation."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)
    coordinator._service_device_ready = False
    coordinator._service_device_id = None

    devices = [
        {"id": "abc123", "name": "Pixel"},
        {"id": "def456", "name": "Tablet"},
    ]

    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(devices, set())

    assert created == 2
    assert all(entry["via_device"] is None for entry in fake_registry.created)
    assert all(entry["via_device_id"] is None for entry in fake_registry.created)

    # Service-device creation should not mutate tracker device parentage.
    coordinator._ensure_service_device_exists()

    service_ident = service_device_identifier("entry-42")
    service_config_identifier = _service_subentry_identifier(entry)
    service_entry = next(
        entry for entry in fake_registry.devices if service_ident in entry.identifiers
    )
    assert service_entry.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert service_entry.translation_placeholders == {}
    assert service_entry.config_subentry_id == entry.service_subentry_id
    assert service_entry.identifiers == {service_ident, service_config_identifier}

    for device_entry in fake_registry.devices:
        if {service_ident, service_config_identifier} & device_entry.identifiers:
            continue
        assert device_entry.via_device_id is None
        assert device_entry.config_subentry_id == entry.tracker_subentry_id


def test_service_device_translation_rejection_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry helpers rejecting translation kwargs should raise TypeError."""

    _FakeDeviceEntry._counter = 0

    if not hasattr(dr, "DeviceEntryType"):
        monkeypatch.setattr(
            dr,
            "DeviceEntryType",
            SimpleNamespace(SERVICE="service"),
            raising=False,
        )

    registry = _TranslationRejectingRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-translation")
    _prepare_coordinator_for_registry(coordinator, entry)

    with pytest.raises(TypeError):
        coordinator._ensure_service_device_exists()

    assert registry.create_attempts == [
        {"has_translation_key": True, "has_translation_placeholders": True}
    ]
    assert registry.devices == []


def test_service_device_missing_translation_triggers_update(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Service device without translation metadata should be updated."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-no-translation-update")
    _prepare_coordinator_for_registry(coordinator, entry)

    service_ident = service_device_identifier(entry.entry_id)
    service_subentry_ident = _service_subentry_identifier(entry)
    existing_service = _FakeDeviceEntry(
        identifiers={service_ident, service_subentry_ident},
        config_entry_id=entry.entry_id,
        name=None,
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        sw_version=INTEGRATION_VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        translation_key=None,
        translation_placeholders=None,
        config_subentry_id=entry.service_subentry_id,
    )
    fake_registry.devices.append(existing_service)

    coordinator._ensure_service_device_exists()

    assert len(fake_registry.updated) == 1
    metadata = fake_registry.updated[0]
    assert metadata["device_id"] == existing_service.id
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id


def test_service_device_updates_add_translation(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Existing service devices gain translation metadata when missing."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-42")
    _prepare_coordinator_for_registry(coordinator, entry)

    service_ident = service_device_identifier("entry-42")
    legacy_service = _FakeDeviceEntry(
        identifiers={service_ident},
        config_entry_id="entry-42",
        name=None,
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        sw_version=INTEGRATION_VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        translation_key=None,
        translation_placeholders=None,
    )
    fake_registry.devices.append(legacy_service)

    coordinator._ensure_service_device_exists()

    assert legacy_service.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert legacy_service.translation_placeholders == {}
    assert legacy_service.config_subentry_id == entry.service_subentry_id
    service_subentry_ident = _service_subentry_identifier(entry)
    assert legacy_service.identifiers == {service_ident, service_subentry_ident}
    assert fake_registry.updated
    metadata = fake_registry.updated[0]
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id
    assert metadata["new_identifiers"] == {service_ident, service_subentry_ident}


def test_service_device_update_uses_add_config_entry_id() -> None:
    """Service device refreshes must link config entries via add_config_entry_id."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-88")
    _prepare_coordinator_for_registry(coordinator, entry)

    hass = coordinator.hass
    registry = dr.async_get(hass)

    service_ident = service_device_identifier(entry.entry_id)
    existing = registry.async_get_or_create(  # type: ignore[attr-defined]
        config_entry_id=entry.entry_id,
        identifiers={service_ident},
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        name="Existing Hub",
        sw_version="1.0.0",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url="https://example.invalid",
        translation_key=None,
        translation_placeholders={},
        config_subentry_id=None,
    )

    coordinator._service_device_ready = True
    coordinator._service_device_id = existing.id

    registry.updated.clear()  # type: ignore[attr-defined]

    coordinator._ensure_service_device_exists()

    assert registry.updated, "Service device update should have been recorded"
    payload = registry.updated[-1]  # type: ignore[index]
    assert payload["add_config_entry_id"] == entry.entry_id
    assert payload["config_entry_id"] is None
    assert payload["config_subentry_id"] == entry.service_subentry_id


def test_service_device_preserves_user_defined_name(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """User-defined service device names should not be cleared during updates."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-99")
    _prepare_coordinator_for_registry(coordinator, entry)

    service_ident = service_device_identifier("entry-99")
    legacy_service = _FakeDeviceEntry(
        identifiers={service_ident},
        config_entry_id="entry-99",
        name="Custom Hub",
        manufacturer=SERVICE_DEVICE_MANUFACTURER,
        model=SERVICE_DEVICE_MODEL,
        sw_version=INTEGRATION_VERSION,
        entry_type=dr.DeviceEntryType.SERVICE,
        translation_key=None,
        translation_placeholders=None,
        config_subentry_id=None,
    )
    legacy_service.name_by_user = "Custom Hub"
    fake_registry.devices.append(legacy_service)

    coordinator._ensure_service_device_exists()

    assert legacy_service.name == "Custom Hub"
    assert legacy_service.translation_key == SERVICE_DEVICE_TRANSLATION_KEY
    assert legacy_service.translation_placeholders == {}
    assert legacy_service.config_subentry_id == entry.service_subentry_id
    service_subentry_ident = _service_subentry_identifier(entry)
    assert legacy_service.identifiers == {service_ident, service_subentry_ident}
    assert fake_registry.updated
    metadata = fake_registry.updated[0]
    assert metadata["translation_key"] == SERVICE_DEVICE_TRANSLATION_KEY
    assert metadata["translation_placeholders"] == {}
    assert metadata["config_subentry_id"] == entry.service_subentry_id
    assert metadata["new_identifiers"] == {service_ident, service_subentry_ident}


def test_rebuild_flow_creates_devices_without_service_parent(
    fake_registry: _FakeDeviceRegistry,
) -> None:
    """Safe-mode rebuild path recreates tracker devices without service parents."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    entry = _build_entry_with_subentries("entry-77")
    _prepare_coordinator_for_registry(coordinator, entry)

    # Simulate a safe-mode rebuild: service device removed, pending queue cleared.
    coordinator._service_device_ready = False
    coordinator._service_device_id = None

    devices = [{"id": "ghi789", "name": "Phone"}]
    coordinator.data = devices
    created = coordinator._ensure_registry_for_devices(
        devices=devices,
        ignored=set(),
    )

    assert created == 1
    metadata = fake_registry.created[0]
    assert metadata["via_device"] is None
    assert metadata["via_device_id"] is None
    assert metadata["config_subentry_id"] == entry.tracker_subentry_id


@pytest.mark.asyncio
async def test_relink_subentry_entities_repairs_mislinked_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entity healing re-links tracker/service entities to the correct devices."""

    entry = _build_entry_with_subentries("entry-heal")
    hass = SimpleNamespace(data={})

    service_identifier = service_device_identifier(entry.entry_id)
    service_device = SimpleNamespace(
        id="device-service",
        identifiers={
            service_identifier,
            (DOMAIN, f"{entry.entry_id}:{entry.service_subentry_id}:service"),
        },
        config_entries={entry.entry_id},
        entry_type=getattr(dr.DeviceEntryType, "SERVICE", "service"),
        config_subentry_id=entry.service_subentry_id,
    )

    tracker_device = SimpleNamespace(
        id="device-tracker",
        identifiers={
            (DOMAIN, f"{entry.entry_id}:{entry.tracker_subentry_id}:abc123"),
            (DOMAIN, f"{entry.entry_id}:abc123"),
            (DOMAIN, "abc123"),
        },
        config_entries={entry.entry_id},
        entry_type=None,
        config_subentry_id=entry.tracker_subentry_id,
    )

    class _DeviceRegistryStub:
        def __init__(self) -> None:
            self.devices = {
                service_device.id: service_device,
                tracker_device.id: tracker_device,
            }

        def async_get(self, device_id: str) -> Any | None:
            return self.devices.get(device_id)

        def async_get_device(
            self, *, identifiers: set[tuple[str, str]]
        ) -> Any | None:
            for device in self.devices.values():
                device_idents = getattr(device, "identifiers", set())
                if identifiers & set(device_idents):
                    return device
            return None

    class _EntityRegistryStub:
        def __init__(self, entries: list[Any]) -> None:
            self.entities = {entry.entity_id: entry for entry in entries}
            self.updated: list[tuple[str, dict[str, Any]]] = []

        def async_entries_for_config_entry(
            self, config_entry_id: str
        ) -> tuple[Any, ...]:
            return tuple(
                entry
                for entry in self.entities.values()
                if getattr(entry, "config_entry_id", None) == config_entry_id
            )

        def async_update_entity(self, entity_id: str, **changes: Any) -> None:
            entry = self.entities[entity_id]
            if "device_id" in changes:
                entry.device_id = changes["device_id"]
            self.updated.append((entity_id, dict(changes)))

    tracker_entity = SimpleNamespace(
        entity_id="device_tracker.googlefindmy_tracker",
        domain="device_tracker",
        platform=DOMAIN,
        unique_id=f"{entry.entry_id}:{entry.tracker_subentry_id}:abc123",
        config_entry_id=entry.entry_id,
        device_id=service_device.id,
    )

    sensor_entity = SimpleNamespace(
        entity_id="sensor.googlefindmy_last_seen",
        domain="sensor",
        platform=DOMAIN,
        unique_id=(
            f"{DOMAIN}_{entry.entry_id}_{entry.tracker_subentry_id}_abc123_last_seen"
        ),
        config_entry_id=entry.entry_id,
        device_id=service_device.id,
    )

    legacy_sensor_entity = SimpleNamespace(
        entity_id="sensor.googlefindmy_legacy_last_seen",
        domain="sensor",
        platform=DOMAIN,
        unique_id=(
            f"{DOMAIN}_{entry.tracker_subentry_id}_abc123_last_seen"
        ),
        config_entry_id=entry.entry_id,
        device_id=service_device.id,
    )

    stats_entity = SimpleNamespace(
        entity_id="sensor.googlefindmy_background_updates",
        domain="sensor",
        platform=DOMAIN,
        unique_id=(
            f"{DOMAIN}_{entry.entry_id}_{entry.service_subentry_id}_background_updates"
        ),
        config_entry_id=entry.entry_id,
        device_id=tracker_device.id,
    )

    binary_sensor_entity = SimpleNamespace(
        entity_id="binary_sensor.googlefindmy_polling",
        domain="binary_sensor",
        platform=DOMAIN,
        unique_id=f"{entry.entry_id}:{entry.service_subentry_id}:polling",
        config_entry_id=entry.entry_id,
        device_id=tracker_device.id,
    )

    entity_registry = _EntityRegistryStub(
        [
            tracker_entity,
            sensor_entity,
            legacy_sensor_entity,
            stats_entity,
            binary_sensor_entity,
        ]
    )
    device_registry = _DeviceRegistryStub()

    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)

    await _async_relink_subentry_entities(hass, entry)

    assert tracker_entity.device_id == tracker_device.id
    assert sensor_entity.device_id == tracker_device.id
    assert legacy_sensor_entity.device_id == tracker_device.id
    assert stats_entity.device_id == service_device.id
    assert binary_sensor_entity.device_id == service_device.id
    assert {entity_id for entity_id, _ in entity_registry.updated} == {
        tracker_entity.entity_id,
        sensor_entity.entity_id,
        legacy_sensor_entity.entity_id,
        stats_entity.entity_id,
        binary_sensor_entity.entity_id,
    }

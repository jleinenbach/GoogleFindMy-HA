# tests/test_config_entry_migration.py
"""Regression tests for config entry migrations and registry repair helpers."""

from __future__ import annotations

import asyncio
from importlib import import_module

# tests/test_config_entry_migration.py

import asyncio
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    OPT_DEVICE_POLL_DELAY,
    DOMAIN,
    LEGACY_SERVICE_IDENTIFIER,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)
from custom_components.googlefindmy.email import normalize_email, unique_account_id


@dataclass(slots=True)
class _MigrationTestEntry:
    """Lightweight config entry stand-in for migration tests."""

    entry_id: str
    data: dict[str, Any]
    title: str = ""
    options: dict[str, Any] = None  # type: ignore[assignment]
    version: int = 1
    unique_id: str | None = None

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = {}

    domain: str = DOMAIN

    def add_to_hass(self, hass: "_MigrationHass") -> None:
        hass.config_entries.add_entry(self)


class _MigrationConfigEntriesManager:
    """Capture updates applied during migration for assertions."""

    def __init__(self) -> None:
        self._entries: dict[str, _MigrationTestEntry] = {}
        self.updated: list[tuple[_MigrationTestEntry, dict[str, Any]]] = []
        self.raise_on_unique_id = False

    def add_entry(self, entry: _MigrationTestEntry) -> None:
        self._entries[entry.entry_id] = entry

    def async_entries(self, domain: str | None = None) -> list[_MigrationTestEntry]:
        if domain is None:
            return list(self._entries.values())
        return [entry for entry in self._entries.values() if entry.domain == domain]

    def async_update_entry(
        self, entry: _MigrationTestEntry, **kwargs: Any
    ) -> None:
        payload = dict(kwargs)
        if self.raise_on_unique_id and "unique_id" in payload:
            self.updated.append((entry, payload))
            raise ValueError("unique_id collision")
        self.updated.append((entry, payload))
        if "data" in payload:
            entry.data = payload["data"]
        if "options" in payload:
            entry.options = payload["options"]
        if "title" in payload:
            entry.title = payload["title"]
        if "unique_id" in payload:
            entry.unique_id = payload["unique_id"]
        if "version" in payload:
            entry.version = payload["version"]


@dataclass(slots=True)
class _MigrationHass:
    """Minimal Home Assistant stub exposing config entries manager."""

    config_entries: _MigrationConfigEntriesManager


def _make_hass_with_entries(
    *entries: _MigrationTestEntry,
) -> _MigrationHass:
    manager = _MigrationConfigEntriesManager()
    hass = _MigrationHass(config_entries=manager)
    for entry in entries:
        entry.add_to_hass(hass)
    return hass


def test_async_migrate_entry_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration should set metadata and unique_id when no duplicates exist."""

    integration = import_module("custom_components.googlefindmy")

    entry = _MigrationTestEntry(
        entry_id="entry-1",
        data={DATA_SECRET_BUNDLE: {"username": "User@Example.com"}},
        title="Legacy",
        version=1,
    )
    hass = _make_hass_with_entries(entry)

    created_issues: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        integration.ir,
        "async_create_issue",
        lambda *args, **kwargs: created_issues.append((args, kwargs)),
    )
    monkeypatch.setattr(integration.ir, "async_delete_issue", lambda *args, **kwargs: None)

    result = asyncio.run(integration.async_migrate_entry(hass, entry))

    assert result is True
    expected_unique_id = unique_account_id(normalize_email("User@Example.com"))
    assert entry.unique_id == expected_unique_id
    assert entry.title == "User@Example.com"
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.data[CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert created_issues == []
    assert hass.config_entries.updated
    update_payload = hass.config_entries.updated[0][1]
    assert update_payload["unique_id"] == expected_unique_id
    assert update_payload["version"] == integration.CONFIG_ENTRY_VERSION


def test_async_migrate_entry_detects_duplicate_before_unique_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate accounts should raise a repair issue and skip unique_id."""

    integration = import_module("custom_components.googlefindmy")

    primary = _MigrationTestEntry(
        entry_id="primary",
        data={CONF_GOOGLE_EMAIL: "duplicate@example.com"},
        title="Primary",
        version=integration.CONFIG_ENTRY_VERSION,
        unique_id=unique_account_id(normalize_email("duplicate@example.com")),
    )
    duplicate = _MigrationTestEntry(
        entry_id="duplicate",
        data={DATA_SECRET_BUNDLE: {"username": "Duplicate@Example.com"}},
        title="Secondary",
        version=1,
    )
    hass = _make_hass_with_entries(primary, duplicate)

    issue_helper = Mock(
        wraps=integration._log_duplicate_and_raise_repair_issue
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy._log_duplicate_and_raise_repair_issue",
        issue_helper,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_delete_issue",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(integration.async_migrate_entry(hass, duplicate))

    assert result is True
    assert duplicate.unique_id is None
    assert duplicate.version == integration.CONFIG_ENTRY_VERSION
    assert hass.config_entries.updated
    update_payload = hass.config_entries.updated[0][1]
    assert "unique_id" not in update_payload
    assert issue_helper.call_count == 1
    assert issue_helper.call_args.kwargs["cause"] == "pre_migration_duplicate"


def test_async_migrate_entry_value_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """ValueError when writing unique_id should trigger a retry without it."""

    integration = import_module("custom_components.googlefindmy")

    entry = _MigrationTestEntry(
        entry_id="entry-value-error",
        data={CONF_GOOGLE_EMAIL: "fallback@example.com"},
        title="Fallback",
        version=1,
    )
    hass = _make_hass_with_entries(entry)
    hass.config_entries.raise_on_unique_id = True

    issue_helper = Mock(
        wraps=integration._log_duplicate_and_raise_repair_issue
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy._log_duplicate_and_raise_repair_issue",
        issue_helper,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_delete_issue",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(integration.async_migrate_entry(hass, entry))

    assert result is True
    assert len(hass.config_entries.updated) == 2
    first_payload = hass.config_entries.updated[0][1]
    second_payload = hass.config_entries.updated[1][1]
    assert "unique_id" in first_payload
    assert "unique_id" not in second_payload
    assert issue_helper.call_count == 1
    assert issue_helper.call_args.kwargs["cause"] == "unique_id_conflict"


def test_async_migrate_entry_recovers_partial_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry stuck from a previous migration should still be updated."""

    integration = import_module("custom_components.googlefindmy")

    primary = _MigrationTestEntry(
        entry_id="existing",
        data={CONF_GOOGLE_EMAIL: "recovery@example.com"},
        title="Existing",
        version=integration.CONFIG_ENTRY_VERSION,
        unique_id=unique_account_id(normalize_email("recovery@example.com")),
    )
    stuck = _MigrationTestEntry(
        entry_id="stuck",
        data={DATA_SECRET_BUNDLE: {"username": "Recovery@Example.com"}},
        title="",
        version=integration.CONFIG_ENTRY_VERSION,
        unique_id=None,
    )
    hass = _make_hass_with_entries(primary, stuck)

    issue_helper = Mock(
        wraps=integration._log_duplicate_and_raise_repair_issue
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy._log_duplicate_and_raise_repair_issue",
        issue_helper,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_delete_issue",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(integration.async_migrate_entry(hass, stuck))

    assert result is True
    assert hass.config_entries.updated
    payload = hass.config_entries.updated[0][1]
    assert "unique_id" not in payload
    assert "data" in payload
    assert issue_helper.call_count == 1
    assert issue_helper.call_args.kwargs["cause"] == "pre_migration_duplicate"



class _StubConfigEntry:
    """Minimal ConfigEntry stand-in for migration tests."""

    def __init__(self) -> None:
        self.entry_id = "test-entry"
        self.version = 1
        self.title = ""
        self.data: dict[str, Any] = {
            DATA_SECRET_BUNDLE: {"username": "User@Example.com"},
            OPT_DEVICE_POLL_DELAY: 7,
        }
        self.options: dict[str, Any] = {}
        self.unique_id: str | None = None
        self.subentries: dict[str, Any] = {}


class _StubConfigEntries:
    """Capture config entry updates performed by the migration."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self._entry = entry
        self.updated: list[dict[str, Any]] = []

    def async_update_entry(self, entry: _StubConfigEntry, **kwargs: Any) -> None:
        assert entry is self._entry
        self.updated.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "title" in kwargs:
            entry.title = kwargs["title"]
        if "unique_id" in kwargs:
            entry.unique_id = kwargs["unique_id"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    def async_entries(self, domain: str | None = None) -> list[_StubConfigEntry]:
        del domain
        return [self._entry]


class _FakeRegistryEntry:
    """Minimal entity registry entry used for unique_id migration tests."""

    def __init__(
        self,
        *,
        entity_id: str,
        domain: str,
        platform: str,
        unique_id: str,
        config_entry_id: str,
        device_id: str | None = None,
        disabled_by: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.domain = domain
        self.platform = platform
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id
        self.device_id = device_id
        self.disabled_by = disabled_by


class _FakeEntityRegistry:
    """Test double for the Home Assistant entity registry."""

    def __init__(self) -> None:
        self.entities: dict[str, _FakeRegistryEntry] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self.updated: list[str] = []
        self.update_payloads: list[dict[str, Any]] = []

    def add(
        self,
        *,
        entity_id: str,
        domain: str,
        platform: str,
        unique_id: str,
        config_entry_id: str,
        device_id: str | None = None,
        disabled_by: str | None = None,
    ) -> None:
        entry = _FakeRegistryEntry(
            entity_id=entity_id,
            domain=domain,
            platform=platform,
            unique_id=unique_id,
            config_entry_id=config_entry_id,
            device_id=device_id,
            disabled_by=disabled_by,
        )
        self.entities[entity_id] = entry
        self._by_key[(domain, platform, unique_id)] = entity_id

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return self._by_key.get((domain, platform, unique_id))

    def async_update_entity(
        self,
        entity_id: str,
        *,
        new_unique_id: str | None = None,
        device_id: str | None = None,
        disabled_by: str | None = None,
    ) -> None:
        entry = self.entities[entity_id]
        if new_unique_id is not None:
            self._by_key.pop((entry.domain, entry.platform, entry.unique_id), None)
            entry.unique_id = new_unique_id
            self._by_key[(entry.domain, entry.platform, new_unique_id)] = entity_id
        if device_id is not None:
            entry.device_id = device_id
        if disabled_by is not None:
            entry.disabled_by = disabled_by
        self.updated.append(entity_id)
        self.update_payloads.append(
            {
                "new_unique_id": new_unique_id,
                "device_id": device_id,
                "disabled_by": disabled_by,
            }
        )

    def async_entries_for_config_entry(
        self, entry_id: str
    ) -> list[_FakeRegistryEntry]:
        return [
            entry
            for entry in self.entities.values()
            if entry.config_entry_id == entry_id
        ]

    def async_get(self, entity_id: str) -> _FakeRegistryEntry | None:
        return self.entities.get(entity_id)


@dataclass(slots=True)
class _FakeDeviceEntry:
    """Minimal device entry used for device registry simulations."""

    id: str
    identifiers: set[tuple[str, str]]
    config_entries: set[str]
    entry_type: str | None = None


class _FakeDeviceRegistry:
    """Minimal device registry stub used to satisfy migration helpers."""

    def __init__(self) -> None:
        self.devices: dict[str, _FakeDeviceEntry] = {}
        self.lookup_calls: list[set[tuple[str, str]]] = []

    def add(self, device: _FakeDeviceEntry) -> None:
        self.devices[device.id] = device

    def async_get(self, device_id: str | None) -> _FakeDeviceEntry | None:
        if not device_id:
            return None
        return self.devices.get(device_id)

    def async_get_device(
        self,
        *,
        identifiers: Iterable[tuple[str, str]] | None = None,
        connections: Iterable[tuple[str, str]] | None = None,
    ) -> _FakeDeviceEntry | None:
        del connections
        if not identifiers:
            return None
        ident_set = set(identifiers)
        self.lookup_calls.append(ident_set)
        for device in self.devices.values():
            if ident_set.issubset(device.identifiers):
                return device
        return None

    def async_update_device(self, **_kwargs: Any) -> None:  # pragma: no cover - stub
        return None


class _StubHass:
    """Namespace providing the config_entries helper."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self.config_entries = _StubConfigEntries(entry)


@dataclass(slots=True)
class _RelinkEnvironment:
    """Bundle of shared registry fixtures for button relink tests."""

    entry: _StubConfigEntry
    entity_registry: _FakeEntityRegistry
    device_registry: _FakeDeviceRegistry
    tracker_device: _FakeDeviceEntry
    service_device: _FakeDeviceEntry
    integration: Any

    def add_button(
        self,
        *,
        entity_id: str,
        unique_id: str,
        device_id: str | None = None,
    ) -> None:
        """Register a googlefindmy button entity in the fake registry."""

        self.entity_registry.add(
            entity_id=entity_id,
            domain="button",
            platform=self.integration.DOMAIN,
            unique_id=unique_id,
            config_entry_id=self.entry.entry_id,
            device_id=device_id,
        )

    def create_hass(self) -> SimpleNamespace:
        """Return a simple hass placeholder for relink invocations."""

        return SimpleNamespace()


@pytest.fixture
def relink_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> _RelinkEnvironment:
    """Seed fake registries and config entry data for relink scenarios."""

    integration = import_module("custom_components.googlefindmy")

    entry = _StubConfigEntry()
    entry.entry_id = "entry-1"
    subentry = SimpleNamespace(
        subentry_id="tracker",
        data={"features": ("button", "device_tracker")},
    )
    entry.subentries = {subentry.subentry_id: subentry}

    entity_registry = _FakeEntityRegistry()
    device_registry = _FakeDeviceRegistry()

    tracker_device = _FakeDeviceEntry(
        id="tracker-device",
        identifiers={
            (DOMAIN, "entry-1:tracker:abc123"),
            (DOMAIN, "entry-1:abc123"),
            (DOMAIN, "abc123"),
        },
        config_entries={"entry-1"},
    )
    device_registry.add(tracker_device)

    service_device = _FakeDeviceEntry(
        id="service-device",
        identifiers={
            service_device_identifier(entry.entry_id),
            (DOMAIN, LEGACY_SERVICE_IDENTIFIER),
        },
        config_entries={entry.entry_id},
        entry_type=_service_entry_type(integration),
    )
    device_registry.add(service_device)

    monkeypatch.setattr(integration.er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(integration.dr, "async_get", lambda _hass: device_registry)

    return _RelinkEnvironment(
        entry=entry,
        entity_registry=entity_registry,
        device_registry=device_registry,
        tracker_device=tracker_device,
        service_device=service_device,
        integration=integration,
    )


def test_async_migrate_entry_populates_email_and_options() -> None:
    """Legacy entries should gain email metadata and soft-migrate options."""

    integration = import_module("custom_components.googlefindmy")

    entry = _StubConfigEntry()
    hass = _StubHass(entry)

    result = asyncio.run(integration.async_migrate_entry(hass, entry))
    assert result is True
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.data[CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert entry.title == "User@Example.com"
    expected_unique_id = unique_account_id(normalize_email("User@Example.com"))
    assert entry.unique_id == expected_unique_id
    assert entry.options[OPT_DEVICE_POLL_DELAY] == 7

    hass_second = _StubHass(entry)
    second_result = asyncio.run(integration.async_migrate_entry(hass_second, entry))
    assert second_result is True
    assert hass_second.config_entries.updated == []


def test_unique_id_subentry_migration_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing unique_ids should be upgraded to include the subentry identifier."""

    integration = import_module("custom_components.googlefindmy")

    entry = _StubConfigEntry()
    entry.entry_id = "entry-1"
    entry.options = MappingProxyType({"unique_id_migrated": True})
    subentry = SimpleNamespace(
        subentry_id="sub-1",
        data={
            "group_key": TRACKER_SUBENTRY_KEY,
            "features": (
                "binary_sensor",
                "button",
                "device_tracker",
                "sensor",
            ),
        },
        title="Core Tracking",
    )
    entry.subentries = {subentry.subentry_id: subentry}

    hass = _StubHass(entry)

    entity_registry = _FakeEntityRegistry()
    entity_registry.add(
        entity_id="device_tracker.googlefindmy_device_1",
        domain="device_tracker",
        platform=integration.DOMAIN,
        unique_id="entry-1:device-1",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="sensor.googlefindmy_device_1_last_seen",
        domain="sensor",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_device-1_last_seen",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="sensor.googlefindmy_api_updates",
        domain="sensor",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_api_updates_total",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="button.googlefindmy_device_1_play_sound",
        domain="button",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_device-1_play_sound",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="button.googlefindmy_device_2_play_sound",
        domain="button",
        platform=integration.DOMAIN,
        unique_id="googlefindmy_entry-1_tracker_device-2_play_sound",
        config_entry_id="entry-1",
    )
    entity_registry.add(
        entity_id="binary_sensor.googlefindmy_polling",
        domain="binary_sensor",
        platform=integration.DOMAIN,
        unique_id="entry-1:polling",
        config_entry_id="entry-1",
    )

    monkeypatch.setattr(integration.er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(
        integration.dr, "async_get", lambda _hass: _FakeDeviceRegistry()
    )

    asyncio.run(integration._async_migrate_unique_ids(hass, entry))

    tracker_entry = entity_registry.entities["device_tracker.googlefindmy_device_1"]
    assert tracker_entry.unique_id == "entry-1:sub-1:device-1"
    last_seen_entry = entity_registry.entities["sensor.googlefindmy_device_1_last_seen"]
    assert last_seen_entry.unique_id == "googlefindmy_entry-1_sub-1_device-1_last_seen"
    stats_entry = entity_registry.entities["sensor.googlefindmy_api_updates"]
    assert stats_entry.unique_id == "googlefindmy_entry-1_sub-1_api_updates_total"
    button_entry = entity_registry.entities["button.googlefindmy_device_1_play_sound"]
    assert button_entry.unique_id == "googlefindmy_entry-1_sub-1_device-1_play_sound"
    tracker_button_entry = entity_registry.entities[
        "button.googlefindmy_device_2_play_sound"
    ]
    assert (
        tracker_button_entry.unique_id
        == "googlefindmy_entry-1_sub-1_device-2_play_sound"
    )
    binary_entry = entity_registry.entities["binary_sensor.googlefindmy_polling"]
    assert binary_entry.unique_id == "entry-1:sub-1:polling"

    assert entry.options["unique_id_migrated"] is True
    assert entry.options["unique_id_subentry_migrated"] is True
    assert len(entity_registry.updated) >= 5
    assert hass.config_entries.updated


def _service_entry_type(integration: Any) -> str:
    """Return the service entry type constant used by the integration."""

    entry_type = getattr(integration.dr, "DeviceEntryType", None)
    if entry_type is not None:
        return getattr(entry_type, "SERVICE", "service")
    return "service"


@pytest.mark.asyncio
async def test_relink_adds_missing_device_id_new_style(
    relink_environment: _RelinkEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buttons without device_id should link to the physical tracker device."""

    entry = relink_environment.entry
    entity_registry = relink_environment.entity_registry
    tracker_device = relink_environment.tracker_device
    integration = relink_environment.integration

    actions = ("play_sound", "stop_sound", "locate_device")
    entity_ids: list[str] = []
    for index, action in enumerate(actions, start=1):
        entity_id = f"button.googlefindmy_device_{index}_{action}"
        entity_ids.append(entity_id)
        relink_environment.add_button(
            entity_id=entity_id,
            unique_id=(
                f"{integration.DOMAIN}_{entry.entry_id}_tracker_abc123_{action}"
            ),
        )

    registry_snapshot = integration._iter_config_entry_entities(
        entity_registry, entry.entry_id
    )
    assert len(registry_snapshot) == len(entity_ids)

    subentry_map = integration._resolve_subentry_identifier_map(entry)
    parsed_sample = integration._parse_button_unique_id(
        f"{integration.DOMAIN}_{entry.entry_id}_tracker_abc123_{actions[0]}",
        entry,
        subentry_map,
        "tracker",
    )
    assert parsed_sample is not None

    hass = relink_environment.create_hass()
    await integration._async_relink_button_devices(hass, entry)

    assert set(entity_registry.updated) == set(entity_ids)
    for entity_id in entity_ids:
        assert entity_registry.entities[entity_id].device_id == tracker_device.id
    assert relink_environment.device_registry.lookup_calls


@pytest.mark.asyncio
async def test_relink_repairs_service_link(
    relink_environment: _RelinkEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buttons linked to the service device should be reassigned to the tracker."""

    entry = relink_environment.entry
    entity_registry = relink_environment.entity_registry
    tracker_device = relink_environment.tracker_device
    integration = relink_environment.integration

    relink_environment.add_button(
        entity_id="button.googlefindmy_device_play_sound",
        unique_id=(
            f"{integration.DOMAIN}_{entry.entry_id}_tracker_abc123_play_sound"
        ),
        device_id=relink_environment.service_device.id,
    )

    hass = relink_environment.create_hass()
    await integration._async_relink_button_devices(hass, entry)

    entry_record = entity_registry.entities["button.googlefindmy_device_play_sound"]
    assert entry_record.device_id == tracker_device.id
    assert entity_registry.updated == ["button.googlefindmy_device_play_sound"]


@pytest.mark.asyncio
async def test_relink_legacy_unique_id_without_subentry(
    relink_environment: _RelinkEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy unique_ids without subentry information should still relink."""

    entry = relink_environment.entry
    entity_registry = relink_environment.entity_registry
    tracker_device = relink_environment.tracker_device
    integration = relink_environment.integration

    relink_environment.add_button(
        entity_id="button.googlefindmy_device_play_sound",
        unique_id=f"{integration.DOMAIN}_{entry.entry_id}_abc123_play_sound",
    )
    relink_environment.add_button(
        entity_id="button.googlefindmy_device_stop_sound",
        unique_id="abc123_stop_sound",
    )

    hass = relink_environment.create_hass()
    await integration._async_relink_button_devices(hass, entry)

    assert {
        entity_registry.entities["button.googlefindmy_device_play_sound"].device_id,
        entity_registry.entities["button.googlefindmy_device_stop_sound"].device_id,
    } == {tracker_device.id}
    assert set(entity_registry.updated) == {
        "button.googlefindmy_device_play_sound",
        "button.googlefindmy_device_stop_sound",
    }


@pytest.mark.asyncio
async def test_relink_idempotent_when_already_correct(
    relink_environment: _RelinkEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities already linked to the tracker must remain unchanged."""

    entry = relink_environment.entry
    entity_registry = relink_environment.entity_registry
    tracker_device = relink_environment.tracker_device
    integration = relink_environment.integration

    relink_environment.add_button(
        entity_id="button.googlefindmy_device_play_sound",
        unique_id=(
            f"{integration.DOMAIN}_{entry.entry_id}_tracker_abc123_play_sound"
        ),
        device_id=tracker_device.id,
    )

    assert integration._iter_config_entry_entities(entity_registry, entry.entry_id)

    hass = relink_environment.create_hass()
    await integration._async_relink_button_devices(hass, entry)

    assert entity_registry.entities["button.googlefindmy_device_play_sound"].device_id == tracker_device.id
    assert entity_registry.updated == []


@pytest.mark.asyncio
async def test_relink_skips_foreign_entry(
    relink_environment: _RelinkEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entities from a different config entry must not be modified."""

    entry = relink_environment.entry
    entity_registry = relink_environment.entity_registry
    integration = relink_environment.integration

    relink_environment.add_button(
        entity_id="button.googlefindmy_foreign_play_sound",
        unique_id=f"{integration.DOMAIN}_other-entry_tracker_zzz_play_sound",
    )

    hass = relink_environment.create_hass()
    await integration._async_relink_button_devices(hass, entry)

    record = entity_registry.entities["button.googlefindmy_foreign_play_sound"]
    assert record.device_id is None
    assert entity_registry.updated == []

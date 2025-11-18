# tests/test_config_entry_migration.py
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    OPT_MIN_POLL_INTERVAL,
)

if TYPE_CHECKING:
    from tests.conftest import IssueRegistryCapture


@dataclass(slots=True)
class _MigrationTestEntry:
    """Minimal config entry stand-in used by migration tests."""

    entry_id: str
    data: dict[str, Any]
    title: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    unique_id: str | None = None
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED
    subentries: dict[str, Any] = field(default_factory=dict)
    disabled_by: object | None = None

    domain: str = DOMAIN

    def add_to_hass(self, hass: _MigrationHass) -> None:
        hass.config_entries.add_entry(self)


class _MigrationConfigEntriesManager:
    """Capture updates applied during migration for assertions."""

    def __init__(self) -> None:
        self._entries: dict[str, _MigrationTestEntry] = {}
        self.updated: list[tuple[_MigrationTestEntry, dict[str, Any]]] = []
        self.disabled: list[tuple[str, object | None]] = []
        self.removed: list[str] = []
        self.setup_calls: list[str] = []

    def add_entry(self, entry: _MigrationTestEntry) -> None:
        self._entries[entry.entry_id] = entry

    def async_entries(self, domain: str | None = None) -> list[_MigrationTestEntry]:
        if domain is None:
            return list(self._entries.values())
        return [entry for entry in self._entries.values() if entry.domain == domain]

    def async_get_entry(self, entry_id: str) -> _MigrationTestEntry | None:
        return self._entries.get(entry_id)

    def async_get_subentries(self, entry_id: str) -> list[Any]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            return list(subentries.values())
        return []

    def async_update_entry(self, entry: _MigrationTestEntry, **kwargs: Any) -> None:
        self.updated.append((entry, dict(kwargs)))
        data = kwargs.get("data")
        if isinstance(data, dict):
            entry.data = dict(data)
        title = kwargs.get("title")
        if isinstance(title, str):
            entry.title = title
        unique_id = kwargs.get("unique_id")
        if isinstance(unique_id, str):
            entry.unique_id = unique_id
        options = kwargs.get("options")
        if isinstance(options, dict):
            entry.options = dict(options)
        version_value = kwargs.get("version")
        if isinstance(version_value, int):
            entry.version = version_value

    async def async_set_disabled_by(
        self, entry_id: str, disabled_by: object | None
    ) -> None:
        entry = self._entries[entry_id]
        entry.disabled_by = disabled_by
        self.disabled.append((entry_id, disabled_by))

    async def async_remove(self, entry_id: str) -> None:
        self.removed.append(entry_id)
        self._entries.pop(entry_id, None)

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


@dataclass(slots=True)
class _MigrationHass:
    """Minimal Home Assistant stub exposing the config entry manager."""

    config_entries: _MigrationConfigEntriesManager


def _make_hass_with_entries(*entries: _MigrationTestEntry) -> _MigrationHass:
    manager = _MigrationConfigEntriesManager()
    hass = _MigrationHass(config_entries=manager)
    for entry in entries:
        entry.add_to_hass(hass)
    return hass


@pytest.mark.asyncio
async def test_async_migrate_entry_normalizes_metadata(
    issue_registry_capture: IssueRegistryCapture,
) -> None:
    """Migration aligns metadata and upgrades the entry version."""

    integration = import_module("custom_components.googlefindmy")
    entry = _MigrationTestEntry(
        entry_id="legacy",
        data={
            DATA_SECRET_BUNDLE: {"username": "User@Example.com "},
            OPT_MIN_POLL_INTERVAL: 45,
        },
        title="Legacy",
        version=1,
    )
    hass = _make_hass_with_entries(entry)
    capture = issue_registry_capture

    result = await integration.async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.title == "User@Example.com"
    assert entry.data[CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert entry.unique_id == "acct:user@example.com"
    assert entry.options[OPT_MIN_POLL_INTERVAL] == 45
    assert capture.created == []
    assert capture.deleted == [(DOMAIN, f"duplicate_account_{entry.entry_id}")]
    assert len(hass.config_entries.updated) == 3
    first_update = hass.config_entries.updated[0][1]
    assert first_update["data"][CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert first_update["title"] == "User@Example.com"
    assert first_update["unique_id"] == "acct:user@example.com"
    options_update = hass.config_entries.updated[1][1]
    assert options_update["options"][OPT_MIN_POLL_INTERVAL] == 45
    assert hass.config_entries.updated[2][1]["version"] == integration.CONFIG_ENTRY_VERSION


@pytest.mark.asyncio
async def test_async_migrate_entry_handles_duplicate_accounts(
    issue_registry_capture: IssueRegistryCapture,
) -> None:
    """Non-authoritative duplicates are held back and flagged during migration."""

    integration = import_module("custom_components.googlefindmy")
    migration_error_state = getattr(
        ConfigEntryState, "MIGRATION_ERROR", ConfigEntryState.SETUP_ERROR
    )
    authoritative = _MigrationTestEntry(
        entry_id="authoritative",
        data={DATA_SECRET_BUNDLE: {"username": "duplicate@example.com"}},
        title="Primary",
        version=1,
        state=ConfigEntryState.LOADED,
    )
    duplicate = _MigrationTestEntry(
        entry_id="held_back",
        data={
            DATA_SECRET_BUNDLE: {"username": "duplicate@example.com"},
            OPT_MIN_POLL_INTERVAL: 60,
        },
        title="Secondary",
        version=1,
        state=migration_error_state,
    )
    hass = _make_hass_with_entries(authoritative, duplicate)
    capture = issue_registry_capture

    first_result = await integration.async_migrate_entry(hass, duplicate)

    assert first_result is True
    assert duplicate.entry_id in hass.config_entries.removed
    assert hass.config_entries.async_get_entry(duplicate.entry_id) is None
    assert authoritative.state == ConfigEntryState.LOADED
    assert capture.created == []

    updates_before = list(hass.config_entries.updated)
    second_result = await integration.async_migrate_entry(hass, authoritative)

    assert second_result is True
    assert authoritative.version == integration.CONFIG_ENTRY_VERSION
    assert authoritative.title == "duplicate@example.com"
    assert authoritative.unique_id == "acct:duplicate@example.com"
    new_updates = hass.config_entries.updated[len(updates_before) :]
    assert any("version" in update[1] for update in new_updates)
    assert capture.created == []
    assert (
        capture.deleted.count((DOMAIN, f"duplicate_account_{authoritative.entry_id}"))
        >= 1
    )


@pytest.mark.asyncio
async def test_async_migrate_entry_without_email_metadata(
    issue_registry_capture: IssueRegistryCapture,
) -> None:
    """Migration succeeds when no email metadata can be resolved."""

    integration = import_module("custom_components.googlefindmy")
    entry = _MigrationTestEntry(
        entry_id="no_email",
        data={DATA_SECRET_BUNDLE: {"username": "   "}},
        title="NoEmail",
        version=1,
    )
    hass = _make_hass_with_entries(entry)
    capture = issue_registry_capture

    result = await integration.async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.title == "NoEmail"
    assert entry.unique_id is None
    assert entry.options == {}
    assert capture.created == []
    assert capture.deleted == [(DOMAIN, f"duplicate_account_{entry.entry_id}")]
    assert hass.config_entries.updated == [
        (entry, {"version": integration.CONFIG_ENTRY_VERSION})
    ]

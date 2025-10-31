# tests/test_config_entry_migration.py
"""Regression tests for the config entry migration shim."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

import pytest

from custom_components.googlefindmy.const import DOMAIN


@dataclass(slots=True)
class _MigrationTestEntry:
    """Minimal config entry stand-in used by migration tests."""

    entry_id: str
    data: dict[str, Any]
    title: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    unique_id: str | None = None
    subentries: dict[str, Any] = field(default_factory=dict)

    domain: str = DOMAIN

    def add_to_hass(self, hass: "_MigrationHass") -> None:
        hass.config_entries.add_entry(self)


class _MigrationConfigEntriesManager:
    """Capture updates applied during migration for assertions."""

    def __init__(self) -> None:
        self._entries: dict[str, _MigrationTestEntry] = {}
        self.updated: list[tuple[_MigrationTestEntry, dict[str, Any]]] = []

    def add_entry(self, entry: _MigrationTestEntry) -> None:
        self._entries[entry.entry_id] = entry

    def async_entries(self, domain: str | None = None) -> list[_MigrationTestEntry]:
        if domain is None:
            return list(self._entries.values())
        return [entry for entry in self._entries.values() if entry.domain == domain]

    def async_update_entry(self, entry: _MigrationTestEntry, **kwargs: Any) -> None:
        self.updated.append((entry, dict(kwargs)))


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
async def test_async_migrate_entry_defers_to_config_flow(caplog: pytest.LogCaptureFixture) -> None:
    """The migration shim should return True without mutating the entry."""

    integration = import_module("custom_components.googlefindmy")

    entry = _MigrationTestEntry(
        entry_id="legacy",
        data={"secrets_data": {"username": "user@example.com"}},
        title="Legacy",
        version=1,
    )
    hass = _make_hass_with_entries(entry)

    with caplog.at_level(logging.DEBUG):
        result = await integration.async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 1
    assert entry.unique_id is None
    assert hass.config_entries.updated == []
    assert "deferring to config flow" in caplog.text


@pytest.mark.asyncio
async def test_async_migrate_entry_does_not_trigger_duplicate_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate accounts are handled by the config flow, not the shim."""

    integration = import_module("custom_components.googlefindmy")

    primary = _MigrationTestEntry(
        entry_id="primary",
        data={"secrets_data": {"username": "primary@example.com"}},
        title="Primary",
        version=integration.CONFIG_ENTRY_VERSION,
        unique_id="acct:primary@example.com",
    )
    duplicate = _MigrationTestEntry(
        entry_id="duplicate",
        data={"secrets_data": {"username": "primary@example.com"}},
        title="Duplicate",
        version=1,
    )
    hass = _make_hass_with_entries(primary, duplicate)

    issues_raised: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_create_issue",
        lambda *args, **kwargs: issues_raised.append((args, kwargs)),
    )

    result = await integration.async_migrate_entry(hass, duplicate)

    assert result is True
    assert hass.config_entries.updated == []
    assert issues_raised == []
    assert duplicate.unique_id is None
    assert duplicate.version == 1

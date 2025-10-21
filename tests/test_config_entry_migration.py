# tests/test_config_entry_migration.py
"""Regression tests for config entry data migration logic."""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    OPT_DEVICE_POLL_DELAY,
)


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


class _StubHass:
    """Namespace providing the config_entries helper."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self.config_entries = _StubConfigEntries(entry)


def test_async_migrate_entry_populates_email_and_options() -> None:
    """Legacy entries should gain email metadata and soft-migrate options."""

    integration = import_module("custom_components.googlefindmy.__init__")

    entry = _StubConfigEntry()
    hass = _StubHass(entry)

    result = asyncio.run(integration.async_migrate_entry(hass, entry))
    assert result is True
    assert entry.version == integration.CONFIG_ENTRY_VERSION
    assert entry.data[CONF_GOOGLE_EMAIL] == "User@Example.com"
    assert entry.title == "User@Example.com"
    assert entry.unique_id == "user@example.com"
    assert entry.options[OPT_DEVICE_POLL_DELAY] == 7

    hass_second = _StubHass(entry)
    second_result = asyncio.run(integration.async_migrate_entry(hass_second, entry))
    assert second_result is True
    assert hass_second.config_entries.updated == []

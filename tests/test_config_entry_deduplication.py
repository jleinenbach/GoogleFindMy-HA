# tests/test_config_entry_deduplication.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)


@dataclass(slots=True)
class _StubConfigEntry:
    entry_id: str
    data: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    disabled_by: object | None = None
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED
    title: str = ""
    domain: str = DOMAIN
    created_at: datetime | None = None
    updated_at: datetime | None = None


class _StubConfigEntriesManager:
    """Minimal config entry manager for credential-deduplication tests."""

    def __init__(self, entries: list[_StubConfigEntry]) -> None:
        self._entries: dict[str, _StubConfigEntry] = {
            entry.entry_id: entry for entry in entries
        }
        self.removed: list[str] = []

    def async_entries(self, domain: str | None = None) -> list[_StubConfigEntry]:
        values = list(self._entries.values())
        if domain is None:
            return list(values)
        return [entry for entry in values if entry.domain == domain]

    def async_get_entry(self, entry_id: str) -> _StubConfigEntry | None:
        return self._entries.get(entry_id)

    async def async_remove(self, entry_id: str) -> None:
        self.removed.append(entry_id)
        self._entries.pop(entry_id, None)


@dataclass(slots=True)
class _StubHass:
    config_entries: _StubConfigEntriesManager


def _entry_with_email(entry_id: str, email: str, **kwargs: Any) -> _StubConfigEntry:
    data = kwargs.pop("data", {})
    combined = {CONF_GOOGLE_EMAIL: email, **data}
    return _StubConfigEntry(entry_id=entry_id, data=combined, **kwargs)


@pytest.mark.asyncio
async def test_coalesce_prefers_valid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    integration = import_module("custom_components.googlefindmy")

    valid = _entry_with_email("valid", "user@example.com", version=4)
    duplicate_a = _entry_with_email("duplicate-a", "user@example.com", version=2)
    duplicate_b = _entry_with_email("duplicate-b", "user@example.com", version=1)

    manager = _StubConfigEntriesManager([valid, duplicate_a, duplicate_b])
    hass = _StubHass(manager)

    health = {
        valid.entry_id: integration._EntryHealth(status="valid", reason="probe_ok"),
        duplicate_a.entry_id: integration._EntryHealth(
            status="invalid", reason="invalid_auth"
        ),
        duplicate_b.entry_id: integration._EntryHealth(
            status="invalid", reason="invalid_auth"
        ),
    }

    async def _fake_assess(
        hass_obj: Any, entry: Any, *, normalized_email: str
    ) -> Any:
        return health[entry.entry_id]

    monkeypatch.setattr(integration, "_async_assess_entry_health", _fake_assess)

    result = await integration.async_coalesce_account_entries(
        hass, canonical_entry=duplicate_a
    )

    assert result.entry_id == valid.entry_id
    assert set(manager.removed) == {duplicate_a.entry_id, duplicate_b.entry_id}
    remaining_ids = [entry.entry_id for entry in manager.async_entries(DOMAIN)]
    assert remaining_ids == [valid.entry_id]

    manager.removed.clear()
    second = await integration.async_coalesce_account_entries(
        hass, canonical_entry=result
    )
    assert second.entry_id == valid.entry_id
    assert manager.removed == []


@pytest.mark.asyncio
async def test_coalesce_tie_breaks_by_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    integration = import_module("custom_components.googlefindmy")

    richer = _entry_with_email(
        "richer",
        "dup@example.com",
        version=5,
        data={DATA_SECRET_BUNDLE: {"google_email": "dup@example.com"}},
    )
    lean = _entry_with_email("lean", "dup@example.com", version=5)
    disabled = _entry_with_email("disabled", "dup@example.com", version=5)
    disabled.disabled_by = "user"

    manager = _StubConfigEntriesManager([richer, lean, disabled])
    hass = _StubHass(manager)

    health = integration._EntryHealth(status="valid", reason="probe_ok")

    async def _fake_assess(
        hass_obj: Any, entry: Any, *, normalized_email: str
    ) -> Any:
        return health

    monkeypatch.setattr(integration, "_async_assess_entry_health", _fake_assess)

    result = await integration.async_coalesce_account_entries(
        hass, canonical_entry=lean
    )

    assert result.entry_id == richer.entry_id
    removed = set(manager.removed)
    assert lean.entry_id in removed
    assert disabled.entry_id in removed


@pytest.mark.asyncio
async def test_coalesce_handles_unknown_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    integration = import_module("custom_components.googlefindmy")

    first = _entry_with_email(
        "first",
        "offline@example.com",
        version=3,
        created_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
    )
    second = _entry_with_email(
        "second",
        "offline@example.com",
        version=3,
        created_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
    )

    manager = _StubConfigEntriesManager([first, second])
    hass = _StubHass(manager)

    unknown = integration._EntryHealth(status="unknown", reason="timeout")

    async def _fake_assess(
        hass_obj: Any, entry: Any, *, normalized_email: str
    ) -> Any:
        return unknown

    monkeypatch.setattr(integration, "_async_assess_entry_health", _fake_assess)

    caplog.set_level("WARNING")
    result = await integration.async_coalesce_account_entries(
        hass, canonical_entry=second
    )

    assert result.entry_id == first.entry_id
    assert first.entry_id not in manager.removed
    assert second.entry_id in manager.removed
    assert any("no verified credentials" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_async_migrate_entry_uses_coalesced_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration = import_module("custom_components.googlefindmy")

    primary = _entry_with_email("primary", "main@example.com")
    duplicate = _entry_with_email("duplicate", "main@example.com")
    manager = _StubConfigEntriesManager([primary, duplicate])
    hass = _StubHass(manager)

    coalesce_calls: list[str] = []

    async def _fake_coalesce(
        hass_obj: Any, *, canonical_entry: _StubConfigEntry
    ) -> _StubConfigEntry:
        coalesce_calls.append(canonical_entry.entry_id)
        return primary

    monkeypatch.setattr(
        integration, "async_coalesce_account_entries", _fake_coalesce
    )

    post_calls: list[str] = []

    async def _fake_post(
        hass_obj: Any, entry: Any, *, duplicate_issue_cause: str
    ) -> tuple[bool, str | None]:
        post_calls.append(entry.entry_id)
        return True, "main@example.com"

    monkeypatch.setattr(
        integration, "_ensure_post_migration_consistency", _fake_post
    )

    soft_calls: list[str] = []

    async def _fake_soft(hass_obj: Any, entry: Any) -> None:
        soft_calls.append(entry.entry_id)

    monkeypatch.setattr(
        integration, "_async_soft_migrate_data_to_options", _fake_soft
    )

    result = await integration.async_migrate_entry(hass, duplicate)

    assert result is True
    assert coalesce_calls == [duplicate.entry_id]
    assert post_calls == []
    assert soft_calls == []


@pytest.mark.asyncio
async def test_rebuild_service_reloads_primary_entry_without_dedup() -> None:
    """The rebuild service now simply reloads config entries without deduplication."""

    services_module = import_module("custom_components.googlefindmy.services")

    @dataclass(slots=True)
    class _ServiceEntry:
        entry_id: str
        data: dict[str, Any]
        options: dict[str, Any] = field(default_factory=dict)
        domain: str = DOMAIN
        state: ConfigEntryState = ConfigEntryState.NOT_LOADED
        title: str = ""

    primary = _ServiceEntry("primary", {CONF_GOOGLE_EMAIL: "service@example.com"})
    duplicate = _ServiceEntry("duplicate", {CONF_GOOGLE_EMAIL: "service@example.com"})

    from tests.helpers import FakeConfigEntriesManager, FakeHass

    manager = FakeConfigEntriesManager([primary, duplicate])
    hass = FakeHass(manager)

    await services_module.async_register_services(hass, {})
    handler = hass.services.handlers[(DOMAIN, services_module.SERVICE_REBUILD_REGISTRY)]

    await handler(ServiceCall({}))

    assert manager.reload_calls == [primary.entry_id]
    assert [entry.entry_id for entry in manager.async_entries(DOMAIN)] == [
        primary.entry_id,
        duplicate.entry_id,
    ]

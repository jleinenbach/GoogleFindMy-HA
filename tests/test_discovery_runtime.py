# tests/test_discovery_runtime.py
"""Tests for the discovery runtime helpers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy import discovery
from custom_components.googlefindmy.const import DOMAIN


class _FakeHass:
    """Minimal Home Assistant stub for discovery tests."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = SimpleNamespace(async_entries=lambda domain: [])
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )
        self.bus = SimpleNamespace(async_listen_once=lambda event, cb: (lambda: None))

    async def async_add_executor_job(self, func, *args) -> Any:
        return func(*args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


@pytest.fixture(name="temp_secrets_path")
def fixture_temp_secrets_path(tmp_path: Path) -> Path:
    """Return a temporary path for Auth/secrets.json."""

    secrets_path = tmp_path / "secrets.json"
    return secrets_path


def _write_secrets(path: Path, email: str, token: str | None = None) -> None:
    payload: dict[str, Any] = {"google_email": email}
    if token is not None:
        payload["oauth_token"] = token
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_secrets_watcher_triggers_new_discovery(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """Writing a new secrets.json bundle should trigger discovery."""

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []

    async def _fake_trigger(hass_obj, **kwargs):
        triggered.append(kwargs)
        return True

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(
        discovery, "async_track_time_interval", lambda *_: (lambda: None)
    )
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)

    async def _fake_translations(hass_obj, language, category, integrations):
        return {
            f"component.{DOMAIN}.discovery.secrets_json.title": "Discovered {email}",
            f"component.{DOMAIN}.discovery.secrets_json_update.title": "Updated {email}",
        }

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    _write_secrets(temp_secrets_path, "user@example.com", token="aas_et/NEW")
    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )

    asyncio.run(watcher.async_start())
    assert len(triggered) == 1
    first = triggered[0]
    assert first["source"] == config_flow.SOURCE_DISCOVERY
    assert first["discovery_ns"] == "test.ns"
    assert first["email"] == "user@example.com"
    asyncio.run(watcher.async_stop())


def test_secrets_watcher_updates_existing_entry(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """Modified secrets should emit discovery updates for existing entries."""

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []

    async def _fake_trigger(hass_obj, **kwargs):
        triggered.append(kwargs)
        return True

    def _fake_find_entry(_hass, email: str):
        return SimpleNamespace(data={config_flow.CONF_GOOGLE_EMAIL: email})

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(
        discovery, "async_track_time_interval", lambda *_: (lambda: None)
    )
    monkeypatch.setattr(
        discovery.cf, "_find_entry_by_email", lambda *_hass, __email: None
    )

    async def _fake_translations(hass_obj, language, category, integrations):
        return {
            f"component.{DOMAIN}.discovery.secrets_json.title": "Discovered {email}",
            f"component.{DOMAIN}.discovery.secrets_json_update.title": "Updated {email}",
        }

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    _write_secrets(temp_secrets_path, "owner@example.com", token="aas_et/OLD")
    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    asyncio.run(watcher.async_start())
    triggered.clear()

    _write_secrets(temp_secrets_path, "owner@example.com", token="aas_et/FRESH")
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", _fake_find_entry)
    asyncio.run(watcher.async_force_scan())

    assert len(triggered) == 1
    update = triggered[0]
    assert update["source"] == config_flow.SOURCE_DISCOVERY_UPDATE_INFO
    assert update["discovery_ns"] == "test.ns"
    assert update["email"] == "owner@example.com"
    assert update.get("title") == "Updated owner@example.com"
    asyncio.run(watcher.async_stop())

# tests/test_discovery_runtime.py
"""Tests for the discovery runtime helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.ha_typing import CloudDiscoveryRuntime
from tests.helpers import config_entry_with_cloud_runtime


class _FakeHass:
    """Minimal Home Assistant stub for discovery tests."""

    def __init__(self, entry: Any | None = None) -> None:
        self.data: dict[str, Any] = {}
        runtime_owner = entry or config_entry_with_cloud_runtime()
        self._entry = runtime_owner
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: [runtime_owner] if domain == DOMAIN else []
        )
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
            f"component.{DOMAIN}.config.progress.discovery_secrets_new": "Discovered {email}",
            f"component.{DOMAIN}.config.progress.discovery_secrets_update": "Updated {email}",
        }

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    async def _exercise() -> None:
        watcher = discovery.SecretsJSONWatcher(
            hass, path=temp_secrets_path, namespace="test.ns"
        )

        await watcher.async_start()
        await asyncio.sleep(0)

        assert len(triggered) == 1
        first = triggered[0]
        assert first["source"] == config_flow.SOURCE_DISCOVERY
        assert first["discovery_ns"] == "test.ns"
        assert first["email"] == "user@example.com"

        await watcher.async_stop()

    _write_secrets(temp_secrets_path, "user@example.com", token="aas_et/NEW")
    asyncio.run(_exercise())


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
        return config_entry_with_cloud_runtime(
            entry_id="entry-id",
            data={config_flow.CONF_GOOGLE_EMAIL: email},
        )

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(
        discovery, "async_track_time_interval", lambda *_: (lambda: None)
    )
    monkeypatch.setattr(
        discovery.cf, "_find_entry_by_email", lambda *_hass, __email: None
    )

    async def _fake_translations(hass_obj, language, category, integrations):
        return {
            f"component.{DOMAIN}.config.progress.discovery_secrets_new": "Discovered {email}",
            f"component.{DOMAIN}.config.progress.discovery_secrets_update": "Updated {email}",
        }

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    async def _exercise() -> None:
        watcher = discovery.SecretsJSONWatcher(
            hass, path=temp_secrets_path, namespace="test.ns"
        )
        await watcher.async_start()
        await asyncio.sleep(0)

        triggered.clear()

        _write_secrets(temp_secrets_path, "owner@example.com", token="aas_et/FRESH")
        monkeypatch.setattr(discovery.cf, "_find_entry_by_email", _fake_find_entry)
        await watcher.async_force_scan()
        await asyncio.sleep(0)

        assert len(triggered) == 1
        update = triggered[0]
        assert update["source"] == config_flow.DISCOVERY_UPDATE_SOURCE
        assert update["discovery_ns"] == "test.ns"
        assert update["email"] == "owner@example.com"
        assert update.get("title") == "Updated owner@example.com"

        await watcher.async_stop()

    _write_secrets(temp_secrets_path, "owner@example.com", token="aas_et/OLD")
    asyncio.run(_exercise())


def test_cloud_discovery_results_suppress_task_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Queued discovery tasks should not leak unhandled exceptions."""

    hass = _FakeHass()
    caplog.set_level(logging.DEBUG)

    async def _boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("discovery explosion")

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _boom)

    async def _exercise() -> None:
        results = discovery._CloudDiscoveryResults(hass)
        results.append({"email": "boom@example.com"})
        await asyncio.sleep(0)

    asyncio.run(_exercise())

    assert any(
        "Suppressed cloud discovery task exception" in record.getMessage()
        for record in caplog.records
    )


def test_cloud_discovery_runtime_rebinds_on_reload() -> None:
    """Reloading should rebind the runtime results to the new hass."""

    entry = SimpleNamespace(
        entry_id="entry-id",
        runtime_data=SimpleNamespace(cloud_discovery=CloudDiscoveryRuntime()),
    )
    hass = _FakeHass(entry)
    runtime = discovery._cloud_discovery_runtime(hass, entry)

    runtime.results.append({"email": "persist@example.com"}, trigger=False)

    new_hass = _FakeHass(entry)
    rebound = discovery._cloud_discovery_runtime(new_hass, entry)

    assert rebound is runtime
    assert rebound.results._hass is new_hass
    assert len(rebound.results) == 1


def test_cleanup_cloud_discovery_runtime_cancels_handles() -> None:
    """Cleanup should clear runtime handles and unsubscribe listeners."""

    runtime_container = CloudDiscoveryRuntime()
    runtime_container.active_keys.update({"a", "b"})
    runtime_container.results = discovery._CloudDiscoveryResults(_FakeHass())

    unsub_called: list[str] = []
    runtime_container.dispatcher_unsubscribers.append(
        lambda: unsub_called.append("unsub")
    )

    cancelled: list[str] = []

    class _Handle:
        def cancel(self) -> None:  # type: ignore[no-untyped-def]
            cancelled.append("cancel")

    runtime_container.retry_handles.add(_Handle())

    runtime_data = SimpleNamespace(cloud_discovery=runtime_container)

    integration._cleanup_cloud_discovery_runtime(runtime_data)

    assert unsub_called == ["unsub"]
    assert cancelled == ["cancel"]
    assert not runtime_container.active_keys
    assert not runtime_container.retry_handles
    assert runtime_container.results is None

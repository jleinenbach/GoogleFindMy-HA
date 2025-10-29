# tests/test_token_cache_secrets.py
"""Regression tests ensuring secrets bundles retain FCM identifiers."""

from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from types import ModuleType
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import token_cache
from custom_components.googlefindmy.Auth.token_cache import TokenCache


class _CapturingCache:
    """Minimal cache stub recording values written by the secrets migrator."""

    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        self.saved[name] = value


class _StubHass:
    """Async Home Assistant stub mirroring executor offloading."""

    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def _install_recording_store(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the Store stub with a recorder capturing writes and freshness."""

    storage_module = sys.modules["homeassistant.helpers.storage"]
    instances: list[Any] = []

    class _RecordingStore:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._data: dict[str, Any] | None = None
            self.saved_snapshots: list[dict[str, Any]] = []
            self.fresh = False
            instances.append(self)

        async def async_load(self) -> dict[str, Any] | None:
            return self._data

        def async_delay_save(self, writer: Any, _delay: float) -> None:
            snapshot = writer()
            self.saved_snapshots.append(snapshot)
            self._data = snapshot
            self.fresh = True

        async def async_save(self, data: dict[str, Any]) -> None:
            self._data = data

    monkeypatch.setattr(storage_module, "Store", _RecordingStore)
    monkeypatch.setattr(token_cache, "Store", _RecordingStore)
    return instances


def test_async_save_secrets_data_preserves_gcm_identifiers() -> None:
    """android_id and security_token remain available after secrets migration."""

    if "homeassistant.loader" not in sys.modules:
        loader_module = ModuleType("homeassistant.loader")
        loader_module.async_get_integration = lambda *_args, **_kwargs: None
        sys.modules["homeassistant.loader"] = loader_module

    integration_init = import_module("custom_components.googlefindmy")

    cache = _CapturingCache()
    secrets_bundle = {
        "fcm_credentials": {
            "gcm": {
                "android_id": "1234567890",
                "security_token": "9876543210",
            },
            "fcm": {
                "registration": {"token": "cached-token"},
            },
        },
        "username": "user@example.com",
    }

    asyncio.run(integration_init._async_save_secrets_data(cache, secrets_bundle))

    assert "fcm_credentials" in cache.saved
    stored = cache.saved["fcm_credentials"]
    assert isinstance(stored, dict)
    gcm_block = stored["gcm"]
    assert gcm_block["android_id"] == "1234567890"
    assert gcm_block["security_token"] == "9876543210"


@pytest.mark.asyncio
async def test_token_cache_create_migrates_legacy_bundle(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy secrets JSON migrates into the Store via TokenCache.create()."""

    monkeypatch.setattr(token_cache, "_LEGACY_MIGRATION_DONE", False, raising=False)
    stores = _install_recording_store(monkeypatch)

    hass = _StubHass()
    legacy_path = tmp_path / "legacy_secrets.json"
    legacy_payload = {
        "oauth_token": "legacy-token",
        "username": "legacy@example.com",
        "fcm_credentials": {
            "gcm": {"android_id": "111", "security_token": "222"},
            "fcm": {"registration": {"token": "legacy-registration"}},
        },
    }
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    cache = await TokenCache.create(hass, "entry-legacy", str(legacy_path))

    assert not legacy_path.exists()
    assert stores, "Expected the Store stub to be instantiated"
    legacy_store = stores[-1]
    assert legacy_store.saved_snapshots, "Legacy migration should persist merged data"
    stored_snapshot = legacy_store.saved_snapshots[-1]
    assert stored_snapshot["username"] == "legacy@example.com"
    gcm_credentials = stored_snapshot["fcm_credentials"]["gcm"]
    assert gcm_credentials["android_id"] == "111"
    assert gcm_credentials["security_token"] == "222"
    assert legacy_store.fresh is True

    assert await cache.get("oauth_token") == "legacy-token"
    assert await cache.get("username") == "legacy@example.com"

    await cache.close()


@pytest.mark.asyncio
async def test_token_cache_migration_runs_only_once(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_LEGACY_MIGRATION_DONE avoids deleting later legacy cache files."""

    monkeypatch.setattr(token_cache, "_LEGACY_MIGRATION_DONE", False, raising=False)
    stores = _install_recording_store(monkeypatch)

    hass = _StubHass()
    legacy_first = tmp_path / "legacy_first.json"
    legacy_first.write_text(json.dumps({"username": "first"}), encoding="utf-8")

    first_cache = await TokenCache.create(hass, "entry-first", str(legacy_first))
    assert stores, "Expected Store instances after first migration"
    first_store = stores[0]
    assert not legacy_first.exists()
    assert first_store.saved_snapshots
    assert first_store.fresh is True

    legacy_second = tmp_path / "legacy_second.json"
    legacy_second.write_text(json.dumps({"username": "second"}), encoding="utf-8")

    second_cache = await TokenCache.create(hass, "entry-second", str(legacy_second))
    assert legacy_second.exists()
    assert len(stores) == 2
    second_store = stores[1]
    assert second_store.saved_snapshots == []

    await first_cache.close()
    await second_cache.close()

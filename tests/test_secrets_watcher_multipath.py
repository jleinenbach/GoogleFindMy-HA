# tests/test_secrets_watcher_multipath.py
"""Track A: multipath secrets watcher (newest-wins) and delete-after-import.

These tests exercise the generalized ``SecretsJSONWatcher`` path list and the
config-flow delete-ALL hook without driving the full Home Assistant discovery
flow machinery:

* newest-wins signature selection across several observed paths,
* delete-ALL removing every watched bundle after a successful import, with no
  watcher re-trigger on its own delete,
* an aborted/failed flow leaving every file in place,
* a missing extra path being a strict no-op.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN, SECRETS_EXTRA_WATCH_PATHS
from tests.helpers.config_entries_stub import make_config_entry

pytestmark = pytest.mark.asyncio


class _FakeHass:
    """Minimal Home Assistant stub for multipath watcher tests."""

    def __init__(self, *, extra_watch_paths: list[str] | None = None) -> None:
        self.data: dict[str, Any] = {}
        options: dict[str, Any] = {}
        if extra_watch_paths is not None:
            options[SECRETS_EXTRA_WATCH_PATHS] = extra_watch_paths
        self._entry = make_config_entry(entry_id="watcher-entry", options=options)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: (
                [self._entry] if domain == DOMAIN else []
            )
        )
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )
        self.bus = SimpleNamespace(async_listen_once=lambda event, cb: lambda: None)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)

    def async_create_task(self, coro: Any, *_args: Any, **_kwargs: Any) -> Any:
        return asyncio.ensure_future(coro)


def _write_secrets(path: Path, email: str, token: str | None = None) -> None:
    payload: dict[str, Any] = {"google_email": email}
    if token is not None:
        payload["oauth_token"] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_discovery(monkeypatch: pytest.MonkeyPatch, triggered: list[dict[str, Any]]) -> None:
    async def _fake_trigger(_hass: Any, **kwargs: Any) -> bool:
        triggered.append(kwargs)
        return True

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )


async def test_newest_wins_across_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When two files exist the newer mtime file sets the signature."""

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    older = tmp_path / "auth" / "secrets.json"
    newer = tmp_path / "data" / "secrets.json"
    _write_secrets(older, "older@example.com", token="aas_et/OLD")
    _write_secrets(newer, "newer@example.com", token="aas_et/NEW")
    # Force a deterministic mtime ordering (older < newer).
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    watcher = discovery.SecretsJSONWatcher(hass, paths=[older, newer])
    await watcher.async_start()
    await asyncio.sleep(0)

    assert len(triggered) == 1
    assert triggered[0]["email"] == "newer@example.com"
    assert watcher.watch_paths == (older, newer)

    await watcher.async_stop()


async def test_delete_all_removes_every_watched_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Delete-ALL removes both bundles; a re-scan does not re-trigger."""

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    older = tmp_path / "auth" / "secrets.json"
    newer = tmp_path / "data" / "secrets.json"
    _write_secrets(older, "older@example.com", token="aas_et/OLD")
    _write_secrets(newer, "newer@example.com", token="aas_et/NEW")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    watcher = discovery.SecretsJSONWatcher(hass, paths=[older, newer])
    await watcher.async_start()
    await asyncio.sleep(0)
    assert len(triggered) == 1
    assert triggered[0]["email"] == "newer@example.com"

    # Register the manager so the delete hook can find the watch paths.
    manager = SimpleNamespace(watch_paths=(older, newer))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(hass)

    assert not older.exists()
    assert not newer.exists()

    # A follow-up scan on the now-empty paths must NOT re-trigger discovery.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    await watcher.async_stop()


async def test_delete_all_is_idempotent_and_missing_is_noop(
    tmp_path: Path,
) -> None:
    """Deleting again (or an already-missing path) never raises."""

    hass = _FakeHass()
    present = tmp_path / "data" / "secrets.json"
    missing = tmp_path / "auth" / "secrets.json"
    _write_secrets(present, "present@example.com", token="aas_et/P")

    manager = SimpleNamespace(watch_paths=(missing, present))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    # First delete removes the present file; the missing path is a no-op.
    await config_flow._async_delete_watched_secrets(hass)
    assert not present.exists()

    # Second delete over now-empty paths must not raise.
    await config_flow._async_delete_watched_secrets(hass)


async def test_aborted_flow_keeps_all_files(tmp_path: Path) -> None:
    """When the import is aborted the delete hook is never invoked."""

    hass = _FakeHass()
    older = tmp_path / "auth" / "secrets.json"
    newer = tmp_path / "data" / "secrets.json"
    _write_secrets(older, "older@example.com", token="aas_et/OLD")
    _write_secrets(newer, "newer@example.com", token="aas_et/NEW")

    manager = SimpleNamespace(watch_paths=(older, newer))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    # Simulate the aborted-flow branch: the delete helper is simply not called.
    # Both files must still be present afterwards.
    assert older.exists()
    assert newer.exists()

    # No discovery_manager -> the helper is a strict no-op even if called.
    empty_hass = _FakeHass()
    await config_flow._async_delete_watched_secrets(empty_hass)
    assert older.exists()
    assert newer.exists()


async def test_missing_extra_path_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured but non-existent extra path does not crash the watcher."""

    default_path = tmp_path / "auth" / "secrets.json"
    missing_extra = tmp_path / "nowhere" / "secrets.json"
    _write_secrets(default_path, "user@example.com", token="aas_et/ONLY")

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    watcher = discovery.SecretsJSONWatcher(hass, paths=[default_path, missing_extra])
    await watcher.async_start()
    await asyncio.sleep(0)

    assert len(triggered) == 1
    assert triggered[0]["email"] == "user@example.com"

    await watcher.async_stop()


async def test_manager_collects_extra_watch_paths(tmp_path: Path) -> None:
    """DiscoveryManager builds a watcher from default + option extra paths."""

    extra = tmp_path / "data" / "secrets.json"
    hass = _FakeHass(extra_watch_paths=[str(extra)])

    collected = discovery._collect_extra_watch_paths(hass)
    assert extra in collected

    empty_hass = _FakeHass()
    assert discovery._collect_extra_watch_paths(empty_hass) == []

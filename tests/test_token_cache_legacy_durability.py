# tests/test_token_cache_legacy_durability.py
"""Durability contract for the legacy ``Auth/secrets.json`` migration.

``TokenCache._migrate_legacy_file`` deletes the legacy credential file. The
deletion is irreversible and the file is the only remaining copy of the
credentials, so it may only happen once the merged data is provably on disk.

Two return paths make "no exception" a useless proof here, both read in
``homeassistant/helpers/storage.py`` (2026.1.3):

* ``async_delay_save`` is a ``@callback`` returning ``None``; the write happens
  up to ``delay`` seconds later and cannot be awaited (``:456-495``).
* ``_async_handle_write_data`` catches ``SerializationError`` and ``WriteError``
  and only logs them (``:563-566``), so a full disk returns cleanly from
  ``async_save``.

Each test below isolates exactly one of the ways the store can fail to persist
and asserts the same thing: the legacy file survives. The error direction is
"orphaned legacy file", never "lost credentials".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import token_cache
from custom_components.googlefindmy.Auth.token_cache import TokenCache

_LEGACY_PAYLOAD: dict[str, Any] = {
    "oauth_token": "legacy-oauth-token",
    "username": "legacy@example.com",
}


class _DiskStore:
    """Store double that persists a real HA envelope to a real path.

    Mirrors ``Store`` closely enough for the durability question: ``path`` is a
    real file, ``async_save`` writes the ``{"version", "minor_version", "key",
    "data"}`` envelope, and ``async_delay_save`` records the call *without*
    writing, which is what the debounced timer looks like before it fires.
    """

    version = 1
    minor_version = 1

    def __init__(self, path: Path, *, persists: bool = True) -> None:
        self.path = str(path)
        self.key = "googlefindmy_cache"
        self.persists = persists
        self.loaded: dict[str, Any] | None = None
        self.delay_save_calls = 0
        self.save_calls = 0

    async def async_load(self) -> dict[str, Any] | None:
        return self.loaded

    def async_delay_save(self, writer: Callable[[], Any], delay: float = 0.0) -> None:
        """Record a debounced save. Nothing reaches disk until the timer fires."""

        self.delay_save_calls += 1

    async def async_save(self, data: dict[str, Any]) -> None:
        """Write the envelope, or return cleanly without writing anything.

        ``persists=False`` reproduces the swallowed ``WriteError``: the caller
        sees a successful coroutine and an empty disk.
        """

        self.save_calls += 1
        if not self.persists:
            return
        envelope = {
            "version": self.version,
            "minor_version": self.minor_version,
            "key": self.key,
            "data": data,
        }
        Path(self.path).write_text(json.dumps(envelope), encoding="utf-8")

    def stored_keys(self) -> set[str]:
        """Return the keys actually present in the file on disk."""

        store_file = Path(self.path)
        if not store_file.exists():
            return set()
        payload = json.loads(store_file.read_text(encoding="utf-8"))
        return set(payload["data"])


class _StubHass:
    """Home Assistant stub that runs executor jobs inline."""

    async def async_add_executor_job(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def _reset_legacy_migration_flag() -> Iterator[None]:
    """Clear the process-wide migration sentinel around every test."""

    token_cache._set_legacy_migration_flag(False)
    yield
    token_cache._set_legacy_migration_flag(False)


def _write_legacy(tmp_path: Path) -> Path:
    legacy_path = tmp_path / "secrets.json"
    legacy_path.write_text(json.dumps(_LEGACY_PAYLOAD), encoding="utf-8")
    return legacy_path


def _install_store(monkeypatch: pytest.MonkeyPatch, store: _DiskStore) -> None:
    monkeypatch.setattr(token_cache, "Store", lambda *_a, **_kw: store)


@pytest.mark.asyncio
async def test_invalid_snapshot_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the snapshot is not serializable, so no save is issued.

    The unrelated key is deliberate. ``_is_valid_snapshot`` judges the whole
    cache, not the keys being migrated, so a single foreign object suppresses
    the migration write while the legacy file would still be deleted.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json")
    store.loaded = {"unrelated_key": object()}
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-invalid-snapshot", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted although nothing was persisted"
    assert store.stored_keys() == set()


@pytest.mark.asyncio
async def test_store_write_failure_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: ``async_save`` returns cleanly but writes nothing.

    This is the only test that separates "await the save" from "verify the
    result": a fix that merely awaits ``async_save`` and trusts the missing
    exception still deletes the file here.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json", persists=False)
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-write-failure", str(legacy_path))

    assert store.save_calls == 1, "Migration must attempt an immediate, awaited save"
    assert legacy_path.exists(), "Legacy file deleted although the write was lost"
    assert store.stored_keys() == set()


@pytest.mark.asyncio
async def test_successful_migration_removes_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the merged data is on disk, so the deletion may run.

    Guards the other direction. A verification that can never succeed would
    keep every legacy file forever and pass all three tests above.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json")
    _install_store(monkeypatch, store)

    cache = await TokenCache.create(_StubHass(), "entry-happy", str(legacy_path))

    assert store.stored_keys() >= set(_LEGACY_PAYLOAD)
    assert not legacy_path.exists(), "Legacy file kept although the data is on disk"
    assert await cache.get("oauth_token") == _LEGACY_PAYLOAD["oauth_token"]


@pytest.mark.asyncio
async def test_delayed_save_is_not_used_for_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the migration never routes through the debounce.

    ``async_delay_save`` cannot be awaited, so any use of it in this path makes
    the subsequent verification a race against a timer.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json")
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-no-debounce", str(legacy_path))

    assert store.delay_save_calls == 0, "Migration must not use the debounced save"
    assert store.save_calls == 1

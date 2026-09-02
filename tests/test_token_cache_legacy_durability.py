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


def _install_store(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    monkeypatch.setattr(token_cache, "Store", lambda *_a, **_kw: store)


def _install_store_sequence(monkeypatch: pytest.MonkeyPatch, stores: list[Any]) -> None:
    """Hand out one store per TokenCache, in order, for multi-entry setups."""

    remaining = list(stores)

    def _next_store(*_a: Any, **_kw: Any) -> Any:
        return remaining.pop(0)

    monkeypatch.setattr(token_cache, "Store", _next_store)


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


@pytest.mark.asyncio
async def test_store_without_path_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the store exposes no path, so nothing can be read back."""

    class _PathlessStore:
        def __init__(self) -> None:
            self.saved: list[dict[str, Any]] = []

        async def async_load(self) -> dict[str, Any] | None:
            return None

        def async_delay_save(
            self, writer: Callable[[], Any], delay: float = 0.0
        ) -> None:
            raise AssertionError("Migration must not use the debounced save")

        async def async_save(self, data: dict[str, Any]) -> None:
            self.saved.append(data)

    legacy_path = _write_legacy(tmp_path)
    store = _PathlessStore()
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-pathless", str(legacy_path))

    assert store.saved, "The snapshot should still be handed to the store"
    assert legacy_path.exists(), (
        "Legacy file deleted although nothing could be verified"
    )


@pytest.mark.asyncio
async def test_unreadable_store_envelope_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the file on disk is not a store envelope."""

    legacy_path = _write_legacy(tmp_path)
    store_file = tmp_path / "store.json"
    store = _DiskStore(store_file, persists=False)
    store_file.write_text("not json at all", encoding="utf-8")
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-broken-envelope", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted on an unreadable store file"


@pytest.mark.asyncio
async def test_already_persisted_keys_remove_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the store already holds the legacy keys, so no write is due.

    Covers the deletion that happens without any write attempt: the merge changes
    nothing, and the proof still has to come from the file.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json")
    store.loaded = dict(_LEGACY_PAYLOAD)
    await store.async_save(dict(_LEGACY_PAYLOAD))
    store.save_calls = 0
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-already-there", str(legacy_path))

    assert store.save_calls == 0, "Nothing changed, so nothing should be written"
    assert not legacy_path.exists()


@pytest.mark.asyncio
async def test_failed_verification_does_not_migrate_into_a_second_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed migration must not hand the file to the next config entry.

    ``legacy_path`` is one shared module path for every entry, so the process-wide
    sentinel is also the guard against a second entry adopting the first entry's
    credentials. Clearing it on failure would delete the file after all, one
    instance later, and put another account's token into the second store.
    """

    legacy_path = _write_legacy(tmp_path)
    failing_store = _DiskStore(tmp_path / "store_a.json", persists=False)
    second_store = _DiskStore(tmp_path / "store_b.json")
    _install_store_sequence(monkeypatch, [failing_store, second_store])

    await TokenCache.create(_StubHass(), "entry-a", str(legacy_path))
    assert legacy_path.exists()

    await TokenCache.create(_StubHass(), "entry-b", str(legacy_path))

    assert legacy_path.exists(), "Second entry deleted the first entry's legacy file"
    assert second_store.save_calls == 0
    assert second_store.stored_keys() == set()


def _write_envelope(
    path: Path,
    data: dict[str, Any],
    *,
    key: str = "googlefindmy_cache",
    version: int = 1,
) -> None:
    """Write a store envelope directly, bypassing the double's own save path."""

    path.write_text(
        json.dumps({"version": version, "minor_version": 1, "key": key, "data": data}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_stale_values_under_the_same_keys_keep_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the file carries the right key names but older values.

    Key names alone are not a proof. A store file left over from an earlier run
    can name every migrated key while the current values never reached the disk.
    """

    legacy_path = _write_legacy(tmp_path)
    store_file = tmp_path / "store.json"
    _write_envelope(
        store_file, {"oauth_token": "stale-token", "username": "stale@example.com"}
    )
    store = _DiskStore(store_file, persists=False)
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-stale-values", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted on stale values"


@pytest.mark.asyncio
async def test_foreign_store_envelope_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the envelope on disk belongs to a different store key."""

    legacy_path = _write_legacy(tmp_path)
    store_file = tmp_path / "store.json"
    _write_envelope(store_file, dict(_LEGACY_PAYLOAD), key="some_other_store")
    store = _DiskStore(store_file, persists=False)
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-foreign-key", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted on a foreign envelope"


@pytest.mark.asyncio
async def test_unloadable_storage_version_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: the envelope carries a version this build cannot load.

    Data that Home Assistant refuses on the next start is not persisted data.
    """

    legacy_path = _write_legacy(tmp_path)
    store_file = tmp_path / "store.json"
    _write_envelope(store_file, dict(_LEGACY_PAYLOAD), version=99)
    store = _DiskStore(store_file, persists=False)
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-bad-version", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted on an unloadable envelope"


@pytest.mark.asyncio
async def test_unserializable_migrated_value_keeps_legacy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: a migrated key holds a value that cannot be serialized.

    The promise of the verification is that an open question keeps the file, so
    building the comparison must not be able to escape as an exception either.
    `TokenCache.create` is called unguarded during setup.
    """

    legacy_path = _write_legacy(tmp_path)
    store = _DiskStore(tmp_path / "store.json")
    store.loaded = {"oauth_token": object()}
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-unserializable", str(legacy_path))

    assert legacy_path.exists(), "Legacy file deleted although nothing was persisted"


@pytest.mark.asyncio
async def test_migration_verifies_against_the_home_assistant_serializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof must hold for what Home Assistant really writes, not for json.dumps.

    Home Assistant serializes store payloads with orjson, which turns a tuple into
    a list and encodes non-ASCII directly. A verification comparing in-memory
    objects would fail a migration that in fact persisted correctly.
    """

    from homeassistant.helpers.json import json_bytes

    class _OrjsonStore(_DiskStore):
        async def async_save(self, data: dict[str, Any]) -> None:
            self.save_calls += 1
            envelope = {
                "version": self.version,
                "minor_version": self.minor_version,
                "key": self.key,
                "data": data,
            }
            Path(self.path).write_bytes(json_bytes(envelope))

    payload = {
        "oauth_token": "legacy-oauth-token",
        "username": "légacy@example.com",
        "fcm_credentials": {
            "gcm": {"android_id": "111", "security_token": "222"},
            "fcm": {"registration": {"token": "ünïcode-täken"}},
        },
        "counters": [1, 2, 3],
    }
    legacy_path = tmp_path / "secrets.json"
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")

    store = _OrjsonStore(tmp_path / "store.json")
    _install_store(monkeypatch, store)

    cache = await TokenCache.create(_StubHass(), "entry-orjson", str(legacy_path))

    assert not legacy_path.exists(), "Legacy file kept although the data is on disk"
    assert store.stored_keys() >= set(payload)
    assert await cache.get("username") == "légacy@example.com"


@pytest.mark.asyncio
async def test_legacy_file_replaced_during_migration_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated condition: a newer bundle lands while the migration is awaiting.

    The standalone login replaces this exact file atomically and the discovery
    watcher polls it, so the window between reading and deleting is a real
    producer race. What gets deleted must be the bytes that were proved, not
    whatever happens to be at the path afterwards.
    """

    legacy_path = _write_legacy(tmp_path)

    class _RacingStore(_DiskStore):
        async def async_save(self, data: dict[str, Any]) -> None:
            # Stands in for the login container writing a fresh bundle into the
            # window the awaited save opens.
            legacy_path.write_text(
                json.dumps({"oauth_token": "brand-new-token"}), encoding="utf-8"
            )
            await super().async_save(data)

    store = _RacingStore(tmp_path / "store.json")
    _install_store(monkeypatch, store)

    await TokenCache.create(_StubHass(), "entry-racing", str(legacy_path))

    assert legacy_path.exists(), "A bundle that was never migrated got deleted"
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == {
        "oauth_token": "brand-new-token"
    }

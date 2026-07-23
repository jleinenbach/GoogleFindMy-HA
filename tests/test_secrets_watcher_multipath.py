# tests/test_secrets_watcher_multipath.py
"""Track A: multipath secrets watcher (newest-wins) and delete-after-import.

These tests exercise the generalized ``SecretsJSONWatcher`` path list and the
config-flow delete hook without driving the full Home Assistant discovery
flow machinery:

* newest-wins signature selection across several observed paths,
* account- AND content-aware delete after a successful import: every
  same-account copy goes when the imported bundle is still the newest one, with
  no watcher re-trigger on its own delete,
* the write-during-confirmation race: a *newer* same-account bundle survives the
  delete and only the copies carrying the imported content are removed,
* a foreign account and an unidentifiable file always survive,
* an aborted/failed flow leaving every file in place,
* a missing extra path being a strict no-op,
* the *timing* of the delete (P2): the confirmed discovery flow only STAGES the
  delete in ``hass.data`` and the file survives the step, because
  ``ConfigFlow.async_create_entry`` merely builds a FlowResult while Home
  Assistant stores the entry afterwards in
  ``ConfigEntriesFlowManager.async_finish_flow``. The staged job is executed
  once by the runner ``async_setup_entry`` calls, survives a failing delete,
  and is not repeated on a reload. The discovery *update* case keeps its inline
  delete (``async_update_entry`` persists synchronously).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN, SECRETS_EXTRA_WATCH_PATHS
from custom_components.googlefindmy.email_utils import unique_account_id
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)

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
            async_entries=lambda domain: [self._entry] if domain == DOMAIN else []
        )
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )
        self.bus = SimpleNamespace(async_listen_once=lambda event, cb: lambda: None)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)

    def async_create_task(self, coro: Any, *_args: Any, **_kwargs: Any) -> Any:
        return asyncio.ensure_future(coro)


class _EntriesHass(_FakeHass):
    """``_FakeHass`` variant whose config entries are supplied by the test.

    ``_FakeHass`` hard-codes exactly one entry, which is enough for the
    collector tests but not for the watch-path recomputation tests: those need
    to vary the entry set (extra path present/absent, entry disabled) between
    two refreshes on the same manager.
    """

    def __init__(self, entries: list[Any]) -> None:
        super().__init__()
        self.entries: list[Any] = list(entries)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(self.entries) if domain == DOMAIN else []
        )


def _record_watcher_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, bool | None]]:
    """Record ``async_update_paths``/``async_force_scan`` on every watcher.

    Patched on the class because :class:`SecretsJSONWatcher` defines
    ``__slots__``, so per-instance attribute injection is impossible. The real
    implementations still run, so the recorded calls describe actual behaviour
    rather than replacing it.
    """

    calls: list[tuple[str, bool | None]] = []
    real_update = discovery.SecretsJSONWatcher.async_update_paths
    real_scan = discovery.SecretsJSONWatcher.async_force_scan

    async def _update(
        watcher: discovery.SecretsJSONWatcher,
        paths: list[Path],
        *,
        forget_signature: bool = True,
    ) -> None:
        calls.append(("update", forget_signature))
        await real_update(watcher, paths, forget_signature=forget_signature)

    async def _scan(watcher: discovery.SecretsJSONWatcher) -> None:
        calls.append(("scan", None))
        await real_scan(watcher)

    monkeypatch.setattr(discovery.SecretsJSONWatcher, "async_update_paths", _update)
    monkeypatch.setattr(discovery.SecretsJSONWatcher, "async_force_scan", _scan)
    return calls


def _bundle(email: str, token: str | None = None) -> dict[str, Any]:
    """Return the bundle payload ``_write_secrets`` persists."""

    payload: dict[str, Any] = {"google_email": email}
    if token is not None:
        payload["oauth_token"] = token
    return payload


def _write_secrets(path: Path, email: str, token: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_bundle(email, token)), encoding="utf-8")


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch, triggered: list[dict[str, Any]]
) -> None:
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


def _stable_key_for(email: str, token: str | None = None) -> str:
    """Return the account stable-key the delete hook compares against."""

    return discovery._cloud_discovery_stable_key(email, token, None)


def _digest_for(email: str, token: str | None = None) -> str:
    """Return the content digest the delete hook compares against.

    Same function that stamps every watched file, so a bundle written by
    ``_write_secrets`` and the "imported payload" hash identically.
    """

    return discovery.secrets_bundle_digest(_bundle(email, token))


async def test_delete_removes_winner_and_same_account_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Core case: the imported account's copies go when the import is current.

    Two watched files carry the SAME account and the SAME content (the real
    Auth/+data/ redundancy); passing that account's stable_key plus the imported
    content digest removes BOTH, and a re-scan on the now-empty paths does not
    re-trigger discovery.
    """

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    auth_copy = tmp_path / "auth" / "secrets.json"
    data_copy = tmp_path / "data" / "secrets.json"
    _write_secrets(auth_copy, "user@example.com", token="aas_et/SAME")
    _write_secrets(data_copy, "user@example.com", token="aas_et/SAME")
    os.utime(auth_copy, (1_000, 1_000))
    os.utime(data_copy, (2_000, 2_000))

    watcher = discovery.SecretsJSONWatcher(hass, paths=[auth_copy, data_copy])
    await watcher.async_start()
    await asyncio.sleep(0)
    assert len(triggered) == 1
    assert triggered[0]["email"] == "user@example.com"

    # Register the manager so the delete hook can find the watch paths.
    manager = SimpleNamespace(watch_paths=(auth_copy, data_copy))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/SAME"),
    )

    assert not auth_copy.exists()
    assert not data_copy.exists()

    # A follow-up scan on the now-empty paths must NOT re-trigger discovery.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    await watcher.async_stop()


async def test_delete_keeps_foreign_account_file_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A co-located bundle of a DIFFERENT account is kept and warned about."""

    import logging

    hass = _FakeHass()
    winner = tmp_path / "data" / "secrets.json"
    foreign = tmp_path / "other" / "secrets.json"
    _write_secrets(winner, "winner@example.com", token="aas_et/WIN")
    _write_secrets(foreign, "foreign@example.com", token="aas_et/FOR")

    manager = SimpleNamespace(watch_paths=(winner, foreign))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.WARNING):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("winner@example.com"),
            imported_digest=_digest_for("winner@example.com", "aas_et/WIN"),
        )

    assert not winner.exists()  # imported account removed
    assert foreign.exists()  # foreign account preserved
    # A redacted warning is logged; the foreign account is not in plaintext.
    assert any("different account" in record.getMessage() for record in caplog.records)
    assert "foreign@example.com" not in caplog.text


async def test_delete_keeps_unreadable_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable/unparseable file is kept (account not determinable)."""

    import logging

    hass = _FakeHass()
    winner = tmp_path / "data" / "secrets.json"
    garbage = tmp_path / "auth" / "secrets.json"
    _write_secrets(winner, "winner@example.com", token="aas_et/WIN")
    garbage.parent.mkdir(parents=True, exist_ok=True)
    garbage.write_text("{ this is not valid json", encoding="utf-8")

    manager = SimpleNamespace(watch_paths=(winner, garbage))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.DEBUG):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("winner@example.com"),
            imported_digest=_digest_for("winner@example.com", "aas_et/WIN"),
        )

    assert not winner.exists()  # imported account removed
    assert garbage.exists()  # unparseable file preserved (conservative)


async def test_delete_without_identity_keeps_all(tmp_path: Path) -> None:
    """Fail-safe: an unknown account key OR an unknown content digest deletes nothing.

    A payload without a secrets bundle has no content identity and did not come
    from one of these files, so neither half of the identity may be guessed.
    """

    hass = _FakeHass()
    a = tmp_path / "data" / "secrets.json"
    b = tmp_path / "auth" / "secrets.json"
    _write_secrets(a, "a@example.com", token="aas_et/A")
    _write_secrets(b, "b@example.com", token="aas_et/B")

    manager = SimpleNamespace(watch_paths=(a, b))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=None,
        imported_digest=_digest_for("a@example.com", "aas_et/A"),
    )
    assert a.exists()
    assert b.exists()

    # Same account key, but no content identity: still a strict no-op.
    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("a@example.com"),
        imported_digest=None,
    )
    assert a.exists()
    assert b.exists()


async def test_delete_is_idempotent_and_missing_is_noop(
    tmp_path: Path,
) -> None:
    """Deleting again (or an already-missing path) never raises."""

    hass = _FakeHass()
    present = tmp_path / "data" / "secrets.json"
    missing = tmp_path / "auth" / "secrets.json"
    _write_secrets(present, "present@example.com", token="aas_et/P")

    manager = SimpleNamespace(watch_paths=(missing, present))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    key = _stable_key_for("present@example.com")
    digest = _digest_for("present@example.com", "aas_et/P")

    # First delete removes the present file; the missing path is a no-op.
    await config_flow._async_delete_watched_secrets(
        hass, imported_stable_key=key, imported_digest=digest
    )
    assert not present.exists()

    # Second delete over now-empty paths must not raise.
    await config_flow._async_delete_watched_secrets(
        hass, imported_stable_key=key, imported_digest=digest
    )


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
    await config_flow._async_delete_watched_secrets(
        empty_hass,
        imported_stable_key=_stable_key_for("older@example.com"),
        imported_digest=_digest_for("older@example.com", "aas_et/OLD"),
    )
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


async def test_collect_extra_watch_paths_honours_exclude_entry_id(
    tmp_path: Path,
) -> None:
    """``exclude_entry_id`` drops the paths of exactly the named entry.

    Entry removal uses this so a watch path does not outlive its owning entry.
    An unrelated entry id must leave the result untouched.
    """

    extra = tmp_path / "data" / "secrets.json"
    hass = _FakeHass(extra_watch_paths=[str(extra)])

    assert discovery._collect_extra_watch_paths(hass, exclude_entry_id=None) == [extra]
    assert discovery._collect_extra_watch_paths(hass, exclude_entry_id="other") == [
        extra
    ]
    assert (
        discovery._collect_extra_watch_paths(hass, exclude_entry_id="watcher-entry")
        == []
    )


async def test_collect_extra_watch_paths_skips_disabled_entries(
    tmp_path: Path,
) -> None:
    """A disabled entry's extra path is not collected.

    ``ConfigEntries.async_entries`` returns disabled entries as well
    (``include_disabled`` defaults to ``True``), so a deliberately switched-off
    account would otherwise keep its secrets path polled.
    """

    enabled_path = tmp_path / "enabled" / "secrets.json"
    disabled_path = tmp_path / "disabled" / "secrets.json"
    hass = _EntriesHass(
        [
            make_config_entry(
                entry_id="entry-enabled",
                options={SECRETS_EXTRA_WATCH_PATHS: [str(enabled_path)]},
            ),
            make_config_entry(
                entry_id="entry-disabled",
                options={SECRETS_EXTRA_WATCH_PATHS: [str(disabled_path)]},
                disabled_by="user",
            ),
        ]
    )

    assert discovery._collect_extra_watch_paths(hass) == [enabled_path]


async def test_refresh_watch_paths_without_watchers_is_a_noop() -> None:
    """A manager that was never started has nothing to recompute.

    ``async_remove_entry`` calls this unconditionally (what it does is decided
    here, not by the caller), so this is the state on an instance where the
    discovery runtime failed to start.
    """

    manager = discovery.DiscoveryManager(_EntriesHass([]))

    await manager.async_refresh_watch_paths(exclude_entry_id="entry-gone")

    assert manager.watch_paths == ()


async def test_refresh_watch_paths_is_a_noop_when_the_path_set_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unchanged path set must not touch the watcher at all.

    The removal hook refreshes on every entry removal, and in the normal case
    the removed entry owned no extra path. Re-arming and rescanning there would
    re-import a bundle that is already known, and the discovery update flow
    applies such an import without asking the user.
    """

    default_path = tmp_path / "auth" / "secrets.json"
    _write_secrets(default_path, "user@example.com", token="aas_et/ONLY")

    hass = _EntriesHass([make_config_entry(entry_id="entry-plain")])
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)
    monkeypatch.setattr(discovery, "_default_watch_paths", lambda: [default_path])

    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    calls = _record_watcher_calls(monkeypatch)
    await manager.async_refresh_watch_paths(exclude_entry_id="entry-plain")

    assert calls == []
    assert len(triggered) == 1
    assert manager.watch_paths == (default_path,)

    await manager.async_stop()


async def test_refresh_watch_paths_does_not_rescan_when_paths_only_disappear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shrinking path set updates the watcher but keeps signature and scan.

    Nothing new can be discovered on a subset, so a forced scan there could only
    re-import a bundle the watcher has already seen on a surviving path.
    """

    default_path = tmp_path / "auth" / "secrets.json"
    extra_path = tmp_path / "extra" / "secrets.json"
    _write_secrets(default_path, "user@example.com", token="aas_et/ONLY")

    entry = make_config_entry(
        entry_id="entry-extra",
        options={SECRETS_EXTRA_WATCH_PATHS: [str(extra_path)]},
    )
    hass = _EntriesHass([entry])
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)
    monkeypatch.setattr(discovery, "_default_watch_paths", lambda: [default_path])

    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()
    await asyncio.sleep(0)
    assert manager.watch_paths == (default_path, extra_path)
    assert len(triggered) == 1

    calls = _record_watcher_calls(monkeypatch)
    await manager.async_refresh_watch_paths(exclude_entry_id="entry-extra")

    assert calls == [("update", False)]
    assert manager.watch_paths == (default_path,)
    # The still-present default bundle must not be handed to discovery twice.
    assert len(triggered) == 1

    # The signature must still be armed, not merely un-scanned: the next
    # periodic scan of the surviving path has to stay a no-op as well.
    await manager.async_force_secrets_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    await manager.async_stop()


async def test_refresh_watch_paths_rescans_when_a_path_is_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A newly configured path is armed and scanned immediately."""

    default_path = tmp_path / "auth" / "secrets.json"
    extra_path = tmp_path / "extra" / "secrets.json"
    _write_secrets(extra_path, "user@example.com", token="aas_et/ONLY")

    entry = make_config_entry(entry_id="entry-extra")
    hass = _EntriesHass([entry])
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)
    monkeypatch.setattr(discovery, "_default_watch_paths", lambda: [default_path])

    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()
    await asyncio.sleep(0)
    assert manager.watch_paths == (default_path,)
    assert triggered == []

    calls = _record_watcher_calls(monkeypatch)
    entry.options[SECRETS_EXTRA_WATCH_PATHS] = [str(extra_path)]
    await manager.async_refresh_watch_paths()
    await asyncio.sleep(0)

    assert calls == [("update", True), ("scan", None)]
    assert manager.watch_paths == (default_path, extra_path)
    assert len(triggered) == 1
    assert triggered[0]["email"] == "user@example.com"

    await manager.async_stop()


async def test_default_watch_paths_include_container_data_default() -> None:
    """F3: the zero-config defaults watch Auth/secrets.json AND docker-login/data.

    The login container writes its bundle to ``docker-login/data/secrets.json``
    (the writable compose ``./data`` volume), a deterministic sibling of the
    integration package. On a single-host install that path must be watched
    without any user configuration, so Track A works out of the box.

    Declared ``async`` although the assertions are synchronous: the module-level
    ``pytestmark`` marks every test here with ``pytest.mark.asyncio``, and a
    synchronous function under that mark makes pytest emit a ``PytestWarning``
    in every suite run.
    """

    defaults = discovery._default_watch_paths()

    auth_default = discovery._default_secrets_path()
    container_default = discovery._default_container_data_path()

    assert auth_default in defaults
    assert container_default in defaults
    # The container default is the deterministic docker-login/data sibling path.
    assert container_default.parts[-3:] == ("docker-login", "data", "secrets.json")
    # Both defaults live under the integration package directory.
    integration_dir = Path(discovery.__file__).resolve().parent
    assert integration_dir in container_default.parents
    assert integration_dir in auth_default.parents


async def test_manager_watches_container_data_default_zero_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DiscoveryManager wires the container-data default into the watcher (no option).

    Even with an empty options set (no ``SECRETS_EXTRA_WATCH_PATHS``), the manager
    must build its watcher over BOTH zero-config defaults so a same-machine
    one-click login is imported without any extra configuration.
    """

    captured: dict[str, Any] = {}

    class _StubWatcher:
        def __init__(self, hass: Any, *, paths: list[Path], **_kw: Any) -> None:
            captured["paths"] = list(paths)

        async def async_start(self) -> None:
            return None

    monkeypatch.setattr(discovery, "SecretsJSONWatcher", _StubWatcher)

    hass = _FakeHass()  # no extra_watch_paths option set
    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()

    paths = captured["paths"]
    assert discovery._default_secrets_path() in paths
    assert discovery._default_container_data_path() in paths


async def test_delete_clears_container_data_default_same_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The auto-default container-data path IS in delete scope (F4/F3 seam).

    The delete hook reads the manager's ``watch_paths``; because the container
    default is part of the default watch set it is removed after a successful
    import when it carries the imported account, exactly like an explicitly
    configured path.
    """

    hass = _FakeHass()
    auth_secret = tmp_path / "auth" / "secrets.json"
    container_secret = tmp_path / "docker-login" / "data" / "secrets.json"
    # Same account across both copies (the real Auth/+data/ redundancy).
    _write_secrets(auth_secret, "user@example.com", token="aas_et/U")
    _write_secrets(container_secret, "user@example.com", token="aas_et/U")

    # The manager exposes the auto-default container path among its watch paths.
    manager = SimpleNamespace(watch_paths=(auth_secret, container_secret))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/U"),
    )

    assert not auth_secret.exists()
    assert not container_secret.exists()


async def test_delete_removes_stale_same_account_copy_when_import_is_current(
    tmp_path: Path,
) -> None:
    """Import is the newest same-account bundle -> every same-account copy goes.

    The stale ``Auth/`` copy carries *older* content than the imported one. Since
    the newest same-account bundle on disk is still the imported payload, nothing
    fresher can be lost, so the leftover is removed instead of lingering as an
    orphan secret.
    """

    hass = _FakeHass()
    stale = tmp_path / "auth" / "secrets.json"
    imported = tmp_path / "data" / "secrets.json"
    _write_secrets(stale, "user@example.com", token="aas_et/OLD")
    _write_secrets(imported, "user@example.com", token="aas_et/NEW")
    os.utime(stale, (1_000, 1_000))
    os.utime(imported, (2_000, 2_000))

    manager = SimpleNamespace(watch_paths=(stale, imported))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/NEW"),
    )

    assert not stale.exists()
    assert not imported.exists()


async def test_delete_keeps_newer_same_account_bundle_written_during_confirmation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P1 race: a fresher same-account bundle must survive the delete.

    The account key collapses to ``email:<addr>``, so a key-only match would also
    hit a bundle the container wrote *while* the user was still confirming the
    discovery flow. Only the copies carrying the imported content are removed;
    the newer bundle stays on disk and the watcher imports it on its next scan.
    """

    import logging

    hass = _FakeHass()
    imported_copy = tmp_path / "auth" / "secrets.json"
    fresh = tmp_path / "data" / "secrets.json"
    _write_secrets(imported_copy, "user@example.com", token="aas_et/IMPORTED")
    _write_secrets(fresh, "user@example.com", token="aas_et/FRESH")
    os.utime(imported_copy, (1_000, 1_000))
    os.utime(fresh, (2_000, 2_000))

    manager = SimpleNamespace(watch_paths=(imported_copy, fresh))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.INFO):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("user@example.com"),
            imported_digest=_digest_for("user@example.com", "aas_et/IMPORTED"),
        )

    assert not imported_copy.exists()  # carries the imported content
    assert fresh.exists()  # newer credentials survive
    assert any(
        "not older than the imported one" in rec.getMessage() for rec in caplog.records
    )


def _tokens_ordered_by_digest(email: str) -> tuple[str, str]:
    """Return two tokens whose bundle digests have a known lexical order.

    ``(smaller_digest_token, larger_digest_token)``. The delete hook must not
    care which of the two the import carries, so the tie test drives both
    assignments from this pair instead of hard-coding a digest order that could
    silently flip when the bundle shape changes.
    """

    candidates = [f"aas_et/TIE{index}" for index in range(8)]
    ranked = sorted(candidates, key=lambda token: _digest_for(email, token))
    return ranked[0], ranked[-1]


@pytest.mark.parametrize("import_has_larger_digest", [True, False])
async def test_delete_keeps_fresher_bundle_on_identical_mtimes(
    tmp_path: Path, import_has_larger_digest: bool
) -> None:
    """P1 tie: with equal mtimes the digest order must not decide who dies.

    ``scan_secrets_bundles`` breaks equal mtimes with the larger SHA-256, which
    says nothing about age. Deriving "the import is still current" from that
    winner meant a lexically larger *old* digest could authorize the deletion of
    every same-account copy, including a fresh bundle the login container had
    just written (coarse mtimes on network mounts/QNAP are exactly why the
    tiebreak exists). Both digest orders must therefore end the same way: the
    file carrying the imported content goes, the differing one survives.
    """

    email = "user@example.com"
    smaller, larger = _tokens_ordered_by_digest(email)
    imported_token = larger if import_has_larger_digest else smaller
    fresh_token = smaller if import_has_larger_digest else larger
    assert (_digest_for(email, imported_token) > _digest_for(email, fresh_token)) is (
        import_has_larger_digest
    )

    hass = _FakeHass()
    imported_copy = tmp_path / "auth" / "secrets.json"
    fresh = tmp_path / "data" / "secrets.json"
    _write_secrets(imported_copy, email, token=imported_token)
    _write_secrets(fresh, email, token=fresh_token)
    # Identical timestamps: the container wrote within one mtime resolution step.
    os.utime(imported_copy, (1_000, 1_000))
    os.utime(fresh, (1_000, 1_000))

    manager = SimpleNamespace(watch_paths=(imported_copy, fresh))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for(email),
        imported_digest=_digest_for(email, imported_token),
    )

    assert not imported_copy.exists()  # exact content match: safe to remove
    assert fresh.exists()  # never imported, not provably older -> kept


async def test_delete_skips_file_rewritten_between_scan_and_remove(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TOCTOU: a bundle written after the scan must survive the delete.

    The hook scans in one executor job and would delete in another, so the flow
    yields the event loop in between. If the login container writes fresh
    credentials into that window, the path still carries the imported digest *in
    the snapshot* while holding new content on disk. The removal therefore
    re-reads the file inside the same executor job as the unlink and aborts on a
    digest mismatch.
    """

    import logging

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/IMPORTED")

    class _RewritingHass(_FakeHass):
        """Simulates the container writing right after the scan job returned."""

        def __init__(self) -> None:
            super().__init__()
            self.jobs = 0

        async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
            self.jobs += 1
            result = func(*args)
            if self.jobs == 1:  # the scan snapshot has just been taken
                _write_secrets(watched, "user@example.com", token="aas_et/FRESH")
            return result

    hass = _RewritingHass()
    manager = SimpleNamespace(watch_paths=(watched,))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.INFO):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("user@example.com"),
            imported_digest=_digest_for("user@example.com", "aas_et/IMPORTED"),
        )

    assert watched.exists()
    assert json.loads(watched.read_text(encoding="utf-8"))["oauth_token"] == (
        "aas_et/FRESH"
    )
    assert any(
        "content changed after the scan" in rec.getMessage() for rec in caplog.records
    )


async def test_delete_of_file_vanished_between_scan_and_remove_is_a_noop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that disappears after the scan is a silent no-op, not an error.

    The re-read guarding the unlink also covers the "someone else was faster"
    case (TTL cleanup, user, a second flow): the bundle is no longer readable, so
    the hook logs and returns instead of raising out of the config flow.
    """

    import logging

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/IMPORTED")

    class _UnlinkingHass(_FakeHass):
        """Removes the watched file right after the scan job returned."""

        def __init__(self) -> None:
            super().__init__()
            self.jobs = 0

        async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
            self.jobs += 1
            result = func(*args)
            if self.jobs == 1:  # the scan snapshot has just been taken
                watched.unlink()
            return result

    hass = _UnlinkingHass()
    manager = SimpleNamespace(watch_paths=(watched,))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.DEBUG):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("user@example.com"),
            imported_digest=_digest_for("user@example.com", "aas_et/IMPORTED"),
        )

    assert not watched.exists()
    assert any(
        "no longer identifiable since the scan" in rec.getMessage()
        for rec in caplog.records
    )


async def test_delete_keeps_all_when_only_a_newer_same_account_bundle_remains(
    tmp_path: Path,
) -> None:
    """The winner check is per account, not global.

    A globally newer *foreign* bundle must not make the hook believe the imported
    account moved on: the foreign file is kept as a foreign file, and the
    same-account copy is still recognized as the current import and removed.
    """

    hass = _FakeHass()
    imported_copy = tmp_path / "auth" / "secrets.json"
    foreign_newer = tmp_path / "other" / "secrets.json"
    _write_secrets(imported_copy, "user@example.com", token="aas_et/IMPORTED")
    _write_secrets(foreign_newer, "other@example.com", token="aas_et/OTHER")
    os.utime(imported_copy, (1_000, 1_000))
    os.utime(foreign_newer, (2_000, 2_000))

    manager = SimpleNamespace(watch_paths=(imported_copy, foreign_newer))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/IMPORTED"),
    )

    assert not imported_copy.exists()
    assert foreign_newer.exists()


async def test_payload_digest_matches_on_disk_digest_across_normalization(
    tmp_path: Path,
) -> None:
    """The imported payload and its source file must hash identically.

    The config flow stores the *normalized* bundle (whitespace stripped, scoped
    ``shared_key_<id>`` promoted), so a digest taken over the raw file content
    would never match the payload and the delete hook would degrade into "keep
    everything". ``secrets_bundle_digest`` normalizes first, which makes the two
    values identity-equal; this test pins that invariant with a bundle that
    normalization actually changes.
    """

    raw = {
        "google_email": "user@example.com",
        "oauth_token": "aas_et/NORMALIZATION",
        # Scoped key -> normalization promotes it to a top-level shared_key.
        "shared_key_1234": "DD EE FF",
    }
    path = tmp_path / "data" / "secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")

    on_disk = discovery.read_secrets_bundle(path)
    assert on_disk is not None

    payload = config_flow._normalize_and_validate_discovery_payload(
        {"secrets_json": dict(raw)}
    )
    assert payload.secrets_bundle is not None
    assert dict(payload.secrets_bundle) != raw  # normalization really changed it

    assert config_flow._digest_for_discovery_payload(payload) == on_disk.digest
    assert config_flow._stable_key_for_discovery_payload(payload) == on_disk.stable_key


# ---------------------------------------------------------------------------
# Deferred delete (P2): staged in the flow, executed in async_setup_entry
#
# ``ConfigFlow.async_create_entry`` only builds a FlowResult -- Home Assistant
# creates and stores the entry afterwards in
# ``ConfigEntriesFlowManager.async_finish_flow`` (``await
# self.config_entries.async_add(entry)``). Deleting the imported secrets files
# inside the step would therefore destroy the credentials while the entry may
# still fail to materialise. The flow stages the delete in ``hass.data``
# (in-memory only, never HA storage) and ``async_setup_entry`` runs it.
# ---------------------------------------------------------------------------


def _importable_bundle(email: str, token: str) -> dict[str, Any]:
    """A bundle that passes the discovery single-key gate (needs a shared_key)."""

    return {
        "google_email": email,
        "oauth_token": token,
        "shared_key": "DDEEFF",
    }


def _staged_cleanup(hass: Any) -> list[Any]:
    """Return the in-memory staging area the flow writes its cleanup tickets to.

    A FIFO list of per-flow tickets, not a mapping keyed by account: two
    overlapping create flows for the same account must stay separable.
    """

    bucket = hass.data.get(DOMAIN) or {}
    staged = bucket.get(config_flow.PENDING_CONTAINER_CLEANUP_KEY) or []
    assert isinstance(staged, list)
    return staged


async def _run_staged_cleanup(
    hass: Any, *, unique_id: str | None, entry_id: str | None = None
) -> None:
    """Claim one staged ticket and execute it, as the durability gate does.

    Production never runs the jobs inline: ``async_setup_entry`` only claims the
    ticket and arms a background task that waits for proof that Home Assistant's
    storage holds the state that authorises the cleanup (see
    ``config_flow.async_schedule_pending_container_cleanup``). These tests cover
    the job semantics *after* that proof, so they drive the two halves directly
    instead of standing up HA's storage against a hand-built ``hass`` double.

    ``entry_id`` is required for the tickets of the *update* paths: those name
    their entry, and a claim that does not name the same entry must not get
    them.
    """

    jobs = config_flow._async_claim_container_cleanup(
        hass, unique_id=unique_id, entry_id=entry_id
    )
    await config_flow._async_execute_container_cleanup(hass, jobs)


async def test_discovery_confirm_stages_delete_instead_of_running_it(
    tmp_path: Path,
) -> None:
    """The confirmed discovery flow stages the delete; the file survives the step.

    Pins the actual regression: a ``CREATE_ENTRY`` FlowResult is a promise, not
    a stored entry, so the watched copies must still be on disk when the step
    returns. Only the staged job is observable.
    """

    hass = _FakeHass()
    watched = tmp_path / "data" / "secrets.json"
    bundle = _importable_bundle("user@example.com", "aas_et/CONFIRMED")
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text(json.dumps(bundle), encoding="utf-8")
    hass.data[DOMAIN] = {"discovery_manager": SimpleNamespace(watch_paths=(watched,))}

    on_disk = discovery.read_secrets_bundle(watched)
    assert on_disk is not None

    payload = config_flow._normalize_and_validate_discovery_payload(
        {"google_email": "user@example.com", "secrets_json": dict(bundle)}
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._discovery_confirm_pending = True  # type: ignore[attr-defined]
    flow._pending_discovery_payload = payload  # type: ignore[attr-defined]
    flow._pending_discovery_updates = None  # type: ignore[attr-defined]

    async def _fake_device_selection() -> dict[str, Any]:
        return {"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    result = await flow.async_step_discovery({})
    assert result["type"] == config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY

    # NOT deleted yet: the entry does not exist at this point.
    assert watched.exists()

    # Staged instead, on this flow's own ticket. The synthetic flow never set a
    # unique id, so the ticket stays account-less: claimable by any entry of
    # this integration, but still only by exactly one of them.
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].unique_id is None
    jobs = staged[0].jobs
    assert len(jobs) == 1
    # The staged identity is the one of the file that must eventually go.
    assert jobs[0].imported_stable_key == on_disk.stable_key
    assert jobs[0].imported_digest == on_disk.digest
    # A pure delete job carries no container ack.
    assert jobs[0].ack is None


async def test_aborted_discovery_confirm_stages_nothing(tmp_path: Path) -> None:
    """Device selection that does not create an entry stages no delete."""

    hass = _FakeHass()
    watched = tmp_path / "data" / "secrets.json"
    bundle = _importable_bundle("user@example.com", "aas_et/ABORTED_TOKEN_VALUE")
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text(json.dumps(bundle), encoding="utf-8")
    hass.data[DOMAIN] = {"discovery_manager": SimpleNamespace(watch_paths=(watched,))}

    payload = config_flow._normalize_and_validate_discovery_payload(
        {"google_email": "user@example.com", "secrets_json": dict(bundle)}
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._discovery_confirm_pending = True  # type: ignore[attr-defined]
    flow._pending_discovery_payload = payload  # type: ignore[attr-defined]
    flow._pending_discovery_updates = None  # type: ignore[attr-defined]

    async def _fake_device_selection() -> dict[str, Any]:
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    result = await flow.async_step_discovery({})
    assert result["type"] == "form"
    assert watched.exists()
    assert _staged_cleanup(hass) == []


async def test_staged_delete_runs_once_when_setup_entry_claims_it(
    tmp_path: Path,
) -> None:
    """The runner deletes the consumed copies, and only for the first claim.

    The second run stands in for a reload: ``async_setup_entry`` executes again
    on every reload, so a re-created bundle (the watcher's next import) must NOT
    be swept away by a job that already ran.
    """

    hass = _FakeHass()
    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/DEFERRED")
    hass.data[DOMAIN] = {"discovery_manager": SimpleNamespace(watch_paths=(watched,))}

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-claim-once",
        unique_id="user@example.com",
        job=config_flow.PendingContainerCleanup(
            imported_stable_key=_stable_key_for("user@example.com", "aas_et/DEFERRED"),
            imported_digest=_digest_for("user@example.com", "aas_et/DEFERRED"),
        ),
    )
    assert watched.exists()

    await _run_staged_cleanup(hass, unique_id="user@example.com")
    assert not watched.exists()
    assert _staged_cleanup(hass) == []

    # Reload with a freshly written bundle: the job was consumed, so nothing
    # touches the new file.
    _write_secrets(watched, "user@example.com", token="aas_et/DEFERRED")
    await _run_staged_cleanup(hass, unique_id="user@example.com")
    assert watched.exists()


async def test_staged_delete_failure_never_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A delete that blows up must not surface in ``async_setup_entry``.

    The credential file simply stays put and the watcher re-imports it on its
    next scan, which is strictly better than failing an otherwise good setup.
    """

    hass = _FakeHass()
    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/BOOM")
    hass.data[DOMAIN] = {"discovery_manager": SimpleNamespace(watch_paths=(watched,))}

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("delete exploded")

    monkeypatch.setattr(config_flow, "_async_delete_watched_secrets", _boom)

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-delete-boom",
        unique_id="user@example.com",
        job=config_flow.PendingContainerCleanup(
            imported_stable_key=_stable_key_for("user@example.com", "aas_et/BOOM"),
            imported_digest=_digest_for("user@example.com", "aas_et/BOOM"),
        ),
    )

    # Must not raise.
    await _run_staged_cleanup(hass, unique_id="user@example.com")
    assert watched.exists()


class _UpdateFlowHass:
    """Hass double for the discovery-*update* flow (existing entry present).

    Richer than :class:`_FakeHass`: the update path resolves an existing entry,
    writes it through ``async_update_entry`` and schedules a reload, so the
    ``config_entries`` double needs the lookup/update/reload surface on top of
    the executor hop the delete hook uses.
    """

    def __init__(self, entry: Any) -> None:
        self.data: dict[str, Any] = {}
        self._entry = entry

        class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
            def __init__(self) -> None:
                self.updated: list[tuple[Any, dict[str, Any]]] = []
                self.reloaded: list[str] = []
                attach_config_entries_flow_manager(self)

            def async_entries(self, domain: str) -> list[Any]:
                return [entry] if domain == DOMAIN else []

            def async_get_entry(self, entry_id: str) -> Any | None:
                return entry if entry_id == entry.entry_id else None

            def async_update_entry(self, target: Any, **updates: Any) -> None:
                self.updated.append((target, updates))
                if "data" in updates:
                    target.data = updates["data"]

            def async_reload(self, entry_id: str) -> None:
                self.reloaded.append(entry_id)

        prepare_flow_hass_config_entries(self, _ConfigEntries, frame_module=frame)
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)

    def async_create_task(self, coro: Any, *_args: Any, **_kwargs: Any) -> Any:
        return asyncio.ensure_future(coro)


async def test_discovery_update_case_stages_the_delete_behind_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The discovery-*update* case stages its delete instead of running it.

    ``hass.config_entries.async_update_entry`` does not write through: it
    mutates the in-memory entry and schedules Home Assistant's debounced store
    save. Deleting the imported bundle inside that window would mean a crash
    there restores the OLD credentials while the newly imported file is already
    gone -- the exact failure direction this subsystem exists to exclude
    (Codex P2, fund A).

    Drives the *production* step (``async_step_discovery_update_info`` with an
    existing entry), not the delete helper: calling the helper directly would
    keep passing even if the step lost its staging altogether. Only the
    credential ingestion is mocked out, because that one talks to Google.
    """

    watched = tmp_path / "data" / "secrets.json"
    bundle = _importable_bundle("user@example.com", "aas_et/INLINE_UPDATE_TOKEN")
    watched.parent.mkdir(parents=True, exist_ok=True)
    watched.write_text(json.dumps(bundle), encoding="utf-8")

    entry = make_config_entry(
        entry_id="entry-update-inline",
        data={"google_email": "user@example.com", "oauth_token": "aas_et/OLD"},
        unique_id=unique_account_id("user@example.com"),
        subentries={},
    )
    hass = _UpdateFlowHass(entry)
    hass.data[DOMAIN] = {"discovery_manager": SimpleNamespace(watch_paths=(watched,))}

    ingested: list[Any] = []

    async def _fake_ingest(
        _flow: Any, normalized: Any, *, existing_entry: Any | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        # The real ingest validates the token against Google; the delete
        # identity comes from ``normalized``, which is built for real above.
        ingested.append(normalized)
        return ({}, {"data": {"oauth_token": "aas_et/INLINE_UPDATE_TOKEN"}})

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _fake_ingest)

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": config_flow.DISCOVERY_UPDATE_SOURCE}
    flow._async_current_entries = lambda **_: [entry]  # type: ignore[assignment]

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

    result = await flow.async_step_discovery_update_info(
        {"google_email": "user@example.com", "secrets_json": dict(bundle)}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert ingested, "the update path never reached the credential ingest"
    # The entry update happened, and the reload that will arm the gate was
    # scheduled.
    assert [target for target, _ in hass.config_entries.updated] == [entry]
    assert hass.config_entries.reloaded == [entry.entry_id]
    # NOT deleted in the step: the save is still pending at this point.
    assert watched.exists()
    # Staged instead, addressed to this entry and gated on its watermark, so
    # the proof cannot be satisfied by the entry id that was stored all along.
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == entry.entry_id
    assert staged[0].min_modified_at == entry.modified_at
    assert len(staged[0].jobs) == 1
    assert staged[0].jobs[0].imported_digest is not None

    # And the staged job is the real one: running it through the executor half
    # of the gate removes exactly the file the import consumed.
    await _run_staged_cleanup(hass, unique_id=entry.unique_id, entry_id=entry.entry_id)
    assert not watched.exists()


async def test_delete_swallows_file_not_found_race_at_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing the file between the guarded re-read and ``os.remove`` is a no-op.

    ``_remove_if_digest_matches`` re-reads the bundle and unlinks it in the same
    executor job, but POSIX has no atomic re-read+remove: a second deleter (the
    container's own TTL cleanup, a parallel flow) can still win the final gap, so
    ``os.remove`` raises ``FileNotFoundError``. The hook must swallow it *silently*
    and let the config flow complete instead of propagating out of the durability
    gate.
    """

    import logging

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/GONE")

    removed: list[str] = []

    def _raise_missing(path: str) -> None:
        removed.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(config_flow.os, "remove", _raise_missing)

    hass = _FakeHass()
    manager = SimpleNamespace(watch_paths=(watched,))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    # Must not raise even though the unlink hits a vanished file.
    with caplog.at_level(logging.WARNING):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("user@example.com"),
            imported_digest=_digest_for("user@example.com", "aas_et/GONE"),
        )

    # The unlink branch was actually entered (the digest still matched) and the
    # FileNotFoundError was swallowed rather than propagated.
    assert removed == [str(watched)]
    # Distinct from the OSError branch below: a file that vanished in the final
    # gap is a benign race and is swallowed *without* a warning. Pinning the
    # silence is what stops ``except FileNotFoundError`` from collapsing into the
    # ``except OSError`` handler (FileNotFoundError is an OSError subclass).
    assert not any(
        "Failed to remove watched secrets file after import" in rec.getMessage()
        for rec in caplog.records
    )


async def test_delete_warns_and_completes_on_unremovable_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-removable watched file logs a warning and never breaks the flow.

    The hook promises it "never raises: a non-writable external path only logs a
    warning so the flow completes". A same-account secrets copy on a read-only
    mount is exactly that case: ``os.remove`` raises ``OSError`` and the flow has
    to survive it.
    """

    import logging

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/RO")

    def _raise_oserror(path: str) -> None:
        raise PermissionError(path)  # subclass of OSError

    monkeypatch.setattr(config_flow.os, "remove", _raise_oserror)

    hass = _FakeHass()
    manager = SimpleNamespace(watch_paths=(watched,))
    hass.data[DOMAIN] = {"discovery_manager": manager}

    with caplog.at_level(logging.WARNING):
        await config_flow._async_delete_watched_secrets(
            hass,
            imported_stable_key=_stable_key_for("user@example.com"),
            imported_digest=_digest_for("user@example.com", "aas_et/RO"),
        )

    # The file is still on disk (the real remove never ran) and the flow logged a
    # warning instead of raising.
    assert watched.exists()
    assert any(
        "Failed to remove watched secrets file after import" in rec.getMessage()
        for rec in caplog.records
    )


async def test_delete_with_no_hass_is_a_noop(tmp_path: Path) -> None:
    """A ``None`` hass (teardown/edge invocation) is a silent no-op, not a crash.

    The hook is armed from the durability gate; if it ever fires without a live
    HomeAssistant it must return rather than dereference ``None.data``.

    The standalone ``if hass is None`` early-return was folded into the mapping
    guard (``if hass is None or not isinstance(domain_data, Mapping)``): runtime
    the ``None`` check is redundant with ``getattr(None, "data", None)`` yielding
    ``None``, but it still carries the type-narrowing that ``mypy --strict`` needs
    for the later ``hass.async_add_executor_job`` calls, which it cannot infer
    through ``getattr``. This test pins the no-crash contract for a ``None`` hass.
    """

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/NOHASS")

    # No exception, and nothing to act on without a hass.
    await config_flow._async_delete_watched_secrets(
        None,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/NOHASS"),
    )

    assert watched.exists()


async def test_delete_with_non_mapping_hass_data_is_a_noop(tmp_path: Path) -> None:
    """A hass whose ``.data`` is not the domain mapping yet is a no-op.

    Early in startup ``hass.data`` may not be a mapping; the hook must bail out
    instead of calling ``.get`` on the wrong type and raising into the flow.
    """

    watched = tmp_path / "data" / "secrets.json"
    _write_secrets(watched, "user@example.com", token="aas_et/NODATA")

    hass = SimpleNamespace(data=None)

    await config_flow._async_delete_watched_secrets(
        hass,
        imported_stable_key=_stable_key_for("user@example.com"),
        imported_digest=_digest_for("user@example.com", "aas_et/NODATA"),
    )

    assert watched.exists()


async def test_undeletable_winner_does_not_shadow_a_second_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read-only winner that cannot be deleted must not block other accounts.

    Regression for the multi-account signature lock: ``_read_bundles`` returns
    the newest bundle as the winner, and before the fix an armed winner that
    stayed on disk (read-only mount, delete-after-import cannot remove it) made
    every later scan short-circuit on the unchanged signature, so a second
    account's older bundle was never armed. The watcher now skips already
    dispatched signatures and steps to the next candidate.
    """

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    older = tmp_path / "auth" / "secrets.json"  # account two
    newer = tmp_path / "data" / "secrets.json"  # account one (winner)
    _write_secrets(older, "second@example.com", token="aas_et/TWO")
    _write_secrets(newer, "first@example.com", token="aas_et/ONE")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    watcher = discovery.SecretsJSONWatcher(hass, paths=[older, newer])

    # First scan arms the winner (newest); it is deliberately NOT deleted, as a
    # read-only mount would leave it in place.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert [t["email"] for t in triggered] == ["first@example.com"]

    # Second scan: the winner is still the newest bundle on disk, but it has
    # already been dispatched. Without the settled-set skip this returned early;
    # now it steps to the older, second account.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert [t["email"] for t in triggered] == [
        "first@example.com",
        "second@example.com",
    ]

    # Third scan: both bundles are settled -> nothing new is armed.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 2

    await watcher.async_stop()


async def test_settled_winner_is_not_rearmed_until_it_fully_disappears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A settled bundle is armed once; again only after the whole path set goes
    empty (which drops the settled set) and it reappears.
    """

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []
    _patch_discovery(monkeypatch, triggered)

    path = tmp_path / "data" / "secrets.json"
    _write_secrets(path, "solo@example.com", token="aas_et/SOLO")
    os.utime(path, (1_000, 1_000))

    watcher = discovery.SecretsJSONWatcher(hass, paths=[path])

    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    # Still present and unchanged -> settled -> not re-armed.
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    # The bundle vanishes entirely: the settled set is dropped by _forget_signature.
    path.unlink()
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 1

    # A byte-identical bundle reappears -> a fresh import, as documented.
    _write_secrets(path, "solo@example.com", token="aas_et/SOLO")
    os.utime(path, (3_000, 3_000))
    await watcher.async_force_scan()
    await asyncio.sleep(0)
    assert len(triggered) == 2

    await watcher.async_stop()


async def test_stale_failure_of_a_concurrent_dispatch_is_not_lost(
    tmp_path: Path,
) -> None:
    """A failure for a bundle that is no longer the last-armed one must drop it
    from the settled set, so the next scan re-arms it instead of losing it.

    The settled set permits several concurrent in-flight dispatches (that is the
    multi-account fix). The single ``_last_signature`` retry budget only tracks
    the most recently armed bundle, so a stale failure callback for an earlier,
    still-in-flight bundle takes the ``_last_signature != signature`` branch. It
    must discard that signature rather than leave it settled forever, which would
    drop the account with no retry and no give-up warning. The multi-in-flight
    occurrence itself was reproduced against the live watcher during review; this
    pins the method contract that prevents the silent loss.
    """

    hass = _FakeHass()
    watcher = discovery.SecretsJSONWatcher(hass, paths=[tmp_path / "secrets.json"])

    # Two bundles were armed; the newest ("acct-B") is the last-armed one.
    watcher._settled_signatures = {"acct-A", "acct-B"}
    watcher._last_signature = "acct-B"

    # A stale failure arrives for the earlier, still-in-flight bundle "acct-A".
    watcher._invalidate_signature("acct-A")

    # "acct-A" is released for a fresh re-arm; "acct-B" stays settled/in flight.
    assert "acct-A" not in watcher._settled_signatures
    assert "acct-B" in watcher._settled_signatures

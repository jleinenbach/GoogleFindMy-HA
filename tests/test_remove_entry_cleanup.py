# tests/test_remove_entry_cleanup.py

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import DOMAIN, discovery
from custom_components.googlefindmy.const import (
    OPT_DELETE_CACHES_ON_REMOVE,
    SECRETS_EXTRA_WATCH_PATHS,
)
from tests.helpers.config_entries_stub import make_config_entry


class _TokenCacheStub:
    """Capture close/remove calls during entry removal."""

    def __init__(self) -> None:
        self.closed = False
        self.store_removed = False

    async def close(self) -> None:
        self.closed = True

    async def async_remove_store(self) -> None:
        self.store_removed = True


class _CoordinatorStub:
    """Minimal coordinator stub tracking shutdown."""

    def __init__(self) -> None:
        self.shutdown_called = False

    async def async_shutdown(self) -> None:
        self.shutdown_called = True


class _GoogleHomeFilterStub:
    """Stubbed Google Home filter capturing shutdown invocations."""

    def __init__(self) -> None:
        self.shutdown_called = False

    def async_shutdown(self) -> None:
        self.shutdown_called = True


class _HassStub:
    """Home Assistant stub exposing the data layout used by async_remove_entry."""

    def __init__(
        self, entry: SimpleNamespace, runtime_data: integration.RuntimeData
    ) -> None:
        self.data: dict[str, Any] = {
            DOMAIN: {
                "entries": {entry.entry_id: runtime_data},
                "device_owner_index": {"device-1": entry.entry_id},
            }
        }
        self.config_entries = SimpleNamespace(async_entries=lambda _domain=None: [])


class _DiscoveryHassStub:
    """Hass stub rich enough to host a live ``DiscoveryManager`` during removal.

    ``_HassStub`` above reports a permanently empty entry set, which is exactly
    what the watch-path regressions must vary. This stub therefore keeps a
    mutable entry list and adds the executor job runner and the event bus that
    the manager and its watcher use. ``async_create_task`` is present for
    completeness only: these tests never let a scan find a bundle, so no
    discovery flow is ever queued.
    """

    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {DOMAIN: {"entries": {}, "device_owner_index": {}}}
        self.entries: list[Any] = list(entries)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain=None: (
                list(self.entries) if domain in (None, DOMAIN) else []
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


async def _start_discovery_manager(
    hass: _DiscoveryHassStub,
    monkeypatch: pytest.MonkeyPatch,
    default_path: Path,
    triggered: list[dict[str, Any]] | None = None,
) -> Any:
    """Start a real discovery manager on ``hass`` and register it as singleton.

    ``default_path`` replaces :func:`discovery._default_watch_paths`. The real
    defaults are repo-relative (``Auth/secrets.json`` and
    ``docker-login/data/secrets.json``), so without this the test would read
    whatever a login container left in the working copy; the watch-path
    bookkeeping under test does not care which paths the defaults are.

    The watcher stays inert where it would reach out: the poll timer, the entry
    lookup and the translation backend are replaced, and
    ``_trigger_cloud_discovery`` -- the coroutine the results container
    schedules when a scan appends a payload -- is replaced by a stub that
    records its keyword arguments into ``triggered`` instead of opening a flow.
    """

    async def _fake_trigger(_hass: Any, **kwargs: Any) -> bool:
        if triggered is not None:
            triggered.append(kwargs)
        return True

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(discovery, "_default_watch_paths", lambda: [default_path])
    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(
        discovery, "async_track_time_interval", lambda *_args: lambda: None
    )
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_args: None)
    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    manager = discovery.DiscoveryManager(hass)
    await manager.async_start()
    hass.data[DOMAIN]["discovery_manager"] = manager
    return manager


def _watch_path_entry(entry_id: str, extra_path: Path) -> Any:
    """Return a config entry stub owning exactly one extra watch path."""

    return make_config_entry(
        entry_id=entry_id,
        title=entry_id,
        options={
            SECRETS_EXTRA_WATCH_PATHS: [str(extra_path)],
            # Keep the cache purge (and its Store round trip) out of scope.
            OPT_DELETE_CACHES_ON_REMOVE: False,
        },
    )


@pytest.fixture
def _no_fcm_release(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Intercept shared FCM release calls and record invocations."""

    calls: list[None] = []

    async def _fake_release(hass: Any, entry: Any | None = None) -> None:  # noqa: ANN401 - test stub
        calls.append(None)

    monkeypatch.setattr(integration, "_async_release_shared_fcm", _fake_release)
    return calls


def _setup_runtime(
    entry: SimpleNamespace,
) -> tuple[
    _CoordinatorStub, _TokenCacheStub, _GoogleHomeFilterStub, integration.RuntimeData
]:
    """Create runtime objects associated with the entry stub."""

    coordinator = _CoordinatorStub()
    token_cache = _TokenCacheStub()
    google_home_filter = _GoogleHomeFilterStub()
    runtime_data = integration.RuntimeData(
        coordinator=coordinator,
        token_cache=token_cache,
        subentry_manager=SimpleNamespace(),
        fcm_receiver=None,
        google_home_filter=google_home_filter,
    )
    entry.runtime_data = runtime_data
    return coordinator, token_cache, google_home_filter, runtime_data


def test_async_remove_entry_purges_store_and_creates_issue(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """Removing an entry should purge the cache and create an informational issue."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    entry = make_config_entry(
        entry_id="entry-remove",
        title="Find My Entry",
    )
    coordinator, token_cache, google_home_filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert token_cache.closed is True
    assert token_cache.store_removed is True
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert "device-1" not in hass.data[DOMAIN]["device_owner_index"]
    assert entry.runtime_data is None
    assert _no_fcm_release  # FCM release should have been attempted

    issue_id = f"cache_purged_{entry.entry_id}"
    assert any(item["issue_id"] == issue_id for item in issue_registry_capture.created)

    # Removal must retire every entry-scoped FCM repair issue, since no
    # supervisor will run again to clear them (Codex PR #1104 follow-up).
    deleted_ids = {iid for _domain, iid in issue_registry_capture.deleted}
    assert f"fcm_reauth_required_{entry.entry_id}" in deleted_ids
    assert f"fcm_stuck_{entry.entry_id}" in deleted_ids
    assert f"fcm_short_run_crash_loop_{entry.entry_id}" in deleted_ids


def test_async_remove_entry_respects_retention_option(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """When cache deletion is disabled, the store should be preserved and no issue created."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    entry = make_config_entry(
        entry_id="entry-remove",
        title="Find My Entry",
    )
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    coordinator, token_cache, google_home_filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    issue_id = f"cache_purged_{entry.entry_id}"
    issue_registry_capture.registry.issues[issue_id] = {
        "issue_id": issue_id,
        "domain": DOMAIN,
    }

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert token_cache.store_removed is False
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert entry.runtime_data is None
    assert _no_fcm_release

    assert all(item["issue_id"] != issue_id for item in issue_registry_capture.created)
    assert (DOMAIN, issue_id) in issue_registry_capture.deleted


def test_async_remove_entry_fallback_store_remove(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """Fallback store removal should run when no token cache is present."""

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    store_args: list[tuple[Any, Any, str]] = []
    store_removals: list[str] = []

    class _StoreStub:
        def __init__(self, hass_obj: Any, version: Any, key: str) -> None:
            store_args.append((hass_obj, version, key))
            self._hass = hass_obj
            self._key = key

        async def async_remove(self) -> None:
            store_removals.append(self._key)

    monkeypatch.setattr(integration, "Store", _StoreStub)

    entry = make_config_entry(
        entry_id="entry-remove",
        title="Find My Entry",
    )
    coordinator = _CoordinatorStub()
    google_home_filter = _GoogleHomeFilterStub()
    runtime_data = integration.RuntimeData(
        coordinator=coordinator,
        token_cache=None,
        subentry_manager=SimpleNamespace(),
        fcm_receiver=None,
        google_home_filter=google_home_filter,
    )
    entry.runtime_data = runtime_data
    hass = _HassStub(entry, runtime_data)

    asyncio.run(integration.async_remove_entry(hass, entry))

    assert coordinator.shutdown_called is True
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert entry.runtime_data is None
    assert store_args == [
        (
            hass,
            integration.STORAGE_VERSION,
            f"{integration.STORAGE_KEY}_{entry.entry_id}",
        )
    ]
    assert store_removals == [f"{integration.STORAGE_KEY}_{entry.entry_id}"]
    assert _no_fcm_release

    issue_id = f"cache_purged_{entry.entry_id}"
    assert any(item["issue_id"] == issue_id for item in issue_registry_capture.created)


@pytest.mark.asyncio
async def test_async_remove_entry_drops_watch_path_of_removed_entry(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
    tmp_path: Path,
) -> None:
    """Removing an entry stops the watcher from polling that entry's extra path.

    The update listener that normally recomputes the paths is unregistered by
    the unload preceding the removal, so ``async_remove_entry`` must trigger the
    refresh itself. A second entry proves there is no collateral damage: its own
    extra path survives.
    """

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    default_path = tmp_path / "defaults" / "secrets.json"
    removed_path = tmp_path / "removed" / "secrets.json"
    survivor_path = tmp_path / "survivor" / "secrets.json"
    removed_entry = _watch_path_entry("entry-removed", removed_path)
    survivor_entry = _watch_path_entry("entry-survivor", survivor_path)

    hass = _DiscoveryHassStub([removed_entry, survivor_entry])
    manager = await _start_discovery_manager(hass, monkeypatch, default_path)

    assert removed_path in manager.watch_paths
    assert survivor_path in manager.watch_paths

    # Home Assistant deletes the entry from its registry *before* it awaits
    # async_remove_entry (ConfigEntries._async_remove), so mirror that order.
    hass.entries.remove(removed_entry)

    await integration.async_remove_entry(hass, removed_entry)

    assert removed_path not in manager.watch_paths
    assert survivor_path in manager.watch_paths
    # The zero-config defaults must stay observed after the removal.
    assert default_path in manager.watch_paths

    await manager.async_stop()


@pytest.mark.asyncio
async def test_async_remove_entry_excludes_entry_still_in_registry(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
    tmp_path: Path,
) -> None:
    """The removed entry is excluded explicitly, not only implicitly.

    Defense in depth: the registry here still reports the entry being removed,
    which is what would happen if Home Assistant ever awaited
    ``async_remove_entry`` before dropping the entry from ``_entries``. That
    ordering is an undocumented internal, so the exclusion must not rely on it.
    """

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    default_path = tmp_path / "defaults" / "secrets.json"
    removed_path = tmp_path / "removed" / "secrets.json"
    survivor_path = tmp_path / "survivor" / "secrets.json"
    removed_entry = _watch_path_entry("entry-removed", removed_path)
    survivor_entry = _watch_path_entry("entry-survivor", survivor_path)

    hass = _DiscoveryHassStub([removed_entry, survivor_entry])
    manager = await _start_discovery_manager(hass, monkeypatch, default_path)

    assert removed_path in manager.watch_paths

    # Deliberately NOT removing the entry from the registry stub.
    await integration.async_remove_entry(hass, removed_entry)

    assert removed_entry in hass.entries
    assert removed_path not in manager.watch_paths
    assert survivor_path in manager.watch_paths

    await manager.async_stop()


@pytest.mark.asyncio
async def test_async_remove_entry_does_not_rescan_foreign_bundle(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
    tmp_path: Path,
) -> None:
    """Removing an entry must not re-import a bundle that is already known.

    The removal refresh fires on *every* removal, and in the normal case the
    removed entry owns no extra path at all. A bundle of a different account
    left on a watched path must not be handed to discovery a second time: the
    discovery update flow applies such an import to the matching entry without
    any user interaction, which would overwrite that entry's credentials and
    delete its bundle.
    """

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    default_path = tmp_path / "defaults" / "secrets.json"
    default_path.parent.mkdir(parents=True, exist_ok=True)
    default_path.write_text(
        json.dumps(
            {"google_email": "other@example.com", "oauth_token": "aas_et/OTHER"}
        ),
        encoding="utf-8",
    )

    # Neither entry configures an extra path, so the removal cannot change the
    # observed path set at all.
    removed_entry = make_config_entry(
        entry_id="entry-removed",
        title="entry-removed",
        options={OPT_DELETE_CACHES_ON_REMOVE: False},
    )
    survivor_entry = make_config_entry(
        entry_id="entry-survivor",
        title="entry-survivor",
        options={OPT_DELETE_CACHES_ON_REMOVE: False},
    )

    hass = _DiscoveryHassStub([removed_entry, survivor_entry])
    triggered: list[dict[str, Any]] = []
    manager = await _start_discovery_manager(hass, monkeypatch, default_path, triggered)
    await asyncio.sleep(0)

    # The manager's initial scan imports the bundle exactly once.
    assert [call["email"] for call in triggered] == ["other@example.com"]

    hass.entries.remove(removed_entry)
    await integration.async_remove_entry(hass, removed_entry)
    await asyncio.sleep(0)

    assert [call["email"] for call in triggered] == ["other@example.com"]
    assert manager.watch_paths == (default_path,)

    await manager.async_stop()


@pytest.mark.asyncio
async def test_async_remove_entry_survives_failing_watch_path_refresh(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],
) -> None:
    """A failing watch-path refresh is logged, never fatal for the removal.

    Watch-path bookkeeping is housekeeping: if the discovery manager raises,
    the entry must still be torn down completely.
    """

    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)

    calls: list[str | None] = []

    class _RaisingDiscoveryManager:
        async def async_refresh_watch_paths(
            self, *, exclude_entry_id: str | None = None
        ) -> None:
            calls.append(exclude_entry_id)
            raise RuntimeError("watcher refresh exploded")

    entry = make_config_entry(entry_id="entry-remove", title="Find My Entry")
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    coordinator, _token_cache, google_home_filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)
    hass.data[DOMAIN]["discovery_manager"] = _RaisingDiscoveryManager()

    await integration.async_remove_entry(hass, entry)

    assert calls == [entry.entry_id]
    assert coordinator.shutdown_called is True
    assert google_home_filter.shutdown_called is True
    assert entry.entry_id not in hass.data[DOMAIN]["entries"]
    assert "device-1" not in hass.data[DOMAIN]["device_owner_index"]


@pytest.mark.asyncio
async def test_async_remove_entry_drains_only_this_entrys_cleanup_tickets() -> None:
    """The removal seam passes the entry id, not the account, to the drain.

    ``async_remove_entry`` is the only production caller of
    ``async_discard_pending_container_cleanup_for_entry``. Without this test the
    seam is unverified: a later refactor to ``unique_id`` would make
    ``ticket.entry_id == <unique_id>`` never match, the tickets would survive the
    removal holding a pairing nonce and a delete token, and no test would go red.
    The foreign ticket pins the other direction -- the drain must not reach
    across entries.
    """

    from custom_components.googlefindmy import config_flow
    from custom_components.googlefindmy.config_flow import (
        PENDING_CONTAINER_CLEANUP_KEY,
    )
    from custom_components.googlefindmy.const import CONTAINER_TOKEN_PORT

    def _stage(hass: Any, *, flow_id: str, entry: Any) -> None:
        config_flow._async_stage_container_cleanup_for(
            hass,
            flow_id=flow_id,
            unique_id=getattr(entry, "unique_id", None),
            job=config_flow.PendingContainerCleanup(
                ack=config_flow._ContainerAckTarget(
                    host="127.0.0.1",
                    port=CONTAINER_TOKEN_PORT,
                    pairing_code="pair-code",
                    delete_token="delete-token",
                )
            ),
            entry=entry,
        )

    entry = make_config_entry(
        entry_id="entry-remove", unique_id="user@example.com", title="Find My Entry"
    )
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    _coordinator, _token_cache, _filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    # Two tickets for the entry being removed, one for a different entry.
    foreign = make_config_entry(entry_id="entry-other", unique_id="other@example.com")
    _stage(hass, flow_id="flow-a", entry=entry)
    _stage(hass, flow_id="flow-b", entry=entry)
    _stage(hass, flow_id="flow-foreign", entry=foreign)
    assert len(hass.data[DOMAIN][PENDING_CONTAINER_CLEANUP_KEY]) == 3

    await integration.async_remove_entry(hass, entry)

    survivors = hass.data[DOMAIN][PENDING_CONTAINER_CLEANUP_KEY]
    assert [ticket.entry_id for ticket in survivors] == ["entry-other"]


@pytest.mark.asyncio
async def test_async_remove_entry_discards_the_ticket_it_cannot_name() -> None:
    """Removal must also drop the create-path ticket that names no entry.

    An ``entry_promised`` ticket carries ``entry_id is None`` on purpose: the
    flow that staged it could not know the id yet, and
    ``_async_discard_cleanup_ticket_for_flow`` deliberately keeps it so a first
    ``async_setup_entry`` that raised ``ConfigEntryNotReady`` can still claim it
    on the retry. When that setup never succeeds and the user removes the entry,
    the entry-id drain cannot see the ticket at all -- it names no entry -- so it
    would sit in ``hass.data`` for the rest of the process lifetime. Rule 2 of
    ``_async_claim_container_cleanup_ticket`` then hands it to the *next* entry
    the user creates for the same account, which acks a foreign login container
    and deletes credential copies it never imported.
    """

    from custom_components.googlefindmy import config_flow
    from custom_components.googlefindmy.config_flow import (
        PENDING_CONTAINER_CLEANUP_KEY,
    )
    from custom_components.googlefindmy.const import CONTAINER_TOKEN_PORT

    entry = make_config_entry(
        entry_id="entry-remove", unique_id="user@example.com", title="Find My Entry"
    )
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    _coordinator, _token_cache, _filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    # Create path: no entry, so no entry id and no durability watermark.
    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-create",
        unique_id=entry.unique_id,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code="pair-code",
                delete_token="delete-token",
            )
        ),
        entry=None,
    )
    # ... and the flow promised an entry, which is what kept the ticket alive
    # across its own removal.
    assert config_flow._async_mark_cleanup_ticket_entry_promised(hass, "flow-create")

    staged = hass.data[DOMAIN][PENDING_CONTAINER_CLEANUP_KEY]
    assert len(staged) == 1
    assert staged[0].entry_id is None and staged[0].entry_promised is True

    await integration.async_remove_entry(hass, entry)

    assert PENDING_CONTAINER_CLEANUP_KEY not in hass.data[DOMAIN]

    # The decisive consequence: a freshly created entry for the same account
    # inherits nothing.
    assert (
        config_flow._async_claim_container_cleanup_ticket(
            hass, unique_id="user@example.com", entry_id="entry-new"
        )
        is None
    )


@pytest.mark.asyncio
async def test_async_remove_entry_leaves_a_foreign_accounts_ticket_alone() -> None:
    """The claim-based second pass must not reach across accounts.

    Counterpart to the test above and the reason the removal keeps using the
    account correlation instead of a blanket sweep: a concurrent flow for a
    *different* account holds an equally uncorrelated ticket, and discarding it
    would leave that flow's watched bundle undeleted and its container un-acked.
    """

    from custom_components.googlefindmy import config_flow
    from custom_components.googlefindmy.config_flow import (
        PENDING_CONTAINER_CLEANUP_KEY,
    )
    from custom_components.googlefindmy.const import CONTAINER_TOKEN_PORT

    entry = make_config_entry(
        entry_id="entry-remove", unique_id="user@example.com", title="Find My Entry"
    )
    entry.options[OPT_DELETE_CACHES_ON_REMOVE] = False
    _coordinator, _token_cache, _filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-other-account",
        unique_id="other@example.com",
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code="pair-code",
                delete_token="delete-token",
            )
        ),
        entry=None,
    )

    await integration.async_remove_entry(hass, entry)

    survivors = hass.data[DOMAIN][PENDING_CONTAINER_CLEANUP_KEY]
    assert [ticket.flow_id for ticket in survivors] == ["flow-other-account"]

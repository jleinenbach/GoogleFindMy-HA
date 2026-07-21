# tests/test_discovery_runtime.py
"""Tests for the discovery runtime helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
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

    def __init__(
        self, entry: Any | None = None, *, allow_missing_entry: bool = False
    ) -> None:
        self.data: dict[str, Any] = {}
        runtime_owner = entry
        if runtime_owner is None and not allow_missing_entry:
            runtime_owner = config_entry_with_cloud_runtime()
        self._entry = runtime_owner
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: (
                [runtime_owner]
                if domain == DOMAIN and runtime_owner is not None
                else []
            )
        )
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )
        self.bus = SimpleNamespace(async_listen_once=lambda event, cb: lambda: None)

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


async def _settle() -> None:
    """Yield until queued discovery tasks *and* their done callbacks have run.

    A single ``sleep(0)`` only lets the task body run; the ``add_done_callback``
    hooks are then scheduled with ``call_soon`` and need a further loop pass.
    """

    for _ in range(4):
        await asyncio.sleep(0)


def _patch_watcher_environment(
    monkeypatch: pytest.MonkeyPatch, trigger: Callable[..., Any]
) -> None:
    """Isolate a watcher from timers, entry lookups and translations.

    Only the discovery trigger differs between the watcher regression tests;
    the rest is the same inert scaffolding, so it lives here once.
    """

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", trigger)
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)
    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )


@pytest.mark.asyncio
async def test_secrets_watcher_triggers_new_discovery(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """Writing a new secrets.json bundle should trigger discovery."""

    hass = _FakeHass()
    triggered: list[dict[str, Any]] = []

    async def _fake_trigger(hass_obj, **kwargs):
        triggered.append(kwargs)
        return True

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _fake_trigger)
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
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
    await _exercise()


@pytest.mark.asyncio
async def test_secrets_watcher_updates_existing_entry(
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
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
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
    await _exercise()


class _OptionsHass:
    """Hass stub whose single entry exposes a mutable options mapping.

    Used to prove that changing ``SECRETS_EXTRA_WATCH_PATHS`` at runtime and
    calling ``async_refresh_watch_paths`` re-evaluates the watcher paths without
    a restart.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._entry = SimpleNamespace(options={})
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: [self._entry] if domain == DOMAIN else []
        )
        self.config = SimpleNamespace(
            language="en", components=set(), top_level_components=set()
        )
        self.bus = SimpleNamespace(async_listen_once=lambda event, cb: lambda: None)

    def set_extra_paths(self, paths: list[str]) -> None:
        from custom_components.googlefindmy.const import SECRETS_EXTRA_WATCH_PATHS

        self._entry.options = {SECRETS_EXTRA_WATCH_PATHS: paths}

    async def async_add_executor_job(self, func, *args) -> Any:
        return func(*args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


@pytest.mark.asyncio
async def test_refresh_watch_paths_picks_up_new_option(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F-N2: a runtime option change is applied without a Home Assistant restart."""

    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    async def _exercise() -> None:
        hass = _OptionsHass()
        manager = discovery.DiscoveryManager(hass)
        await manager.async_start()

        new_path = tmp_path / "container" / "secrets.json"
        assert new_path not in manager.watch_paths

        # Simulate the options flow persisting a new extra watch path.
        hass.set_extra_paths([str(new_path)])
        await manager.async_refresh_watch_paths()

        assert new_path in manager.watch_paths
        # The zero-config defaults are preserved alongside the new path.
        assert discovery._default_secrets_path() in manager.watch_paths

        await manager.async_stop()

    await _exercise()


@pytest.mark.asyncio
async def test_cloud_discovery_results_suppress_task_exceptions(
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
        # ``_settle`` (not a single ``sleep(0)``) because the suppressing log
        # lives in the task's done callback, one loop pass behind the failure.
        await _settle()

    await _exercise()

    assert any(
        "Suppressed cloud discovery task exception" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_secrets_watcher_retries_unchanged_bundle_after_task_failure(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """A failed discovery task must leave the bundle retryable.

    The watcher records the bundle signature *before* the discovery task runs.
    Without a failure feedback channel a transient flow-creation error stalls
    the import until the file is rewritten or Home Assistant restarts, because
    every later scan sees the unchanged signature and returns early.
    """

    hass = _FakeHass()
    attempts: list[dict[str, Any]] = []

    async def _flaky_trigger(hass_obj: Any, **kwargs: Any) -> bool:
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("transient flow creation failure")
        return True

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _flaky_trigger)
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    _write_secrets(temp_secrets_path, "retry@example.com", token="aas_et/RETRY")

    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()
    await _settle()

    assert len(attempts) == 1

    # The bundle on disk is untouched; only the failed attempt may re-open it.
    await watcher.async_force_scan()
    await _settle()

    assert len(attempts) == 2
    assert attempts[1]["email"] == attempts[0]["email"] == "retry@example.com"

    # The successful retry re-arms the signature, so an unchanged bundle is not
    # imported over and over again.
    await watcher.async_force_scan()
    await _settle()

    assert len(attempts) == 2

    await watcher.async_stop()


@pytest.mark.asyncio
async def test_stale_failure_does_not_clobber_a_newer_signature(
    temp_secrets_path: Path,
) -> None:
    """A late failure callback must not re-arm an already superseded bundle."""

    watcher = discovery.SecretsJSONWatcher(
        _FakeHass(), path=temp_secrets_path, namespace="test.ns"
    )
    watcher._last_signature = "newer-bundle"

    watcher._invalidate_signature("older-bundle")

    assert watcher._last_signature == "newer-bundle"

    watcher._invalidate_signature("newer-bundle")

    assert watcher._last_signature is None


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_not_a_failure() -> None:
    """Cancellation during a real Home Assistant shutdown must not re-arm."""

    failures: list[str] = []
    cancelled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    cancelled.cancel()

    discovery._handle_discovery_task_result(
        cancelled,
        hass=SimpleNamespace(is_stopping=True),
        on_failure=lambda: failures.append("retry"),
    )

    assert failures == []


@pytest.mark.asyncio
async def test_cancellation_without_shutdown_is_a_failure() -> None:
    """A cancel while Home Assistant keeps running must re-arm the producer.

    Cancellation is not a shutdown marker: an entry unload cancels the very
    same handles. A ``hass`` object that does not expose ``is_stopping`` at all
    (stripped core, test double) must resolve to "not stopping" instead of
    raising or silently swallowing the retry.
    """

    for hass in (SimpleNamespace(is_stopping=False), SimpleNamespace()):
        failures: list[str] = []
        cancelled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        cancelled.cancel()

        discovery._handle_discovery_task_result(
            cancelled, hass=hass, on_failure=lambda: failures.append("retry")
        )

        assert failures == ["retry"]


@pytest.mark.asyncio
async def test_discovery_failure_hook_errors_are_contained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising failure hook must not escape into the event loop."""

    caplog.set_level(logging.DEBUG)
    failing: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    failing.set_exception(RuntimeError("discovery explosion"))

    def _raise() -> None:
        raise ValueError("hook exploded")

    discovery._handle_discovery_task_result(failing, on_failure=_raise)

    assert any(
        "Discovery failure hook raised" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_unobservable_discovery_task_is_treated_as_failure() -> None:
    """A task handle without ``add_done_callback`` counts as a failed attempt."""

    class _BlindHass(_FakeHass):
        def async_create_task(self, coro: Any, name: str | None = None) -> object:
            coro.close()
            return object()

    failures: list[str] = []
    results = discovery._CloudDiscoveryResults(_BlindHass())

    results.append(
        {"email": "blind@example.com"}, on_failure=lambda: failures.append("retry")
    )

    assert failures == ["retry"]


@pytest.mark.asyncio
async def test_unattachable_done_callback_is_treated_as_failure() -> None:
    """If the outcome cannot be observed the attempt must stay retryable."""

    def _reject(_callback: Any) -> None:
        raise RuntimeError("callbacks unsupported")

    class _BrittleHass(_FakeHass):
        def async_create_task(self, coro: Any, name: str | None = None) -> object:
            coro.close()
            return SimpleNamespace(add_done_callback=_reject)

    failures: list[str] = []
    results = discovery._CloudDiscoveryResults(_BrittleHass())

    results.append(
        {"email": "brittle@example.com"}, on_failure=lambda: failures.append("retry")
    )

    assert failures == ["retry"]


@pytest.mark.asyncio
async def test_schedule_falls_back_to_asyncio_create_task() -> None:
    """Without ``hass.async_create_task`` the coroutine still runs and succeeds."""

    results = discovery._CloudDiscoveryResults(SimpleNamespace())  # type: ignore[arg-type]
    failures: list[str] = []
    ran: list[str] = []

    async def _work() -> None:
        ran.append("ran")

    results._schedule(_work(), on_failure=lambda: failures.append("retry"))
    await _settle()

    assert ran == ["ran"]
    assert failures == []


def test_schedule_without_running_loop_is_treated_as_failure() -> None:
    """A skipped schedule (no running loop) must re-arm the producer.

    Intentionally a synchronous test: the branch under test only exists when
    there is no running event loop at all.
    """

    failures: list[str] = []
    results = discovery._CloudDiscoveryResults(SimpleNamespace())  # type: ignore[arg-type]

    async def _noop() -> None:
        return None

    coro = _noop()
    try:
        results._schedule(coro, on_failure=lambda: failures.append("retry"))
    finally:
        coro.close()

    assert failures == ["retry"]


@pytest.mark.asyncio
async def test_entry_unload_cancellation_keeps_bundle_retryable(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """An entry unload/reload must not silently consume the bundle signature.

    Production path: ``_cleanup_cloud_discovery_runtime`` cancels the queued
    discovery handles on *every* entry unload, and an unload happens on every
    reload (an options change, a reauth). The watcher is a Home Assistant
    instance singleton and survives that reload, so reading the cancel as
    "shutdown, no failure" would leave ``_last_signature`` armed on a bundle
    that was never imported -- every later scan returns early and the bundle is
    lost until the file is rewritten or Home Assistant restarts.
    """

    hass = _FakeHass()
    attempts: list[dict[str, Any]] = []
    release = asyncio.Event()

    async def _hanging_trigger(_hass: Any, **kwargs: Any) -> bool:
        attempts.append(kwargs)
        await release.wait()
        return True

    _patch_watcher_environment(monkeypatch, _hanging_trigger)

    _write_secrets(temp_secrets_path, "unload@example.com", token="aas_et/UNLOAD")

    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()
    await _settle()

    assert len(attempts) == 1
    assert watcher._last_signature is not None

    # Home Assistant keeps running; only the config entry is unloaded.
    assert getattr(hass, "is_stopping", False) is False
    integration._cleanup_cloud_discovery_runtime(hass._entry.runtime_data)
    await _settle()

    assert watcher._last_signature is None

    await watcher.async_force_scan()
    await _settle()

    assert len(attempts) == 2
    assert watcher._last_signature is not None

    # Same cancellation, but now Home Assistant really is shutting down: the
    # signature must stay armed instead of scheduling work on a dying core.
    hass.is_stopping = True
    integration._cleanup_cloud_discovery_runtime(hass._entry.runtime_data)
    await _settle()

    assert watcher._last_signature is not None

    await watcher.async_force_scan()
    await _settle()

    assert len(attempts) == 2

    release.set()
    await _settle()
    await watcher.async_stop()


@pytest.mark.asyncio
async def test_permanent_failure_stops_after_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
    temp_secrets_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deterministically failing bundle must stop retrying (and copying).

    Production path: every attempt appends another full payload -- OAuth token
    and complete secrets bundle included -- to ``runtime.results`` and can emit
    another warning. Unbounded retries therefore grow credential copies in
    memory and flood the log at scan rate (every 30 s) for as long as the entry
    lives. After the budget is spent the signature stays armed, and rewriting
    the bundle buys a fresh budget.
    """

    caplog.set_level(logging.WARNING)
    hass = _FakeHass()
    attempts: list[dict[str, Any]] = []

    async def _always_failing(_hass: Any, **kwargs: Any) -> bool:
        attempts.append(kwargs)
        raise RuntimeError("permanent flow creation failure")

    _patch_watcher_environment(monkeypatch, _always_failing)

    _write_secrets(temp_secrets_path, "budget@example.com", token="aas_et/BUDGET")

    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()
    await _settle()

    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    expected_attempts = discovery._MAX_SECRETS_RETRY_ATTEMPTS + 1
    assert len(attempts) == expected_attempts

    runtime = hass._entry.runtime_data.cloud_discovery
    assert len(runtime.results) == expected_attempts

    gave_up = [
        record
        for record in caplog.records
        if "gave up on the current bundle" in record.getMessage()
    ]
    assert len(gave_up) == 1

    # Negative path exit: a changed bundle buys a *full* new budget, not just a
    # single extra attempt.
    _write_secrets(temp_secrets_path, "budget@example.com", token="aas_et/FRESH")
    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    assert len(attempts) == 2 * expected_attempts

    await watcher.async_stop()


@pytest.mark.asyncio
async def test_vanished_bundle_resets_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """Removing and restoring the same bundle must buy a fresh retry budget.

    The exhausted budget is tied to the armed signature, so forgetting the
    signature (bundle gone, watch paths changed, watcher stopped) has to forget
    the budget with it. Otherwise a byte-identical bundle that comes back would
    inherit the previous life's exhausted counter and never be retried.
    """

    hass = _FakeHass()
    attempts: list[dict[str, Any]] = []

    async def _always_failing(_hass: Any, **kwargs: Any) -> bool:
        attempts.append(kwargs)
        raise RuntimeError("permanent flow creation failure")

    _patch_watcher_environment(monkeypatch, _always_failing)

    _write_secrets(temp_secrets_path, "vanish@example.com", token="aas_et/VANISH")

    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()
    await _settle()

    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    expected_attempts = discovery._MAX_SECRETS_RETRY_ATTEMPTS + 1
    assert len(attempts) == expected_attempts

    temp_secrets_path.unlink()
    await watcher.async_force_scan()
    await _settle()

    # Byte-identical content, hence the very same signature as before. The
    # restored bundle must get a *full* budget again; a single extra attempt
    # would also happen with a stale counter, so assert the whole budget.
    _write_secrets(temp_secrets_path, "vanish@example.com", token="aas_et/VANISH")
    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    assert len(attempts) == 2 * expected_attempts

    await watcher.async_stop()


@pytest.mark.asyncio
async def test_returning_bundle_gets_a_full_budget_after_a_successful_other(
    monkeypatch: pytest.MonkeyPatch,
    temp_secrets_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exhausted budget must not survive another bundle being armed.

    The budget belongs to the signature that is currently armed. If a second
    bundle is armed in between and *succeeds*, nothing ever re-bases the
    counter, so a returning first bundle would inherit the spent one: a single
    attempt instead of a full budget, plus a give-up warning claiming attempts
    that never happened in this life. This is not exotic -- two watched paths
    plus the delete-after-import hook routinely make an older bundle win again.
    """

    caplog.set_level(logging.WARNING)
    hass = _FakeHass()
    attempts: list[str] = []

    async def _fails_only_for_the_first_account(_hass: Any, **kwargs: Any) -> bool:
        email = str(kwargs.get("email") or "")
        attempts.append(email)
        if email == "returns@example.com":
            raise RuntimeError("permanent flow creation failure")
        return True

    _patch_watcher_environment(monkeypatch, _fails_only_for_the_first_account)

    _write_secrets(temp_secrets_path, "returns@example.com", token="aas_et/RETURN")

    watcher = discovery.SecretsJSONWatcher(
        hass, path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()
    await _settle()

    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    budget = discovery._MAX_SECRETS_RETRY_ATTEMPTS + 1
    assert attempts.count("returns@example.com") == budget

    # A different bundle is armed and imports cleanly, so its own failure
    # callback never runs and never touches the counter.
    _write_secrets(temp_secrets_path, "other@example.com", token="aas_et/OTHER")
    for _ in range(3):
        await watcher.async_force_scan()
        await _settle()
    assert attempts.count("other@example.com") == 1

    # The first bundle returns byte-identically, hence with its old signature.
    caplog.clear()
    _write_secrets(temp_secrets_path, "returns@example.com", token="aas_et/RETURN")
    for _ in range(10):
        await watcher.async_force_scan()
        await _settle()

    # A full new budget, not the single attempt a stale counter would allow.
    assert attempts.count("returns@example.com") == 2 * budget
    gave_up = [
        record
        for record in caplog.records
        if "gave up on the current bundle" in record.getMessage()
    ]
    assert len(gave_up) == 1

    await watcher.async_stop()


@pytest.mark.asyncio
async def test_queueing_error_keeps_bundle_retryable(
    monkeypatch: pytest.MonkeyPatch, temp_secrets_path: Path
) -> None:
    """A raising results container must not consume the bundle signature."""

    queued: list[dict[str, Any]] = []

    def _rejecting_append(
        self: Any,
        item: Any,
        *,
        trigger: bool = True,
        on_failure: Any = None,
    ) -> None:
        queued.append(dict(item))
        raise RuntimeError("results container rejected the payload")

    monkeypatch.setattr(discovery._CloudDiscoveryResults, "append", _rejecting_append)
    monkeypatch.setattr(discovery, "async_track_time_interval", lambda *_: lambda: None)
    monkeypatch.setattr(discovery.cf, "_find_entry_by_email", lambda *_: None)

    async def _fake_translations(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(
        discovery.translation, "async_get_translations", _fake_translations
    )

    _write_secrets(temp_secrets_path, "queue@example.com", token="aas_et/QUEUE")

    watcher = discovery.SecretsJSONWatcher(
        _FakeHass(), path=temp_secrets_path, namespace="test.ns"
    )
    await watcher.async_start()

    assert len(queued) == 1

    await watcher.async_force_scan()

    assert len(queued) == 2

    await watcher.async_stop()


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


def test_cloud_discovery_runtime_handles_entryless_startup() -> None:
    """Runtime helper should initialize when no config entries exist."""

    hass = _FakeHass(allow_missing_entry=True)

    runtime = discovery._cloud_discovery_runtime(hass)

    assert isinstance(runtime, CloudDiscoveryRuntime)
    assert runtime.results._entry is None
    assert "cloud_discovery_runtime_owner" in hass.data.get(DOMAIN, {})


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

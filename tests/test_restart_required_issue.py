"""Tests for the global "restart required" Repairs watchdog (AP6).

Covers detection (on-disk vs. running version), self-healing deletion, read-error
resilience, single-timer idempotency across multiple ``async_setup`` calls, the
awaitable guard for ``async_call_later``, and last-entry teardown.

See ``PLAN_GFM_RESTART_REQUIRED_REPAIR.md`` AP6 (cases A-E) and the Codex-P1
legend #1-#6.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import custom_components.googlefindmy as gfm
from custom_components.googlefindmy.const import (
    DOMAIN,
    INTEGRATION_VERSION,
    ISSUE_RESTART_REQUIRED_KEY,
    TRANSLATION_KEY_RESTART_REQUIRED,
)

# pytest-asyncio runs with asyncio_mode = "auto" (see pyproject.toml / tests/AGENTS.md),
# so coroutine tests are detected automatically; sync tests stay unmarked.


class _Hass:
    """Minimal Home Assistant double for the restart watchdog."""

    def __init__(self) -> None:
        self.data: dict[Any, Any] = {}
        self.created_tasks: list[str | None] = []

    async def async_add_executor_job(self, func, *args):  # type: ignore[no-untyped-def]
        return func(*args)

    def async_create_task(self, coro, name=None):  # type: ignore[no-untyped-def]
        # Consume the coroutine so the event loop does not warn about it.
        self.created_tasks.append(name)
        coro.close()


def _active_hass() -> _Hass:
    """Return a hass double whose restart watchdog is registered.

    That is the real precondition under which ``_async_check_restart_required``
    ever runs: it is only ever scheduled by the registered timers. Tests that
    exercise the *create* path must establish it, because the coroutine now
    refuses to (re)create the issue once the watchdog has been torn down by a
    concurrent last-entry unload (Codex-P1 class: tick firing after unload).
    """

    hass = _Hass()
    gfm._domain_data(hass)["restart_check_registered"] = True
    return hass


@pytest.fixture(name="issue_calls")
def _issue_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Capture issue-registry create/delete calls."""

    created: list[dict[str, Any]] = []
    deleted: list[str] = []

    def _create(hass, domain, issue_id, **kwargs):  # type: ignore[no-untyped-def]
        created.append({"domain": domain, "issue_id": issue_id, **kwargs})

    def _delete(hass, domain, issue_id):  # type: ignore[no-untyped-def]
        deleted.append(issue_id)

    monkeypatch.setattr(gfm.ir, "async_create_issue", _create)
    monkeypatch.setattr(gfm.ir, "async_delete_issue", _delete)
    return {"created": created, "deleted": deleted}


# --- Fall A: disk != running -> create issue --------------------------------


async def test_check_creates_issue_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _active_hass()

    async def _fake_installed(_hass: Any) -> str:
        return "9.9.9-disk"

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    await gfm._async_check_restart_required(hass)

    assert len(issue_calls["created"]) == 1
    call = issue_calls["created"][0]
    assert call["issue_id"] == ISSUE_RESTART_REQUIRED_KEY
    assert call["domain"] == DOMAIN
    assert call["is_fixable"] is False
    assert call["translation_key"] == TRANSLATION_KEY_RESTART_REQUIRED
    assert call["translation_placeholders"] == {
        "running_version": INTEGRATION_VERSION,
        "installed_version": "9.9.9-disk",
    }
    assert issue_calls["deleted"] == []


# --- Race: check task finishing after last-entry teardown -------------------


async def test_check_skips_create_after_teardown_race(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    """A mismatch detected after the watchdog was torn down must not resurrect
    the issue (Codex-P1 class: tick firing after unload).

    The timer may fire and spawn the check task an instant before the last-entry
    unload pops ``restart_check_registered``. When the task then resumes past its
    executor read, no timer is left to ever clear a freshly created issue -- so
    creation must be suppressed. ``_Hass`` starts without the flag, modelling the
    post-teardown bucket; neither create nor delete may be invoked.
    """

    hass = _Hass()

    async def _fake_installed(_hass: Any) -> str:
        return "9.9.9-disk"

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    await gfm._async_check_restart_required(hass)

    assert issue_calls["created"] == []
    assert issue_calls["deleted"] == []


# --- Fall B: equal versions -> delete issue (self-healing symmetry) ---------


async def test_check_deletes_issue_when_versions_match(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _Hass()

    async def _fake_installed(_hass: Any) -> str:
        return INTEGRATION_VERSION

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    await gfm._async_check_restart_required(hass)

    assert issue_calls["created"] == []
    assert issue_calls["deleted"] == [ISSUE_RESTART_REQUIRED_KEY]


# --- Fall C: read error -> None -> delete, no create, no crash --------------


async def test_check_clears_issue_on_read_error(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _Hass()

    async def _fake_installed(_hass: Any) -> None:
        return None

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    await gfm._async_check_restart_required(hass)

    assert issue_calls["created"] == []
    assert issue_calls["deleted"] == [ISSUE_RESTART_REQUIRED_KEY]


# --- Fall C2: callback never raises into HA (Codex-P1 #4) -------------------


async def test_check_never_raises_into_ha(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _active_hass()

    async def _fake_installed(_hass: Any) -> str:
        return "9.9.9-disk"

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("issue registry exploded")

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    monkeypatch.setattr(gfm.ir, "async_create_issue", _boom)
    # Must not propagate.
    await gfm._async_check_restart_required(hass)


# --- executor read path (Codex-P1 #3: no blocking IO in the loop) -----------


async def test_check_uses_executor_read_path(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _active_hass()
    monkeypatch.setattr(gfm, "_read_installed_version_sync", lambda: "9.9.9-disk")
    await gfm._async_check_restart_required(hass)
    assert len(issue_calls["created"]) == 1


# --- sync reader coverage ---------------------------------------------------


def test_read_installed_version_sync_reads_manifest() -> None:
    # The real manifest on disk carries INTEGRATION_VERSION after the bump.
    assert gfm._read_installed_version_sync() == INTEGRATION_VERSION


def test_read_installed_version_sync_handles_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("broken json")

    monkeypatch.setattr(gfm.json, "load", _boom)
    assert gfm._read_installed_version_sync() is None


def test_read_installed_version_sync_handles_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gfm.json, "load", lambda *a, **k: {"no_version": True})
    assert gfm._read_installed_version_sync() is None


# --- severity resolution (PA-F6 defensive getattr) --------------------------


async def test_create_passes_resolved_issue_severity(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _active_hass()

    async def _fake_installed(_hass: Any) -> str:
        return "9.9.9-disk"

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    monkeypatch.setattr(
        gfm.ir, "IssueSeverity", types.SimpleNamespace(WARNING="warning_level")
    )
    await gfm._async_check_restart_required(hass)
    assert issue_calls["created"][0]["severity"] == "warning_level"


async def test_create_severity_fallback_without_enum(
    monkeypatch: pytest.MonkeyPatch, issue_calls: dict[str, list[Any]]
) -> None:
    hass = _active_hass()

    async def _fake_installed(_hass: Any) -> str:
        return "9.9.9-disk"

    monkeypatch.setattr(gfm, "_async_read_installed_version", _fake_installed)
    monkeypatch.delattr(gfm.ir, "IssueSeverity", raising=False)
    monkeypatch.setattr(gfm.ir, "WARNING", "fallback_warn", raising=False)
    await gfm._async_check_restart_required(hass)
    assert issue_calls["created"][0]["severity"] == "fallback_warn"


# --- scheduled callback schedules the check (timer body, Codex-P1 #3) -------


async def test_scheduled_callback_schedules_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = _Hass()
    captured: dict[str, Any] = {}

    def _fake_later(_hass: Any, _delay: Any, cb: Any) -> Any:
        captured["cb"] = cb
        return lambda: None

    def _fake_interval(_hass: Any, cb: Any, _interval: Any) -> Any:
        return lambda: None

    monkeypatch.setattr(gfm, "async_call_later", _fake_later)
    monkeypatch.setattr(gfm, "async_track_time_interval", _fake_interval)

    bucket: dict[Any, Any] = {}
    gfm._register_restart_required_check(hass, bucket)

    # Fire the captured timer callback: it must schedule the async check as a task.
    captured["cb"](None)
    assert hass.created_tasks == [f"{gfm.DOMAIN}.restart_required_check"]


# --- Fall D: single timer pair across multiple async_setup calls (P1 #6) ----


async def test_single_timer_across_multiple_setups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = _Hass()
    monkeypatch.setattr(gfm, "_ensure_runtime_imports", lambda: None)

    counts = {"later": 0, "interval": 0}

    def _fake_later(_hass: Any, _delay: Any, _cb: Any) -> Any:
        counts["later"] += 1
        return lambda: None

    def _fake_interval(_hass: Any, _cb: Any, _interval: Any) -> Any:
        counts["interval"] += 1
        return lambda: None

    monkeypatch.setattr(gfm, "async_call_later", _fake_later)
    monkeypatch.setattr(gfm, "async_track_time_interval", _fake_interval)

    bucket = hass.data.setdefault(gfm.DATA_DOMAIN, {})
    bucket["services_registered"] = True  # skip the service-registration branch

    assert await gfm.async_setup(hass, {}) is True
    assert await gfm.async_setup(hass, {}) is True  # second entry / racey re-entry

    assert counts["later"] == 1
    assert counts["interval"] == 1
    assert bucket["restart_check_registered"] is True
    assert callable(bucket["restart_check_initial_unsub"])
    assert callable(bucket["restart_check_unsub"])


# --- awaitable guard for async_call_later (Codex-P1 #5) ---------------------


async def test_register_handles_awaitable_call_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = _Hass()

    async def _await_handle() -> None:
        return None

    def _fake_later(_hass: Any, _delay: Any, _cb: Any) -> Any:
        return _await_handle()

    def _fake_interval(_hass: Any, _cb: Any, _interval: Any) -> Any:
        return lambda: None

    monkeypatch.setattr(gfm, "async_call_later", _fake_later)
    monkeypatch.setattr(gfm, "async_track_time_interval", _fake_interval)

    bucket: dict[Any, Any] = {}
    gfm._register_restart_required_check(hass, bucket)

    # Awaitable path: initial unsub stored as None, the awaitable scheduled.
    assert bucket["restart_check_initial_unsub"] is None
    assert callable(bucket["restart_check_unsub"])
    assert hass.created_tasks  # the awaitable was scheduled as a task


# --- Fall E: last-entry teardown cancels both timers + deletes issue --------


async def test_teardown_cancels_timers_and_deletes_issue(
    issue_calls: dict[str, list[Any]],
) -> None:
    hass = _Hass()
    cancelled = {"initial": False, "interval": False}

    bucket: dict[Any, Any] = {
        "restart_check_registered": True,
        "restart_check_initial_unsub": lambda: cancelled.__setitem__("initial", True),
        "restart_check_unsub": lambda: cancelled.__setitem__("interval", True),
    }

    gfm._teardown_restart_required_check(hass, bucket)

    assert cancelled == {"initial": True, "interval": True}
    assert "restart_check_initial_unsub" not in bucket
    assert "restart_check_unsub" not in bucket
    assert "restart_check_registered" not in bucket
    assert issue_calls["deleted"] == [ISSUE_RESTART_REQUIRED_KEY]


async def test_teardown_is_safe_without_registration(
    issue_calls: dict[str, list[Any]],
) -> None:
    """Teardown on a bucket that never registered must not raise."""

    hass = _Hass()
    bucket: dict[Any, Any] = {}
    gfm._teardown_restart_required_check(hass, bucket)
    # Issue delete is still attempted (idempotent / no-op on the HA side).
    assert issue_calls["deleted"] == [ISSUE_RESTART_REQUIRED_KEY]

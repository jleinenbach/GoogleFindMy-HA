# tests/test_owner_key_version_repairs.py
"""AP9b: Repairs issues for owner-key version gaps (issue #246, L-4, L-17).

A tracker that needs an owner-key version that Spot does not serve and the
secrets bundle cannot supply gets one Repairs issue per entry and version. These
tests pin the full life cycle:

* ``note_decrypt_failure`` raises exactly one issue with the three version
  placeholders and the translation key that names the remedy;
* the issue is deleted at the end of the poll cycle in which the version was
  resolved, and after a background (push) success;
* the push path heals ``owner_key_version_unavailable`` only when a version was
  resolved and no other gap version of the account is still open;
* the coordinator scope equals the token cache scope (AE-8);
* unloading the parent entry drops the process-wide register (PB-5).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir

import custom_components.googlefindmy as integration
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.const import (
    DOMAIN,
    ISSUE_OWNER_KEY_VERSION_LOGIN_KEY,
    ISSUE_OWNER_KEY_VERSION_VAULT_KEY,
    OWNER_KEY_VERSION_ISSUE_PREFIX,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.stats import CryptoStatus
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    OwnerKeyVersionUnavailableError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    open_owner_key_version_gaps,
    owner_key_scope,
    record_owner_key_version_gap,
    record_owner_key_version_resolved,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.polling_mixin_stub import PollingStub

_ENTRY_ID = "entry-id"


class _Scope:
    """Cache double that carries only the entry id the register scopes by."""

    def __init__(self, entry_id: str = _ENTRY_ID) -> None:
        self.entry_id = entry_id


class _Hass:
    """Plain HA double; the test issue registry attaches itself to it."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.loop = MagicMock()

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        coro.close()  # pragma: no cover - the stub path schedules nothing


def _gap_error(
    version: int = 2, *, remedy: str = "login", bundle: tuple[int, ...] = (1, 3)
) -> OwnerKeyVersionUnavailableError:
    return OwnerKeyVersionUnavailableError(
        required_version=version,
        current_version=1,
        bundle_versions=bundle,
        remedy=remedy,  # type: ignore[arg-type]
    )


def _make_stub() -> PollingStub:
    stub = PollingStub(
        hass=_Hass(),
        config_entry=make_config_entry(entry_id=_ENTRY_ID, title="Test Entry"),
    )
    stub._consecutive_decrypt_failures = 0
    stub._last_decrypt_reauth_monotonic = None
    stub._last_decrypt_error = None
    stub._monotonic = lambda: 1_000_000.0
    stub._set_auth_state = MagicMock()
    return stub


def _owner_key_issues(hass: Any) -> dict[str, dict[str, Any]]:
    """Return this domain's owner-key version issues from the test registry."""
    issues = getattr(ir.async_get(hass), "_issues")
    return {
        issue_id: dict(data)
        for (domain, issue_id), data in issues.items()
        if domain == DOMAIN and issue_id.startswith(OWNER_KEY_VERSION_ISSUE_PREFIX)
    }


def _issue_id(version: int, entry_id: str = _ENTRY_ID) -> str:
    return f"{OWNER_KEY_VERSION_ISSUE_PREFIX}_{entry_id}_v{version}"


def _fail(stub: PollingStub, version: int, **kwargs: Any) -> None:
    """Enter the gap like ``decrypt_locations`` does, then report the failure."""
    record_owner_key_version_gap(_Scope(), version)
    assert (
        stub.note_decrypt_failure(
            stale=False, error=_gap_error(version, **kwargs), device="tag"
        )
        is False
    )


# ---------------------------------------------------------------------------
# Creation in note_decrypt_failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remedy", "translation_key"),
    [
        ("login", ISSUE_OWNER_KEY_VERSION_LOGIN_KEY),
        ("vault", ISSUE_OWNER_KEY_VERSION_VAULT_KEY),
    ],
)
def test_gap_failure_creates_one_issue(remedy: str, translation_key: str) -> None:
    """L-4: one issue per entry and version, three version placeholders, remedy text."""
    stub = _make_stub()
    _fail(stub, 2, remedy=remedy)
    _fail(stub, 2, remedy=remedy)

    issues = _owner_key_issues(stub.hass)
    assert list(issues) == [_issue_id(2)]
    issue = issues[_issue_id(2)]
    assert issue["translation_key"] == translation_key
    assert issue["is_fixable"] is False
    assert issue["is_persistent"] is False
    assert issue["translation_placeholders"] == {
        "required_version": "2",
        "current_version": "1",
        "bundle_versions": "1, 3",
    }
    assert stub._consecutive_decrypt_failures == 0
    stub._set_auth_state.assert_not_called()


def test_gap_failure_with_empty_bundle_uses_dash() -> None:
    """An empty bundle list renders as ``-``, never as an empty placeholder."""
    stub = _make_stub()
    _fail(stub, 2, bundle=())

    placeholders = _owner_key_issues(stub.hass)[_issue_id(2)][
        "translation_placeholders"
    ]
    assert placeholders["bundle_versions"] == "-"


def test_gap_failure_without_config_entry_creates_no_issue() -> None:
    """Robustness: without a config entry there is no scope and no issue."""
    stub = _make_stub()
    stub.config_entry = None
    _fail(stub, 2)

    assert _owner_key_issues(stub.hass) == {}
    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE


def test_registry_failure_never_breaks_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising registry is logged by type; status and counter stay correct."""

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("registry down")

    monkeypatch.setattr(ir, "async_create_issue", _boom)
    stub = _make_stub()
    _fail(stub, 2)

    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert stub._consecutive_decrypt_failures == 0


# ---------------------------------------------------------------------------
# Deletion and healing in the push path
# ---------------------------------------------------------------------------


def test_background_success_deletes_issue() -> None:
    """A6: a push success retires the issue of every resolved version."""
    stub = _make_stub()
    _fail(stub, 2)
    assert _issue_id(2) in _owner_key_issues(stub.hass)

    record_owner_key_version_resolved(_Scope(), 2)
    stub.note_background_decrypt_success()

    assert _owner_key_issues(stub.hass) == {}


def test_background_success_heals_owner_key_version_status() -> None:
    """F8: resolved version and no open gap: status and error class go to OK."""
    stub = _make_stub()
    _fail(stub, 2)
    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE

    record_owner_key_version_resolved(_Scope(), 2)
    stub.note_background_decrypt_success()

    assert stub.crypto_status.state == CryptoStatus.OK
    assert stub._last_decrypt_error is None


def test_background_success_without_resolved_version_keeps_status() -> None:
    """F8: no resolved version, no open gap: the status stays.

    The gap is reported without entering the register, so ``resolved`` is the
    only condition that decides; a heal without it would turn this red.
    """
    stub = _make_stub()
    assert stub.note_decrypt_failure(stale=False, error=_gap_error(), device="tag") is (
        False
    )
    assert open_owner_key_version_gaps(_ENTRY_ID) == ()
    before = stub._last_decrypt_error

    stub.note_background_decrypt_success()

    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert stub._last_decrypt_error == before
    assert _issue_id(2) in _owner_key_issues(stub.hass)


def test_background_success_keeps_status_while_other_gap_open() -> None:
    """N6: two gap versions, one heals: its issue goes, the status stays."""
    stub = _make_stub()
    _fail(stub, 2)
    _fail(stub, 3)
    assert set(_owner_key_issues(stub.hass)) == {_issue_id(2), _issue_id(3)}
    before = stub._last_decrypt_error

    record_owner_key_version_resolved(_Scope(), 2)
    stub.note_background_decrypt_success()

    assert set(_owner_key_issues(stub.hass)) == {_issue_id(3)}
    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert stub._last_decrypt_error == before


# ---------------------------------------------------------------------------
# Deletion at the end of a real poll cycle (F1)
# ---------------------------------------------------------------------------


class _DummyCache:
    """Minimal cache stub for the real coordinator build."""

    async def async_get_cached_value(self, _key: str) -> None:  # pragma: no cover
        return None

    async def async_set_cached_value(self, _key: str, _value: Any) -> None:
        return None  # pragma: no cover


class _CoordinatorHass:
    """HA double exposing the loop and task helper the coordinator needs."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict[str, Any] = {}

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        return self.loop.create_task(coro)


class _TwoCycleAPI:
    """Cycle 1: enter the gap for version 2 and raise; cycle 2: resolve it."""

    def __init__(self) -> None:
        self.resolved = False

    async def async_get_device_location(
        self, _device_id: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        if not self.resolved:
            record_owner_key_version_gap(_Scope(), 2)
            raise _gap_error(2)
        record_owner_key_version_resolved(_Scope(), 2)
        return {"latitude": 1.0, "longitude": 2.0, "last_seen": 100}


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch, loop: asyncio.AbstractEventLoop, api: Any
) -> GoogleFindMyCoordinator:
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    coordinator = GoogleFindMyCoordinator(_CoordinatorHass(loop), cache=_DummyCache())
    coordinator.config_entry = make_config_entry(
        entry_id=_ENTRY_ID, options={}, data={}, title="Test Entry"
    )
    coordinator.api = api
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._set_auth_state = MagicMock(return_value=None)
    coordinator._request_poll_reauth = MagicMock(return_value=None)
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data: Any) -> None:
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data
    return coordinator


async def _cycle(
    coordinator: GoogleFindMyCoordinator, devices: list[dict[str, str]]
) -> None:
    coordinator._last_device_list = list(devices)
    try:
        await coordinator._async_start_poll_cycle(devices, force=True)
    finally:
        # The cycle schedules a debounced stats save; cancel it so no task
        # lingers past the test (mirrors test_coordinator_locate_request_clock).
        save_task = coordinator._stats_save_task
        if save_task is not None:
            save_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await save_task
            coordinator._stats_save_task = None


@pytest.mark.asyncio
async def test_issue_deleted_after_spot_serves_required_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: cycle 1 raises the issue, cycle 2 resolves N and deletes it."""
    api = _TwoCycleAPI()
    coordinator = _make_coordinator(monkeypatch, asyncio.get_running_loop(), api)
    devices = [{"id": "dev-gap", "name": "New Tag"}]

    await _cycle(coordinator, devices)
    assert list(_owner_key_issues(coordinator.hass)) == [_issue_id(2)]
    assert coordinator.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE

    api.resolved = True
    await _cycle(coordinator, devices)
    assert _owner_key_issues(coordinator.hass) == {}
    assert coordinator.crypto_status.state == CryptoStatus.OK


# ---------------------------------------------------------------------------
# Scope (AE-8) and unload (PB-5)
# ---------------------------------------------------------------------------


def test_scope_matches_entry_id() -> None:
    """AE-8: the coordinator scope equals the scope of the entry's token cache."""
    entry = make_config_entry(entry_id="entry-scope", title="Scope Entry")
    stub = PollingStub(hass=_Hass(), config_entry=entry)
    cache = TokenCache(SimpleNamespace(loop=MagicMock()), entry.entry_id)

    assert stub._owner_key_version_scope() == "entry-scope"
    assert owner_key_scope(cache) == stub._owner_key_version_scope()


@pytest.mark.asyncio
async def test_unload_parent_forgets_owner_key_refresh_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PB-5: unloading the parent entry drops its owner-key refresh register."""
    entry = make_config_entry(entry_id="entry-unload", title="Unload Entry")
    record_owner_key_version_gap(_Scope("entry-unload"), 2)
    record_owner_key_version_gap(_Scope("entry-other"), 2)
    monkeypatch.setattr(
        integration, "_async_unload_parent_entry", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        integration, "discard_pending_entry_reload", lambda *_args: None
    )

    assert await integration.async_unload_entry(SimpleNamespace(data={}), entry)

    assert open_owner_key_version_gaps("entry-unload") == ()
    assert open_owner_key_version_gaps("entry-other") == (2,)

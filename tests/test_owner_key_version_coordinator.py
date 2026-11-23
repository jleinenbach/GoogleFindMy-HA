# tests/test_owner_key_version_coordinator.py
"""The owner-key version gap passes the coordinator without any reauth.

Issue #246: ``OwnerKeyVersionUnavailableError`` is a ``DecryptionError`` subclass,
but unlike every other subclass except the own-report class it is per tracker and
possibly permanent. These tests pin the coordinator side:

* ``note_decrypt_failure`` classifies it from the error itself, sets
  ``owner_key_version_unavailable`` and never touches the reauth counter;
* ``_decrypt_failure_is_account_wide`` never calls it account-wide;
* the cycle flag ``cycle_had_owner_key_version_gap`` reaches the OK gate;
* a gap cycle marks the poll failed but never fails the coordinator update, so
  a permanent gap cannot make every entity of the account unavailable;
* error text and diagnostics carry numbers only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.stats import CryptoStatus
from custom_components.googlefindmy.coordinator.polling import _MAX_DECRYPT_FAILURES
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    OwnerKeyVersionUnavailableError,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.owner_key_vault_fixtures import make_vault_world
from tests.helpers.polling_mixin_stub import PollingStub
from tests.test_owner_key_version_fallback import _install, _seeded_cache

# These tests examine what an automatic location request does, so every
# tracker in them is turned on; without a stored choice none would be polled.
pytestmark = pytest.mark.usefixtures("automatic_polling_on")

_HEX_WINDOW = 16


def _gap_error(**overrides: Any) -> OwnerKeyVersionUnavailableError:
    kwargs: dict[str, Any] = {
        "required_version": 2,
        "current_version": 1,
        "bundle_versions": (),
        "remedy": "login",
    }
    kwargs.update(overrides)
    return OwnerKeyVersionUnavailableError(**kwargs)


def _make_stub() -> PollingStub:
    """Return a PollingStub seeded with the decrypt-escalation state fields."""
    stub = PollingStub()
    stub._consecutive_decrypt_failures = 0
    stub._last_decrypt_reauth_monotonic = None
    stub._last_decrypt_error = None
    stub._monotonic = lambda: 1_000_000.0
    stub._set_auth_state = MagicMock()
    return stub


# ---------------------------------------------------------------------------
# Unit level: note_decrypt_failure, account-wide verdict, flag pass-through
# ---------------------------------------------------------------------------


def test_gap_branch_never_touches_reauth_counter() -> None:
    """The gap sets its own status and never advances the reauth counter.

    Called with ``stale=False`` (the value every catch site passes), past the
    threshold: without the ``isinstance`` branch the counter would count and the
    status would read ``shared_key_invalid``.
    """
    stub = _make_stub()
    for _ in range(_MAX_DECRYPT_FAILURES + 1):
        assert (
            stub.note_decrypt_failure(stale=False, error=_gap_error(), device="tag")
            is False
        )

    assert stub._consecutive_decrypt_failures == 0
    stub._set_auth_state.assert_not_called()
    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert stub._last_decrypt_error is not None
    assert stub._last_decrypt_error.startswith("OwnerKeyVersionUnavailableError: ")


def test_gap_is_not_account_wide() -> None:
    """Neither with nor without a decrypting sibling is the gap account-wide."""
    stub = _make_stub()
    gap = _gap_error()

    assert stub._decrypt_failure_is_account_wide(gap, False) is False
    assert stub._decrypt_failure_is_account_wide(gap, True) is False
    # The post-loop verdict therefore never escalates and never counts.
    assert (
        stub._resolve_cycle_decrypt_outcome(
            cycle_decrypt_error=gap,
            cycle_had_successful_decrypt=False,
            cycle_had_stale_key=False,
        )
        is False
    )
    assert stub._consecutive_decrypt_failures == 0
    stub._set_auth_state.assert_not_called()


def test_finalize_forwards_gap_flag() -> None:
    """``_finalize_cycle_decrypt_state`` hands the flag to the OK gate."""
    stub = _make_stub()
    stub._set_crypto_status(CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE)

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=None,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=True,
        last_exception=None,
        cycle_had_owner_key_version_gap=True,
    )

    assert stub.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert (cycle_failed, last_exception, reauth_exc) == (True, None, None)

    # Control: the same call without the flag heals to OK, so the assertion
    # above depends on the flag and not on some other gate.
    stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=None,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=False,
        last_exception=None,
    )
    assert stub.crypto_status.state == CryptoStatus.OK


@pytest.mark.asyncio
async def test_error_text_hygiene(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Error text, diagnostic and warning hold numbers only.

    The error comes from the real raise site in ``async_retrieve_identity_key``,
    so a raise site that appended key material would be caught here.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    cache = _seeded_cache(world, None)

    with pytest.raises(OwnerKeyVersionUnavailableError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    err = ei.value

    expected = (
        "Owner key version 2 not available (Spot serves version 1, bundle holds "
        "0 version(s), remedy=login)"
    )
    assert str(err) == expected

    stub = _make_stub()
    with caplog.at_level(logging.DEBUG):
        stub.note_decrypt_failure(stale=False, error=err, device="tag")
    assert stub._last_decrypt_error == f"OwnerKeyVersionUnavailableError: {expected}"

    secrets: list[bytes] = [world.shared_key]
    for mapping in (
        world.owner_keys,
        world.wrapped_owner_keys,
        world.eiks,
        world.encrypted_eiks,
    ):
        secrets.extend(mapping.values())
    windows = {
        blob.hex()[i : i + _HEX_WINDOW]
        for blob in secrets
        for i in range(len(blob.hex()) - _HEX_WINDOW + 1)
    }
    assert windows
    for text in (str(err), repr(err), stub._last_decrypt_error, caplog.text):
        lowered = text.lower()
        assert not any(window in lowered for window in windows)
        assert "b'" not in text
        assert "\\x" not in text


# ---------------------------------------------------------------------------
# Real coordinator: a gap cycle never fails the coordinator update
# ---------------------------------------------------------------------------


class _DummyCache:
    """Minimal cache stub for the real coordinator build."""

    async def async_get_cached_value(self, _key: str) -> None:  # pragma: no cover
        return None

    async def async_set_cached_value(self, _key: str, _value: Any) -> None:
        return None  # pragma: no cover


class _DummyHass:
    """Minimal HA stub exposing the loop and task helper the coordinator needs."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict[str, Any] = {}

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        return self.loop.create_task(coro)


class _GapAPI:
    """Raise the gap for ``dev-gap``, return a decrypted coordinate otherwise."""

    async def async_get_device_location(
        self, device_id: str, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        if device_id == "dev-gap":
            raise _gap_error()
        return {"latitude": 1.0, "longitude": 2.0, "last_seen": 100}


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    devices: list[dict[str, str]],
) -> GoogleFindMyCoordinator:
    """Build a real coordinator (mirrors test_transient_owner_key_propagation)."""
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    coordinator = GoogleFindMyCoordinator(_DummyHass(loop), cache=_DummyCache())
    coordinator.config_entry = make_config_entry(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = _GapAPI()
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)
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


async def _run_cycle(
    monkeypatch: pytest.MonkeyPatch, devices: list[dict[str, str]]
) -> GoogleFindMyCoordinator:
    coordinator = _make_coordinator(monkeypatch, asyncio.get_running_loop(), devices)
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
    return coordinator


def _assert_gap_cycle(coordinator: GoogleFindMyCoordinator) -> None:
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    assert coordinator.last_poll_result == "failed"
    assert coordinator.crypto_status.state == CryptoStatus.OWNER_KEY_VERSION_UNAVAILABLE
    assert coordinator._consecutive_decrypt_failures == 0
    coordinator._request_poll_reauth.assert_not_called()
    # A decrypting sibling may confirm healthy auth (failed=False); the gap
    # itself must never mark auth failed.
    assert not any(
        call.kwargs.get("failed") for call in coordinator._set_auth_state.call_args_list
    )


@pytest.mark.asyncio
async def test_gap_only_cycle_keeps_update_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle with only a gap device fails the poll, not the update."""
    coordinator = await _run_cycle(monkeypatch, [{"id": "dev-gap", "name": "New Tag"}])
    _assert_gap_cycle(coordinator)


@pytest.mark.asyncio
async def test_mixed_gap_and_success_cycle_keeps_update_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap plus a decrypting sibling: update succeeds, the status stays."""
    coordinator = await _run_cycle(
        monkeypatch,
        [{"id": "dev-gap", "name": "New Tag"}, {"id": "dev-ok", "name": "Hub"}],
    )
    _assert_gap_cycle(coordinator)

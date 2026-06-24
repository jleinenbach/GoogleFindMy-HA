# tests/test_transient_owner_key_propagation.py
"""AP10 end-to-end: a transient owner-key miss propagates to the coordinator sink.

A transient owner-key lookup miss (``OwnerKeyLookupTransientError``, base
``Exception`` -- NOT a ``DecryptionError`` and NOT a ``RuntimeError``) raised deep
in the decrypt layer must survive every intermediate surface
(``location_request`` -> ``api`` -> coordinator) and land in the coordinator sink as
a plain per-device skip: no account-wide reauth-budget touch
(``note_decrypt_failure``), no reauth flow, no cycle failure.

These tests exercise the two real coordinator sinks via their established fixtures,
with the transient injected at the ``api.async_get_device_location`` seam -- the
single chokepoint both sinks consume, and the layer the AP3/AP5/AP7 fixes made
transparent so the transient reaches it instead of being swallowed into ``{}``.

Counter-spy scope (precise): the spy targets ONLY the reauth-budget surfaces
``note_decrypt_failure`` and the entry reauth start. ``note_error`` is the
per-tracker diagnostic hook (NOT a counter); the transient locate sink calls it by
design, so it is explicitly NOT treated as a violation here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    OwnerKeyLookupTransientError,
)
from tests.helpers import drain_loop
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.locate_mixin_stub import LocateStub


# ---------------------------------------------------------------------------
# Path (a): poll cycle -> coordinator/polling.py sink (~:2103)
# ---------------------------------------------------------------------------
class _DummyCache:
    """Minimal cache stub for the real coordinator build."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(self, _key: str, _value):  # pragma: no cover
        return None


class _DummyHass:
    """Minimal HA stub exposing the loop and task helper the coordinator needs."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.data: dict = {}

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401
        return self.loop.create_task(coro)


class _TransientOwnerKeyAPI:
    """API stub whose per-device locate raises the transient owner-key error.

    This mirrors the post-fix production behaviour: ``api.async_get_device_location``
    re-raises ``OwnerKeyLookupTransientError`` (AP7) instead of returning ``{}``.
    """

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        raise OwnerKeyLookupTransientError(
            "Owner key retrieval did not complete (transient)."
        )


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    api: object,
    devices: list[dict[str, str]],
) -> GoogleFindMyCoordinator:
    """Build a real coordinator wired with stubs (mirrors test_poll_timeout_hardening)."""
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    hass = _DummyHass(loop)
    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id", options={}, data={}, title="Test Entry"
    )
    coordinator.api = api
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)

    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data):
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data
    return coordinator


def test_poll_cycle_transient_owner_key_skips_device_without_counter_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path (a): a transient raised from ``api.async_get_device_location`` reaches the
    poll sink and is skipped per-device WITHOUT a counter touch or cycle failure.

    Asserts the sink at ``coordinator/polling.py`` (~:2103):
      * ``note_decrypt_failure`` is NEVER called (no reauth-budget event),
      * the account-wide decrypt counter stays at 0 (no ``cycle_decrypt_error``),
      * no auth state is set / no reauth is requested,
      * the cycle does not fail (``last_update_success`` stays True -> no
        ``cycle_failed`` from this device).
    """
    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-transient", "name": "Transient Tag"}]
    coordinator = _make_coordinator(
        monkeypatch, loop, _TransientOwnerKeyAPI(), devices
    )
    coordinator._consecutive_decrypt_failures = 0

    note_decrypt_failure = MagicMock(return_value=False)
    coordinator.note_decrypt_failure = note_decrypt_failure
    set_auth_state = MagicMock(return_value=None)
    coordinator._set_auth_state = set_auth_state
    request_reauth = MagicMock(return_value=None)
    coordinator._request_poll_reauth = request_reauth

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    # Counter-spy: the reauth-budget surfaces were never touched.
    note_decrypt_failure.assert_not_called()
    request_reauth.assert_not_called()
    set_auth_state.assert_not_called()
    # The account-wide decrypt counter never advanced (no cycle_decrypt_error).
    assert coordinator._consecutive_decrypt_failures == 0
    # The transient did not fail the cycle.
    assert coordinator.last_update_success is True


# ---------------------------------------------------------------------------
# Path (b): manual locate -> coordinator/locate.py sink (~:605)
# ---------------------------------------------------------------------------
@pytest.fixture
def locate_coord(monkeypatch: pytest.MonkeyPatch) -> LocateStub:
    """A LocateStub past every cooldown gate so the Nova seam is reached."""
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.locate.time.monotonic",
        lambda: 1000.0,
    )
    entry = make_config_entry(entry_id="transient-locate-entry")
    return LocateStub(config_entry=entry)


@pytest.mark.asyncio
async def test_manual_locate_transient_owner_key_debug_skip_no_counter_no_reauth(
    locate_coord: LocateStub,
) -> None:
    """Path (b): a transient raised from ``api.async_get_device_location`` reaches the
    locate sink and degrades to an empty result without a counter touch or reauth.

    Asserts the sink at ``coordinator/locate.py`` (~:605):
      * the result is the empty mapping,
      * ``note_decrypt_failure`` is NEVER called (no reauth-budget event),
      * no auth state is set / no reauth flow is started.

    ``note_error`` (per-tracker diagnostic, NOT a counter) is called by the
    transient handler by design and is therefore explicitly NOT asserted as a
    violation here (counter-spy precision per AP10).
    """
    coord = locate_coord
    coord.note_decrypt_failure = MagicMock(return_value=False)
    coord.config_entry.async_start_reauth = MagicMock()
    coord.api.async_get_device_location = AsyncMock(
        side_effect=OwnerKeyLookupTransientError(
            "Owner key retrieval did not complete (transient)."
        )
    )

    result = await coord.async_locate_device("dev-1")

    # DEBUG skip: empty result, no escalation.
    assert result == {}
    coord.note_decrypt_failure.assert_not_called()
    coord._set_auth_state.assert_not_called()
    coord.config_entry.async_start_reauth.assert_not_called()
    # note_error is the per-tracker diagnostic hook (NOT a counter); the transient
    # path calls it by design, so its invocation is allowed, not a violation.

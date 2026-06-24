# tests/test_poll_timeout_hardening.py
"""Regression tests for the poll-timeout hardening (B1-B3).

These pin the behaviour introduced to stop the noisy
"Poll timed out ... (FCM connected)" warnings:

* B2 - the outer per-device poll guard must be strictly larger than the inner
  FCM wait, so the inner request can return its clean empty result before the
  outer guard fires (Nygard: stagger nested timeout budgets). It must also cover
  the *preceding* Nova HTTP round-trip: the inner request runs HTTP (capped at
  NOVA_REQUEST_TOTAL_TIMEOUT_S) and only then waits for FCM, so a slow-but-
  successful HTTP call would otherwise push the FCM wait past the guard (the
  Codex finding fixed alongside this suite).
* B3 - an empty location result is an expected idle outcome and must log at
  INFO, not WARNING.
* Behaviour preservation - a genuine FCM-disconnected timeout must still count
  as a failed cycle and advance ``_consecutive_timeouts``; the benign
  FCM-connected timeout must not.
* Idle diagnostics - the DEBUG-only ``_log_idle_poll_diagnostics`` helper stays
  silent unless DEBUG is enabled and degrades gracefully for missing/garbled
  cache timestamps.

The setup mirrors ``tests/test_coordinator_timeout.py`` (explicit event loop +
``_async_start_poll_cycle``), the established pattern for this code path.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy.const import (
    LOCATION_REQUEST_TIMEOUT_S,
    NOVA_REQUEST_TOTAL_TIMEOUT_S,
    POLL_DEVICE_OUTER_TIMEOUT_S,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.helpers.stats import FcmStatus
from custom_components.googlefindmy.coordinator.polling import PollingOperations
from tests.helpers import drain_loop

_POLLING_LOGGER = "custom_components.googlefindmy.coordinator.polling"


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str):  # pragma: no cover - stub
        return None

    async def async_set_cached_value(
        self, _key: str, _value
    ):  # pragma: no cover - stub
        return None


class _DummyBus:
    """Provide async_listen/async_fire placeholders used by the coordinator."""

    def async_listen(self, *_args, **_kwargs):  # pragma: no cover - stub
        return lambda: None

    def async_fire(self, *_args, **_kwargs):  # pragma: no cover - stub
        return None


class _DummyHass:
    """Lightweight Home Assistant stub capturing created tasks."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401
        return self.loop.create_task(coro, name=name)


class _TimeoutAPI:
    """API stub that always times out during location requests."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        raise TimeoutError()


class _EmptyResultAPI:
    """API stub that returns an empty result (no reporter in range)."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        return []


class _TransientOwnerKeyAPI:
    """API stub raising a transient owner-key lookup error per device."""

    async def async_get_device_location(self, _dev_id: str, _dev_name: str):
        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
            OwnerKeyLookupTransientError,
        )

        raise OwnerKeyLookupTransientError(
            "Owner key retrieval did not complete (transient)."
        )


def _make_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    api: object,
    devices: list[dict[str, str]],
) -> GoogleFindMyCoordinator:
    """Build a coordinator wired with stubs, mirroring test_coordinator_timeout."""

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


def test_outer_poll_budget_exceeds_inner_wait() -> None:
    """B2: the outer per-device guard must be strictly larger than the inner wait."""

    assert POLL_DEVICE_OUTER_TIMEOUT_S > LOCATION_REQUEST_TIMEOUT_S


def test_outer_poll_budget_covers_http_plus_fcm_phases() -> None:
    """Codex regression: the inner request runs the Nova HTTP round-trip and the
    FCM wait *sequentially*, so the outer guard must cover BOTH budgets.

    A previous revision sized the guard as ``LOCATION_REQUEST_TIMEOUT_S + 10``
    (40s), which a slow-but-successful HTTP call (up to NOVA_REQUEST_TOTAL_TIMEOUT_S)
    could exhaust before the inner FCM wait even started, re-entering the spurious
    outer-``TimeoutError`` path this hardening removes.
    """

    assert (
        POLL_DEVICE_OUTER_TIMEOUT_S
        >= NOVA_REQUEST_TOTAL_TIMEOUT_S + LOCATION_REQUEST_TIMEOUT_S
    )


def test_transient_owner_key_lookup_skips_device_without_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4/AP5: a transient owner-key lookup error skips the device as an ordinary
    cycle event and never drives the account-wide reauth budget.

    ``OwnerKeyLookupTransientError`` is caught by the dedicated handler (before the
    ``DecryptionError`` block) which continues to the next device without
    advancing the consecutive decrypt-failure counter or setting an auth state, so
    a transient miss cannot trip a spurious reauth.
    """
    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-transient", "name": "Transient Tag"}]
    coordinator = _make_coordinator(
        monkeypatch, loop, _TransientOwnerKeyAPI(), devices
    )
    coordinator._consecutive_decrypt_failures = 0
    set_auth_state = AsyncMock()
    coordinator._set_auth_state = set_auth_state

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)

    # Transient miss did not feed the account-wide reauth path.
    assert coordinator._consecutive_decrypt_failures == 0
    set_auth_state.assert_not_called()


def test_empty_location_logs_info_not_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3: an empty location result is expected and must log at INFO, not WARNING."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-empty", "name": "Idle Tag"}]
    coordinator = _make_coordinator(monkeypatch, loop, _EmptyResultAPI(), devices)

    try:
        with caplog.at_level(logging.INFO, logger=_POLLING_LOGGER):
            loop.run_until_complete(
                coordinator._async_start_poll_cycle(devices, force=True)
            )
    finally:
        drain_loop(loop)

    idle_records = [
        rec
        for rec in caplog.records
        if rec.name == _POLLING_LOGGER
        and "No location data returned" in rec.getMessage()
    ]
    assert idle_records, "expected an idle 'No location data returned' log line"
    assert all(rec.levelno == logging.INFO for rec in idle_records)
    assert not any(
        rec.name == _POLLING_LOGGER
        and rec.levelno >= logging.WARNING
        and "No location data returned" in rec.getMessage()
        for rec in caplog.records
    )


def test_fcm_disconnected_timeout_still_counts_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour preservation: a disconnected-FCM timeout fails the cycle."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-1", "name": "Device"}]
    coordinator = _make_coordinator(monkeypatch, loop, _TimeoutAPI(), devices)
    coordinator._fcm_status_state = FcmStatus.DISCONNECTED
    coordinator._consecutive_timeouts = 0

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.stats["timeouts"] == 1
    assert coordinator._consecutive_timeouts == 1


def test_fcm_connected_timeout_is_benign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected-FCM timeout is healthy: counted as a timeout stat but it must
    not advance the consecutive-timeout failure counter."""

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-1", "name": "Device"}]
    coordinator = _make_coordinator(monkeypatch, loop, _TimeoutAPI(), devices)
    coordinator._fcm_status_state = FcmStatus.CONNECTED
    coordinator._consecutive_timeouts = 0

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)

    assert coordinator.stats["timeouts"] == 1
    assert coordinator._consecutive_timeouts == 0


def _make_idle_diag_stub(**attrs: object) -> SimpleNamespace:
    """Build a minimal stand-in exposing only what ``_log_idle_poll_diagnostics``
    reads. This keeps the helper tests as true unit tests, without constructing a
    full coordinator (and the async background tasks that would entail)."""

    base: dict[str, object] = {
        "_device_location_data": {},
        "_fcm_status_changed_at": None,
        "_fcm_status_state": FcmStatus.CONNECTED,
    }
    base.update(attrs)
    return SimpleNamespace(**base)


def _call_idle_diagnostics(stub: SimpleNamespace, *, source: str) -> None:
    """Invoke the unbound mixin method against the lightweight stub."""

    PollingOperations._log_idle_poll_diagnostics(
        stub, "dev-1", "Idle Tag", source=source
    )


def test_idle_diagnostics_silent_when_debug_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostic is DEBUG-only: with DEBUG disabled it must emit nothing."""

    stub = _make_idle_diag_stub(
        _device_location_data={"dev-1": {"last_seen": 1000.0}},
        _fcm_status_changed_at=500.0,
    )

    with caplog.at_level(logging.INFO, logger=_POLLING_LOGGER):
        _call_idle_diagnostics(stub, source="inner-empty")

    assert not [rec for rec in caplog.records if "Idle poll for" in rec.getMessage()]


def test_idle_diagnostics_reports_ages_when_debug_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With DEBUG enabled and a valid cache, both ages are rendered."""

    stub = _make_idle_diag_stub(
        _device_location_data={"dev-1": {"last_seen": 10.0}},
        _fcm_status_changed_at=5.0,
    )

    with caplog.at_level(logging.DEBUG, logger=_POLLING_LOGGER):
        _call_idle_diagnostics(stub, source="outer-timeout")

    messages = [
        rec.getMessage()
        for rec in caplog.records
        if "Idle poll for" in rec.getMessage()
    ]
    assert len(messages) == 1
    msg = messages[0]
    assert "outer-timeout" in msg
    assert "last report" in msg
    assert "ago" in msg
    # A concrete report age must be computed, not the "never"/"unknown" fallbacks.
    assert "never" not in msg


def test_idle_diagnostics_handles_missing_last_seen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No cached report at all -> the report age renders as ``never``."""

    stub = _make_idle_diag_stub(
        _device_location_data={},
        _fcm_status_changed_at=5.0,
    )

    with caplog.at_level(logging.DEBUG, logger=_POLLING_LOGGER):
        _call_idle_diagnostics(stub, source="inner-empty")

    msg = next(
        rec.getMessage()
        for rec in caplog.records
        if "Idle poll for" in rec.getMessage()
    )
    assert "last report never ago" in msg


def test_idle_diagnostics_handles_garbled_timestamps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-numeric ``last_seen`` and a missing FCM timestamp degrade to
    ``unknown`` instead of raising (defensive coordinate parsing)."""

    stub = _make_idle_diag_stub(
        _device_location_data={"dev-1": {"last_seen": "not-a-number"}},
        _fcm_status_changed_at=None,
    )

    with caplog.at_level(logging.DEBUG, logger=_POLLING_LOGGER):
        _call_idle_diagnostics(stub, source="inner-empty")

    msg = next(
        rec.getMessage()
        for rec in caplog.records
        if "Idle poll for" in rec.getMessage()
    )
    assert "last report unknown ago" in msg
    assert "FCM status" in msg
    assert "for unknown" in msg


_LOCATION_REQUEST_LOGGER = (
    "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker."
    "location_request"
)


def test_inner_fcm_wait_timeout_logs_info_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """B3 at the inner level: when no reporter relays the BLE tag within the
    window, ``get_location_data_for_device`` must log the FCM-wait timeout at
    INFO (not WARNING) and return an empty list.

    This pins the noise-reduction demotion on the source line itself; the
    coordinator-level tests above only exercise a stubbed API, so without this
    test the inner ``location_request.py`` change would be unguarded.
    """

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        location_request as lr,
    )

    class _FakeReceiver:
        async def async_register_for_location_updates(self, _canonic, _callback):
            # Return a token but never invoke the callback, so the FCM wait
            # below times out (the "no reporter in range" idle outcome).
            return "fcm-token"

        async def async_unregister_for_location_updates(self, _canonic):
            return None

    class _Cache:
        entry_id = "entry-1"

        async def async_get_cached_value(self, _key):
            return None

        async def async_set_cached_value(self, _key, _value):
            return None

    # Collapse the inner FCM wait so the timeout fires immediately, and stub the
    # surrounding orchestration that is irrelevant to the logged outcome.
    monkeypatch.setattr(lr, "LOCATION_REQUEST_TIMEOUT_S", 0)
    monkeypatch.setattr(lr, "create_location_request", lambda *a, **k: "00")
    monkeypatch.setattr(lr, "async_nova_request", AsyncMock(return_value=""))
    lr.register_fcm_receiver_provider(lambda _entry=None: _FakeReceiver())

    # Explicit event loop (not asyncio.run) to honour the repo's test-suite
    # guard; mirrors the established pattern in this file.
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.INFO, logger=_LOCATION_REQUEST_LOGGER):
            result = loop.run_until_complete(
                lr.get_location_data_for_device(
                    "canonic-1",
                    "Idle Tag",
                    username="user@example.com",
                    cache=_Cache(),
                )
            )
    finally:
        drain_loop(loop)
        lr.unregister_fcm_receiver_provider()

    assert result == []
    timeout_records = [
        rec
        for rec in caplog.records
        if rec.name == _LOCATION_REQUEST_LOGGER
        and "No location response received" in rec.getMessage()
    ]
    assert timeout_records, "expected the inner FCM-wait timeout log line"
    assert all(rec.levelno == logging.INFO for rec in timeout_records)
    assert not any(rec.levelno >= logging.WARNING for rec in timeout_records)

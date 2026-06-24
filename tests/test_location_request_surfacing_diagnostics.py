# tests/test_location_request_surfacing_diagnostics.py
"""Diagnostic surfacing contract for the locate-request error handler (R6/R9c).

When a locate request fails with an unexpected error, the broad surfacing block
(``location_request.get_location_data_for_device`` -> ``except Exception``) is the
last place the failure is observable. Two plan requirements pin its behavior:

* **R6** (AGENTS.md Section 5 redaction): the user-facing WARNING must not carry a
  raw device display name; it states a count/index instead, while the full name
  stays at DEBUG only (the Count@WARNING / Name@DEBUG line from PR #1129).
* **R9c**: the surfacing record must contain the *full* cause chain so a gRPC
  server detail nested two levels deep (surfacing -> SpotError -> GRPCError) is
  not overwritten by the first level's ``str(exc)``.

These are RED until the AP7 diagnostic-logging rework lands; they exercise the
real surfacing path via the established locate-flow stubs (no ``asyncio.run``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import grpclib.exceptions
import pytest
from grpclib.const import Status

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request,
)
from custom_components.googlefindmy.SpotApi.spot_request import SpotGrpcStatusError

pytestmark = pytest.mark.asyncio


class _DummyFcmReceiver:
    """Receiver that never delivers a report (the locate fails some other way)."""

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> str:
        return "fcm-token"

    async def async_unregister_for_location_updates(self, device_id: str) -> None:
        return None


class _FakeTokenCache:
    """Minimal entry-scoped cache stub for the locate flow."""

    def __init__(self, label: str = "entry-surfacing") -> None:
        self.entry_id = label
        self.values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value


def _wire_locate_flow(
    monkeypatch: pytest.MonkeyPatch, *, surfacing_exc: Exception
) -> None:
    """Stub the locate flow so an unexpected error reaches the broad surfacing block.

    ``create_location_request`` is raised from inside the outer try-body but
    outside every specific ``except`` clause, so the error lands in the final
    ``except Exception`` surfacing handler (the unit under test) rather than in the
    inner Nova-request handler that returns an empty list.
    """
    receiver = _DummyFcmReceiver()
    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda: receiver)

    def _fake_make_callback(
        *, ctx: Any, canonic_device_id: str, **_: Any
    ) -> Callable[[str, str], None]:
        def _callback(_response_canonic_id: str, _hex: str) -> None:
            return None

        return _callback

    monkeypatch.setattr(
        location_request, "_make_location_callback", _fake_make_callback
    )

    def _boom(*_a: object, **_k: object) -> str:
        raise surfacing_exc

    monkeypatch.setattr(location_request, "create_location_request", _boom)


_SENTINEL_NAME = "Jens-Privat-Tracker-SENTINEL-7Q"


async def test_r6_surfacing_warning_omits_device_name_keeps_it_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED (R6): a unique device name must not leak into a user-facing WARNING.

    With a sentinel display name and a forced unexpected error, the surfacing block
    must keep the raw name out of any WARNING/ERROR record (count/index only) and
    only ever expose it at DEBUG. RED today: the broad ``except Exception`` block
    logs ``"Error requesting location for %s"`` with the raw name at ERROR.
    """
    _wire_locate_flow(monkeypatch, surfacing_exc=RuntimeError("unexpected boom"))

    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    result = await location_request.get_location_data_for_device(
        canonic_device_id="device-xyz",
        name=_SENTINEL_NAME,
        session=None,
        username="user@example.com",
        cache=_FakeTokenCache(),
    )
    assert result == []

    user_facing = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    ]
    assert user_facing, "expected at least one user-facing surfacing record"
    for rec in user_facing:
        assert _SENTINEL_NAME not in rec.getMessage()

    debug_text = " ".join(
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.DEBUG
    )
    assert _SENTINEL_NAME in debug_text


async def test_r9c_surfacing_includes_nested_grpc_cause_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED (R9c): the surfacing diagnostic record carries the two-levels-deep gRPC
    detail as a structured cause chain, not just as a raw traceback dump.

    The failure chain is surfacing-exc -> SpotGrpcStatusError -> GRPCError, where the
    GRPCError carries a server detail sentinel. AP7 must walk the full ``__cause__``
    chain into the surfacing diagnostic record (a ``Type: msg -> Type: msg`` string)
    so the sentinel survives at the user-facing diagnostic level. Today the
    surfacing record only logs the outermost ``str(exc)`` ("locate failed"); the
    nested gRPC detail appears solely in the unstructured ``traceback.format_exc()``
    DEBUG dump that R9 is meant to replace. The assertion therefore excludes the
    raw-traceback record (it must not be the only carrier) and requires a
    non-traceback surfacing record to carry the sentinel. RED today.
    """
    grpc_detail = "GRPC-DETAIL-SENTINEL-NESTED"
    grpc_err = grpclib.exceptions.GRPCError(Status.FAILED_PRECONDITION, grpc_detail)
    spot_err = SpotGrpcStatusError("gRPC error: FAILED_PRECONDITION")
    spot_err.__cause__ = grpc_err
    outer = RuntimeError("locate failed")
    outer.__cause__ = spot_err

    _wire_locate_flow(monkeypatch, surfacing_exc=outer)

    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    result = await location_request.get_location_data_for_device(
        canonic_device_id="device-xyz",
        name="Tracker",
        session=None,
        username="user@example.com",
        cache=_FakeTokenCache(),
    )
    assert result == []

    # Exclude the raw ``traceback.format_exc()`` dump: R9c requires the structured
    # cause chain in a dedicated surfacing record, not merely inside a stack trace.
    structured_records = [
        rec
        for rec in caplog.records
        if "Traceback:" not in rec.getMessage()
        and "traceback" not in rec.getMessage().lower()
    ]
    structured_text = " ".join(rec.getMessage() for rec in structured_records)
    assert grpc_detail in structured_text

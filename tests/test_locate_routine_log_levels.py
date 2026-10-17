# tests/test_locate_routine_log_levels.py
"""Pin the log level of the per-locate routine records to DEBUG.

Every successful locate request used to write eleven INFO records (two
request records, send, accept, wait, FCM registration, push receipt,
callback, receipt, decryption, record selection), and an FCM-wait timeout
wrote an INFO record of its own next to the one the poll loop writes for the
same empty result. None of them targets the user, and the Home
Assistant development guidelines ask integrations to "be restrictive with
`_LOGGER.info`, use `_LOGGER.debug` for anything which is not targeting the
user". A Home Assistant instance running with ``logger: default: info`` logged
about 16 such lines per minute.

The check is static (AST) on purpose: driving the success path end to end needs
the protobuf decoder and the decryption stack, and what this test pins is the
level of each call, not the flow around it. The decryption record is also
driven once at runtime, through the FCM callback with the decoder and the
decryption module stubbed, because no other test reaches its success branch.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyLookupTransientError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)

_PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"

# (module relative to the package, start of the format string)
_ROUTINE_RECORDS: tuple[tuple[str, str], ...] = (
    ("api.py", "API v3.0 Async: Requesting location for "),
    ("api.py", "API v3.0 Async: Selected location record for "),
    ("Auth/fcm_receiver_ha.py", "[entry=%s] Manual locate registration ready for "),
    ("Auth/fcm_receiver_ha.py", "push_received("),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Requesting location data for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Sending location request to Google API for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Location request accepted for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Waiting for location response for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "No location response received for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "FCM callback triggered for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Successfully received location data for ",
    ),
    (
        "NovaApi/ExecuteAction/LocateTracker/location_request.py",
        "Successfully decrypted %d location record(s) for ",
    ),
)


def _logger_calls(module: str, prefix: str) -> list[tuple[str, str]]:
    """Return ``(method, format string)`` for every ``<name>.<method>(...)``
    call in ``module`` whose receiver is a bare name containing ``logger`` (any
    case) and whose first argument is a string literal starting with ``prefix``.

    Declared blind spots: ``_LOGGER.log(level, ...)``, attribute receivers
    (``self._logger``, ``module._LOGGER``), a format string held in a variable
    and f-strings. The three modules use such a form only outside the twelve
    records (``api.py`` passes a prepared message to ``_LOGGER.error``); the
    test pins the twelve existing records, it is not a guard for new ones.
    """

    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and "LOGGER" in node.func.value.id.upper()
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        if node.args[0].value.startswith(prefix):
            found.append((node.func.attr, node.args[0].value))
    return found


@pytest.mark.parametrize(("module", "prefix"), _ROUTINE_RECORDS)
def test_routine_locate_record_logs_at_debug(module: str, prefix: str) -> None:
    """Each routine record exists exactly once and logs at DEBUG."""

    calls = _logger_calls(module, prefix)
    assert len(calls) == 1, f"expected one call for {prefix!r} in {module}: {calls}"
    method, _ = calls[0]
    assert method == "debug", f"{prefix!r} in {module} logs at {method}"


@pytest.mark.parametrize(("module", "prefix"), _ROUTINE_RECORDS)
def test_routine_locate_record_has_no_trailing_period(module: str, prefix: str) -> None:
    """Home Assistant log style: no period (and no ellipsis) at the end."""

    _, fmt = _logger_calls(module, prefix)[0]
    assert not fmt.rstrip().endswith("."), f"{fmt!r} ends with a period"


@pytest.mark.asyncio
async def test_decryption_success_record_logs_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A decrypted FCM response logs its record count at DEBUG only.

    The callback also tags the first record with the lower-cased canonical id
    the response arrived for; the mixed-case response id pins that step.
    """

    decrypted = [{"latitude": 49.89}, {"latitude": 49.90}]

    async def _decrypt(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return decrypted

    decoder_module = SimpleNamespace(parse_device_update_protobuf=lambda _hex: object())
    decrypt_module = SimpleNamespace(
        async_decrypt_location_response_locations=_decrypt,
        DecryptionError=DecryptionError,
        StaleOwnerKeyError=StaleOwnerKeyError,
        OwnerKeyLookupTransientError=OwnerKeyLookupTransientError,
    )
    eid_module = SimpleNamespace(SpotApiEmptyResponseError=SpotApiEmptyResponseError)
    monkeypatch.setattr(
        location_request, "_import_decoder_module", lambda: decoder_module
    )
    monkeypatch.setattr(
        location_request, "_import_decrypt_locations_module", lambda: decrypt_module
    )
    monkeypatch.setattr(location_request, "_import_eid_info_module", lambda: eid_module)

    caplog.set_level(logging.DEBUG, logger=location_request.__name__)
    ctx = location_request._CallbackContext()
    callback = location_request._make_location_callback(
        name="Backpack Tracker",
        canonic_device_id="device-canonic-id",
        ctx=ctx,
        loop=asyncio.get_running_loop(),
        cache=MagicMock(),
    )
    callback("Device-Canonic-ID", "00")
    await asyncio.wait_for(ctx.event.wait(), timeout=2.0)

    success = [
        rec
        for rec in caplog.records
        if rec.getMessage()
        == "Successfully decrypted 2 location record(s) for Backpack Tracker"
    ]
    assert [rec.levelno for rec in success] == [logging.DEBUG]
    above_debug = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == location_request.__name__ and rec.levelno > logging.DEBUG
    ]
    assert above_debug == [], f"routine success path logged above DEBUG: {above_debug}"
    assert ctx.data == decrypted
    assert ctx.data[0]["canonic_id"] == "device-canonic-id"

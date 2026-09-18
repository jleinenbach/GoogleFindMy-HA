# tests/test_fcm_receiver_exception_records.py
"""Producer exceptions reach ``fcm_receiver_ha`` records as a summary, not as text.

``Auth/AGENTS.md`` keeps raw exception text and tracebacks out of the log
stream (``describe_exception`` / ``exception_origin``). One test per level
class of the rewritten sinks: ``debug`` (client stop), ``info`` (client start
in the supervisor loop) and ``error`` (background task callback). Each fixture
raises with a marker text that must not appear in any record.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

_LOGGER_NAME = "custom_components.googlefindmy.Auth.fcm_receiver_ha"
_MARKER = "MARKER-raw-producer-text-8f3c1e"


def _records(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.getMessage().startswith(prefix)
    ]


def _assert_summarised(record: logging.LogRecord, type_name: str) -> None:
    message = record.getMessage()
    assert _MARKER not in message
    assert f"{type_name} ({len(_MARKER)} chars withheld)" in message
    assert record.exc_info is None
    assert record.exc_text is None


@pytest.mark.asyncio
async def test_client_stop_failure_is_summarised_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``async_stop``: an unexpected ``pc.stop()`` error is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    entry_id = "entry-stop-debug"
    receiver.pcs[entry_id] = SimpleNamespace(
        stop=AsyncMock(side_effect=RuntimeError(_MARKER)),
    )

    await receiver.async_stop(timeout=0.5)

    records = _records(caplog, f"[entry={entry_id}] FCM client stop unexpected error")
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    _assert_summarised(records[0], "RuntimeError")
    assert entry_id not in receiver.pcs


@pytest.mark.asyncio
async def test_client_start_failure_is_summarised_at_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Supervisor loop: a failing ``pc.start()`` is logged by type at INFO."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    entry_id = "entry-start-info"
    receiver._entry_generation[entry_id] = 1
    started = asyncio.Event()

    async def _start() -> None:
        started.set()
        raise ConnectionError(_MARKER)

    fake_pc = SimpleNamespace(
        start=AsyncMock(side_effect=_start),
        stop=AsyncMock(),
        run_state=None,
        do_listen=False,
    )

    async def _ensure(
        _entry_id: str, _cache: object, _generation: int | None = None
    ) -> object:
        return fake_pc

    async def _register(_entry_id: str, _generation: int | None = None) -> bool:
        return True

    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", _register)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(started.wait(), timeout=2)
    await receiver.async_stop(timeout=0.5)

    records = _records(caplog, f"[entry={entry_id}] FCM client failed to start")
    assert len(records) >= 1
    assert records[0].levelno == logging.INFO
    _assert_summarised(records[0], "ConnectionError")


@pytest.mark.asyncio
async def test_background_task_failure_is_summarised_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_track_task``: the done callback logs type and origin, no traceback."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    async def failing_task() -> None:
        raise ValueError(_MARKER)

    receiver = FcmReceiverHA()
    task = asyncio.create_task(failing_task())
    receiver._track_task(task, label="marker-task")
    # The done callback was registered before this await, so it runs before
    # the awaiting coroutine is woken (under the default, non-eager task
    # factory): no sleep, no timing race.
    with pytest.raises(ValueError):
        await task

    records = _records(caplog, "Background task failed (marker-task)")
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    _assert_summarised(records[0], "ValueError")
    # The origin replaces the traceback: file, line and function of the raise.
    assert "in failing_task" in records[0].getMessage()


# Error paths that no test reached before the rewrite (codecov patch report
# on PR #1306): each one drives the handler with a producer exception carrying
# the marker text and pins the summarised record.


def _summarised(caplog: pytest.LogCaptureFixture, prefix: str, type_name: str) -> None:
    records = _records(caplog, prefix)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    _assert_summarised(records[0], type_name)


def test_short_run_repair_delete_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_delete_repair_issues_for_entry`: a registry error is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    receiver._hass = SimpleNamespace()

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(fcm_receiver_ha.ir, "async_delete_issue", _raise)
    receiver._delete_repair_issues_for_entry("entry-repair")
    _summarised(
        caplog,
        "[entry=entry-repair] Failed to delete short-run crash-loop",
        "RuntimeError",
    )


def test_entry_removal_repair_delete_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`async_delete_entry_repair_issues`: every failing issue id is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(fcm_receiver_ha.ir, "async_delete_issue", _raise)
    FcmReceiverHA.async_delete_entry_repair_issues(SimpleNamespace(), "entry-gone")
    records = _records(caplog, "[entry=entry-gone] Failed to delete repair issue")
    assert len(records) == 3
    for record in records:
        _assert_summarised(record, "RuntimeError")


@pytest.mark.asyncio
async def test_task_result_retrieval_failure_is_summarised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_track_task`: `Task.exception()` raising is logged by type, not traceback."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    task = asyncio.create_task(asyncio.sleep(0))

    def _exception() -> BaseException | None:
        raise RuntimeError(_MARKER)

    task.exception = _exception  # type: ignore[method-assign]
    receiver._track_task(task, label="broken-result")
    await task
    _summarised(caplog, "Unhandled exception retrieving task result", "RuntimeError")


def test_cache_provider_reset_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_scoped_cache_provider`: a failing context reset is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    class _BrokenVar:
        def set(self, _value: object) -> object:
            return object()

        def reset(self, _token: object) -> None:
            raise RuntimeError(_MARKER)

    monkeypatch.setattr(fcm_receiver_ha, "_CACHE_PROVIDER", _BrokenVar())
    with receiver._scoped_cache_provider(SimpleNamespace()):
        pass
    _summarised(caplog, "Cache provider reset failed", "RuntimeError")


def test_base64_decode_failure_is_summarised(caplog: pytest.LogCaptureFixture) -> None:
    """`_extract_hex_payload`: a malformed payload is logged by type at ERROR."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    assert (
        receiver._extract_hex_payload(
            {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": "%%%not-base64%%%"}}
        )
        is None
    )
    records = _records(caplog, "FCM Base64 decode failed")
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert _MARKER not in records[0].getMessage()
    assert "chars withheld" in records[0].getMessage()
    assert records[0].exc_info is None


@pytest.mark.asyncio
async def test_notification_handler_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_handle_notification_async`: a failing parse step is logged by type at ERROR."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    def _raise(_payload: object) -> str | None:
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(receiver, "_extract_hex_payload", _raise)
    await receiver._handle_notification_async("entry-notify", {"data": {}})
    _summarised(caplog, "Failed to handle FCM notification safely", "RuntimeError")


def test_token_routing_update_failure_is_summarised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_update_token_routing`: an unusable entry list is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    class _Broken:
        def __iter__(self) -> object:
            raise RuntimeError(_MARKER)

    receiver._update_token_routing("token-1", _Broken())  # type: ignore[arg-type]
    _summarised(caplog, "Token routing update skipped", "RuntimeError")


def test_canonic_id_extraction_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_extract_canonic_id_from_response`: a decoder error is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    def _raise(_hex: object) -> object:
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(
        fcm_receiver_ha.decoder_module, "parse_device_update_protobuf", _raise
    )
    assert receiver._extract_canonic_id_from_response("00") is None
    _summarised(
        caplog, "Failed to extract canonical id from FCM response", "RuntimeError"
    )


@pytest.mark.asyncio
async def test_flush_task_failure_is_summarised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`_schedule_flush`: a failing `_flush` is logged by type at ERROR."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    receiver._debounce_ms = 0

    async def _raise(_key: object) -> None:
        raise RuntimeError(_MARKER)

    monkeypatch.setattr(receiver, "_flush", _raise)
    key = ("entry-flush", "device-1")
    receiver._schedule_flush(key)
    await asyncio.wait_for(receiver._flush_tasks[key], timeout=2)
    _summarised(caplog, "Flush task for entry-flush/device-1 failed", "RuntimeError")


@pytest.mark.asyncio
async def test_client_stop_network_error_is_summarised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`async_stop`: a `ConnectionError` from `pc.stop()` is logged by type at DEBUG."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()
    receiver.pcs["entry-net"] = SimpleNamespace(
        stop=AsyncMock(side_effect=ConnectionError(_MARKER)),
    )
    await receiver.async_stop(timeout=0.5)
    _summarised(
        caplog, "[entry=entry-net] FCM client stop network error", "ConnectionError"
    )


def test_google_home_filter_failure_is_summarised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_prepare_coordinator_payload`: a failing filter is logged by type, payload kept."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    class _Filter:
        def should_filter_detection(self, _device_id: str, _name: str) -> object:
            raise RuntimeError(_MARKER)

    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(
            runtime_data=SimpleNamespace(google_home_filter=_Filter())
        )
    )
    key = ("entry-ghf", "device-ghf-1234")
    result = receiver._prepare_coordinator_payload(
        coordinator, key, {"semantic_name": "Kitchen"}
    )
    assert result is not None
    _summarised(caplog, "Google Home filter error for device-g", "RuntimeError")


def test_coordinator_cache_fallback_failure_is_summarised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_write_coordinator_payload`: a coordinator without caches is logged by type."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    receiver = FcmReceiverHA()

    class _Broken:
        @property
        def _device_location_data(self) -> dict[str, object]:
            raise RuntimeError(_MARKER)

    assert receiver._write_coordinator_payload(_Broken(), "device-cache", {}) is False
    _summarised(
        caplog, "Coordinator cache update failed for device-cache", "RuntimeError"
    )

# tests/test_poll_missing_location_data_level.py
"""Log level of the "No location data" record in the poll cycle.

A tracker whose answer carries metadata but neither coordinates nor a semantic
name (for example a tag with a flat battery) produced a WARNING on every poll
cycle. Such a tracker is out of reach, not faulty, so the record must not use
WARNING: it logs once at INFO per tracker and then at DEBUG until a location
arrives again (Home Assistant quality-scale rule ``log-when-unavailable``).

The tests assert through ``caplog`` only, so they run unchanged against the
code before and after the change. The setup reuses the coordinator builder of
``tests/test_poll_timeout_hardening.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.test_poll_timeout_hardening import _make_coordinator

_POLLING_LOGGER = "custom_components.googlefindmy.coordinator.polling"
_MISSING = "No location data (coordinates or semantic name) available for"


class _ScriptedAPI:
    """API stub replaying a scripted answer per device.

    ``"meta"`` is an answer with metadata only, ``"fix"`` one with coordinates.
    """

    def __init__(self, script: dict[str, list[str]]) -> None:
        self._script = {dev_id: list(steps) for dev_id, steps in script.items()}

    async def async_get_device_location(self, dev_id: str, dev_name: str):
        step = self._script[dev_id].pop(0)
        if step == "meta":
            return {
                "id": dev_id,
                "name": dev_name,
                "metadata_only": True,
                "owner_key_version": 1,
            }
        return {
            "id": dev_id,
            "name": dev_name,
            "latitude": 1.0,
            "longitude": 1.0,
            "accuracy": 5.0,
            "last_seen": time.time(),
        }


def _missing_records(
    caplog: pytest.LogCaptureFixture, dev_id: str
) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if rec.name == _POLLING_LOGGER
        and rec.getMessage().startswith(_MISSING)
        and dev_id in rec.getMessage()
    ]


async def _run_cycles(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    script: dict[str, list[str]],
    *,
    purge_after: dict[int, str] | None = None,
) -> GoogleFindMyCoordinator:
    """Run one poll cycle per scripted step for every device in ``script``."""

    loop = asyncio.get_running_loop()
    devices = [{"id": dev_id, "name": f"Tag {dev_id}"} for dev_id in script]
    coordinator = _make_coordinator(monkeypatch, loop, _ScriptedAPI(script), devices)
    coordinator.is_automatic_polling_enabled = lambda _device_id: True
    cycles = len(next(iter(script.values())))
    with caplog.at_level(logging.DEBUG, logger=_POLLING_LOGGER):
        for cycle in range(cycles):
            await coordinator._async_start_poll_cycle(devices, force=True)
            if purge_after and cycle in purge_after:
                coordinator.purge_device(purge_after[cycle])
    # The cycle schedules a debounced stats save; end it with the test.
    save_task = getattr(coordinator, "_stats_save_task", None)
    if save_task is not None:
        save_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await save_task
    return coordinator


@pytest.mark.asyncio
async def test_missing_location_data_logs_info_once_then_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without, without, location, without: INFO, DEBUG, then INFO again."""

    await _run_cycles(
        monkeypatch, caplog, {"dev-flat": ["meta", "meta", "fix", "meta"]}
    )

    records = _missing_records(caplog, "dev-flat")
    assert not [rec for rec in records if rec.levelno >= logging.WARNING], (
        "a tracker without location data is not logged at WARNING"
    )
    assert [rec.levelno for rec in records] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO,
    ]
    assert all("Tag dev-flat" not in rec.getMessage() for rec in records)


@pytest.mark.asyncio
async def test_missing_location_data_level_is_kept_per_tracker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Each tracker gets its own first INFO; a purge starts the tracker afresh."""

    await _run_cycles(
        monkeypatch,
        caplog,
        {"dev-a": ["meta", "meta", "meta"], "dev-b": ["meta", "meta", "meta"]},
        purge_after={1: "dev-a"},
    )

    levels_a = [rec.levelno for rec in _missing_records(caplog, "dev-a")]
    levels_b = [rec.levelno for rec in _missing_records(caplog, "dev-b")]
    assert not [level for level in levels_a + levels_b if level >= logging.WARNING], (
        "a tracker without location data is not logged at WARNING"
    )
    assert levels_a == [logging.INFO, logging.DEBUG, logging.INFO]
    assert levels_b == [logging.INFO, logging.DEBUG, logging.DEBUG]

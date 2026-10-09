# tests/test_fcm_receiver_manual_locate_selection.py
"""Tests for ``FcmReceiverHA._select_manual_locate_entry``.

The selector picks the config entry whose cache a manual locate registers
against: a coordinator that reports the device as present wins, otherwise one
that knows a display name for it, otherwise the first entry with an id.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

_DEVICE = "device-canonic-id"


class _Entry:
    def __init__(self, entry_id: str | None) -> None:
        self.entry_id = entry_id


class _Coordinator:
    """Coordinator stub; ``present``/``display`` may be a value or an exception."""

    def __init__(
        self,
        entry_id: str | None,
        *,
        present: Any = False,
        display: Any = None,
        cache: object | None = None,
    ) -> None:
        self.config_entry = _Entry(entry_id)
        self.cache = cache if cache is not None else object()
        self._present = present
        self._display = display

    def is_device_present(self, device_id: str) -> bool:
        if isinstance(self._present, Exception):
            raise self._present
        return bool(self._present) and device_id == _DEVICE

    def get_device_display_name(self, device_id: str) -> str | None:
        if isinstance(self._display, Exception):
            raise self._display
        return self._display if device_id == _DEVICE else None


def _receiver(*coordinators: object) -> FcmReceiverHA:
    receiver = FcmReceiverHA()
    receiver.coordinators.extend(coordinators)
    return receiver


def test_present_entry_wins_and_cache_is_remembered() -> None:
    """A present device returns its entry id and caches the coordinator cache."""

    first = _Coordinator("entry-a")
    second = _Coordinator("entry-b", present=True)
    receiver = _receiver(first, second)

    entry_id, cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-b"
    assert cache is second.cache
    assert receiver._entry_caches["entry-b"] is second.cache


def test_entry_without_id_is_skipped() -> None:
    """A coordinator whose entry has no id is never selected."""

    receiver = _receiver(_Coordinator(None, present=True), _Coordinator("entry-b"))

    entry_id, _cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-b"


def test_display_name_beats_fallback() -> None:
    """Without a present device, an entry that knows the name is preferred."""

    receiver = _receiver(
        _Coordinator("entry-a"),
        _Coordinator("entry-b", display="Keys"),
    )

    entry_id, _cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-b"


def test_fallback_is_first_entry_with_id() -> None:
    """Without presence or name, the first usable entry is returned."""

    receiver = _receiver(_Coordinator("entry-a"), _Coordinator("entry-b"))

    entry_id, _cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-a"


def test_failing_presence_check_logs_entry_id_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising presence check is logged with the entry id, not the entry."""

    caplog.set_level(logging.DEBUG)
    receiver = _receiver(
        _Coordinator("entry-a", present=RuntimeError("boom")),
        _Coordinator("entry-b", present=True),
    )

    entry_id, _cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-b"
    records = [r for r in caplog.records if "presence check failed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].getMessage().startswith("[entry=entry-a] ")


def test_failing_display_lookup_counts_as_no_name() -> None:
    """A raising name lookup does not make the entry the display candidate."""

    receiver = _receiver(
        _Coordinator("entry-a"),
        _Coordinator("entry-b", display=RuntimeError("boom")),
    )

    entry_id, _cache = receiver._select_manual_locate_entry(_DEVICE)

    assert entry_id == "entry-a"


def test_no_coordinators_returns_nothing() -> None:
    """An empty receiver yields no entry and no cache."""

    assert _receiver()._select_manual_locate_entry(_DEVICE) == (None, None)


def test_remembered_cache_is_reused() -> None:
    """A cache already stored for the entry id beats the coordinator cache."""

    remembered = object()
    coordinator = _Coordinator("entry-a", present=True)
    receiver = _receiver(coordinator)
    receiver._entry_caches["entry-a"] = remembered

    assert receiver._select_manual_locate_entry(_DEVICE) == ("entry-a", remembered)


class _BareCoordinator:
    """Coordinator without cache and without presence or name helpers."""

    def __init__(self, entry_id: str) -> None:
        self.config_entry = _Entry(entry_id)


def test_bare_coordinator_falls_back_without_cache() -> None:
    """Missing helpers and caches still yield the entry id, with no cache."""

    receiver = _receiver(_BareCoordinator("entry-a"))

    assert receiver._select_manual_locate_entry(_DEVICE) == ("entry-a", None)
    assert "entry-a" not in receiver._entry_caches

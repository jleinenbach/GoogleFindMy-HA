# tests/test_foreign_reading_tracker.py
"""Tests for the per-device reading memory and the feedback log lines (#223).

Every rule of the log contract gets its own test: INFO once per (device,
reading), a WARNING for failed reports only after three reports in two polls
and never after a success, one WARNING per unknown ``Sx`` length, no device
identifier or key material in any line, and diagnostics per config entry.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    FOREIGN_READING_FEEDBACK_URL,
    P256_FOREIGN_READINGS,
    SECP160R1_FOREIGN_READINGS,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    foreign_reading_tracker,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.foreign_reading_tracker import (
    FOREIGN_READING_TRACKER,
    ForeignReadingTracker,
    foreign_device_key,
    get_foreign_reading_diagnostics,
)

_LOGGER_NAME = foreign_reading_tracker.__name__
_SUFFIX = "It contains no keys, IDs or locations."
_READING_1 = P256_FOREIGN_READINGS[0]
_READING_2 = P256_FOREIGN_READINGS[1]
_CANONIC_A = "canonic-id-aaaa-0001"
_CANONIC_B = "canonic-id-bbbb-0002"
_DEVICE_A = ("entry-1", _CANONIC_A)
_DEVICE_B = ("entry-1", _CANONIC_B)
_TRIED = tuple(reading.reading_id for reading in P256_FOREIGN_READINGS)


@pytest.fixture
def tracker() -> ForeignReadingTracker:
    """Return a fresh tracker."""
    return ForeignReadingTracker()


@pytest.fixture
def info_caplog(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture the tracker's logger at INFO."""
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    return caplog


def _lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _fail(tracker: ForeignReadingTracker, device: tuple[str, str], poll: str) -> None:
    tracker.note_all_failed(device, "secp256r1", 32, _TRIED, poll)


class TestInfo:
    """INFO for a provisional reading, once per (device, reading)."""

    def test_same_reading_three_times_logs_once(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        for _ in range(3):
            tracker.note_success(_DEVICE_A, _READING_1, 32)
        assert len(_lines(info_caplog, logging.INFO)) == 1

    def test_reading_change_a_b_a_logs_twice(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        for reading in (_READING_1, _READING_2, _READING_1):
            tracker.note_success(_DEVICE_A, reading, 32)
        assert len(_lines(info_caplog, logging.INFO)) == 2

    def test_line_pins_marker_suffix_and_fields(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        (line,) = _lines(info_caplog, logging.INFO)
        assert line.startswith("FMDN_FOREIGN_READING decrypted: ")
        assert line.endswith(_SUFFIX)
        assert _READING_1.reading_id in line
        assert "curve=secp256r1, Sx=32 bytes" in line
        assert FOREIGN_READING_FEEDBACK_URL in line

    def test_logger_is_below_the_integration_namespace(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        (record,) = info_caplog.records
        assert record.name.startswith("custom_components.googlefindmy.")

    def test_gate_is_not_consumed_while_info_is_hidden(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        assert len(_lines(caplog, logging.INFO)) == 1

    def test_non_provisional_reading_logs_nothing(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, SECP160R1_FOREIGN_READINGS[0], 20)
        assert info_caplog.records == []
        assert tracker.diagnostics_snapshot("entry-1")["devices"] == []

    def test_devices_are_gated_separately(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        tracker.note_success(_DEVICE_B, _READING_1, 32)
        assert len(_lines(info_caplog, logging.INFO)) == 2


class TestPreferred:
    """The winning reading is remembered per device and entry."""

    def test_preferred_follows_last_success(
        self, tracker: ForeignReadingTracker
    ) -> None:
        assert tracker.preferred(_DEVICE_A) is None
        tracker.note_success(_DEVICE_A, _READING_2, 32)
        assert tracker.preferred(_DEVICE_A) == _READING_2.reading_id
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        assert tracker.preferred(_DEVICE_A) == _READING_1.reading_id

    def test_entries_do_not_share_state(self, tracker: ForeignReadingTracker) -> None:
        tracker.note_success(("entry-1", _CANONIC_A), _READING_2, 32)
        assert tracker.preferred(("entry-2", _CANONIC_A)) is None

    def test_reset_forgets_everything(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        tracker.reset()
        assert tracker.preferred(_DEVICE_A) is None
        assert tracker.diagnostics_snapshot("entry-1")["devices"] == []
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        assert len(_lines(info_caplog, logging.INFO)) == 2


class TestForget:
    """Removing an entry or a device drops its state, and only its state (CX-6)."""

    @staticmethod
    def _fill(tracker: ForeignReadingTracker, device: tuple[str, str]) -> None:
        tracker.note_success(device, _READING_1, 32)
        tracker.note_unsupported(device, 24)

    def test_forget_device_drops_only_that_device(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
        other_entry = ("entry-2", _CANONIC_A)
        for device in (_DEVICE_A, _DEVICE_B, other_entry):
            self._fill(tracker, device)
        caplog.clear()

        tracker.forget_device("entry-1", _CANONIC_A)

        assert tracker.preferred(_DEVICE_A) is None
        assert tracker.preferred(_DEVICE_B) == _READING_1.reading_id
        assert tracker.preferred(other_entry) == _READING_1.reading_id
        assert len(tracker.diagnostics_snapshot("entry-1")["devices"]) == 1
        assert len(tracker.diagnostics_snapshot("entry-2")["devices"]) == 1
        # The once-per-device lines are re-armed for the forgotten device only.
        for device in (_DEVICE_A, _DEVICE_B):
            self._fill(tracker, device)
        assert len(_lines(caplog, logging.INFO)) == 1
        assert len(_lines(caplog, logging.WARNING)) == 1

    @pytest.mark.parametrize(
        "polls_after",
        [
            pytest.param(("poll-3", "poll-4"), id="report_count"),
            pytest.param(("poll-3", "poll-3", "poll-3"), id="poll_set"),
        ],
    )
    def test_forget_device_resets_failure_counters(
        self,
        tracker: ForeignReadingTracker,
        caplog: pytest.LogCaptureFixture,
        polls_after: tuple[str, ...],
    ) -> None:
        """Each counter alone would reach the threshold if it survived.

        ``report_count``: a kept report count makes it four reports in two
        polls. ``poll_set``: a kept poll set makes it three reports in two polls.
        """
        _fail(tracker, _DEVICE_A, "poll-1")
        _fail(tracker, _DEVICE_A, "poll-2")

        tracker.forget_device("entry-1", _CANONIC_A)
        for poll in polls_after:
            _fail(tracker, _DEVICE_A, poll)

        assert _lines(caplog, logging.WARNING) == []

    def test_forget_device_rearms_the_failure_warning(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_B, _READING_1, 32)
        for poll in ("poll-1", "poll-1", "poll-2"):
            _fail(tracker, _DEVICE_A, poll)
        assert len(_lines(caplog, logging.WARNING)) == 1

        tracker.forget_device("entry-1", _CANONIC_A)
        tracker.forget_device("entry-1", _CANONIC_B)
        for device in (_DEVICE_A, _DEVICE_B):
            for poll in ("poll-3", "poll-3", "poll-4"):
                _fail(tracker, device, poll)

        # A warns again (warn-once gate cleared), B warns at all (success cleared).
        assert len(_lines(caplog, logging.WARNING)) == 3

    def test_forget_entry_drops_only_that_entry(
        self, tracker: ForeignReadingTracker
    ) -> None:
        other_entry = ("entry-2", _CANONIC_A)
        for device in (_DEVICE_A, _DEVICE_B, other_entry):
            self._fill(tracker, device)

        tracker.forget_entry("entry-1")

        assert tracker.preferred(_DEVICE_A) is None
        assert tracker.preferred(_DEVICE_B) is None
        assert tracker.diagnostics_snapshot("entry-1")["devices"] == []
        assert tracker.preferred(other_entry) == _READING_1.reading_id
        assert len(tracker.diagnostics_snapshot("entry-2")["devices"]) == 1

    def test_forget_device_ignores_canonic_id_casing(
        self, tracker: ForeignReadingTracker
    ) -> None:
        """CX-7: the registry may hold another hex casing than the decoder saw."""
        device = foreign_device_key("entry-1", _CANONIC_A)
        self._fill(tracker, device)

        tracker.forget_device("entry-1", _CANONIC_A.upper())

        assert tracker.preferred(device) is None
        assert tracker.diagnostics_snapshot("entry-1")["devices"] == []


class TestDeviceKey:
    """One key per physical device, whatever casing the server sends (CX-7)."""

    def test_lowercases_the_canonic_id(self) -> None:
        assert foreign_device_key("entry-1", "AbCdEf-01") == ("entry-1", "abcdef-01")

    def test_keeps_a_missing_entry_id(self) -> None:
        assert foreign_device_key(None, "ABC") == (None, "abc")

    def test_methods_normalize_a_raw_key_themselves(
        self, tracker: ForeignReadingTracker
    ) -> None:
        """A caller that skips ``foreign_device_key`` still hits one entry."""
        upper = ("entry-1", _CANONIC_A.upper())
        lower = ("entry-1", _CANONIC_A)

        tracker.note_success(upper, _READING_1, 32)
        assert tracker.preferred(lower) == _READING_1.reading_id
        tracker.note_success(lower, _READING_2, 32)
        assert tracker.preferred(upper) == _READING_2.reading_id

        tracker.note_unsupported(upper, 24)
        _fail(tracker, upper, "poll-1")
        assert len(tracker.diagnostics_snapshot("entry-1")["devices"]) == 1

        tracker.forget_device("entry-1", _CANONIC_A)
        assert tracker.preferred(upper) is None


class TestAllFailedWarning:
    """WARNING for failed reports: three reports in two polls, no success."""

    def test_two_failures_in_one_poll_do_not_warn(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fail(tracker, _DEVICE_A, "poll-1")
        _fail(tracker, _DEVICE_A, "poll-1")
        assert _lines(caplog, logging.WARNING) == []

    def test_three_failures_in_one_poll_do_not_warn(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        for _ in range(3):
            _fail(tracker, _DEVICE_A, "poll-1")
        assert _lines(caplog, logging.WARNING) == []

    def test_three_failures_in_two_polls_warn_once(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        for poll in ("poll-1", "poll-1", "poll-2", "poll-3", "poll-4"):
            _fail(tracker, _DEVICE_A, poll)
        (line,) = _lines(caplog, logging.WARNING)
        assert line.startswith("FMDN_FOREIGN_READING all_failed: ")
        assert line.endswith(_SUFFIX)
        assert f"tried={','.join(_TRIED)})" in line
        assert "secp256r1 tracker" in line
        assert FOREIGN_READING_FEEDBACK_URL in line

    def test_threshold_is_exactly_three_reports_in_two_polls(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fail(tracker, _DEVICE_A, "poll-1")
        _fail(tracker, _DEVICE_A, "poll-1")
        assert _lines(caplog, logging.WARNING) == []
        _fail(tracker, _DEVICE_A, "poll-2")
        assert len(_lines(caplog, logging.WARNING)) == 1

    def test_two_reports_in_two_polls_do_not_warn(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        _fail(tracker, _DEVICE_A, "poll-1")
        _fail(tracker, _DEVICE_A, "poll-2")
        assert _lines(caplog, logging.WARNING) == []

    def test_failure_state_stays_bounded(self, tracker: ForeignReadingTracker) -> None:
        # White-box on purpose: an always-failing foreign report must not grow
        # the per-device state with every poll for the lifetime of the process.
        for poll in range(50):
            _fail(tracker, _DEVICE_A, f"poll-{poll}")
        tracker.note_success(_DEVICE_B, _READING_1, 32)
        for poll in range(50):
            _fail(tracker, _DEVICE_B, f"poll-{poll}")
        assert len(tracker._fail_polls.get(_DEVICE_A, set())) <= 2
        assert _DEVICE_B not in tracker._fail_polls

    def test_no_warning_after_a_success(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        for poll in ("poll-1", "poll-2", "poll-3"):
            _fail(tracker, _DEVICE_A, poll)
        assert _lines(caplog, logging.WARNING) == []

    def test_secp160r1_failures_are_ignored(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        for poll in ("poll-1", "poll-2", "poll-3"):
            tracker.note_all_failed(_DEVICE_A, "secp160r1", 20, ("x",), poll)
        assert _lines(caplog, logging.WARNING) == []
        assert tracker.diagnostics_snapshot("entry-1")["devices"] == []


class TestUnsupportedLength:
    """WARNING once per (device, length)."""

    def test_two_lengths_warn_twice_repeats_do_not(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        for sx_len in (24, 24, 28, 28):
            tracker.note_unsupported(_DEVICE_A, sx_len)
        lines = _lines(caplog, logging.WARNING)
        assert len(lines) == 2
        assert all(
            line.startswith("FMDN_FOREIGN_READING unsupported_length: ")
            and line.endswith(_SUFFIX)
            for line in lines
        )
        assert "a 24-byte Sx" in lines[0]


class TestNoIdentifiersInLogs:
    """No log line names a device or carries key material or locations."""

    def test_no_identifier_in_any_line(
        self, tracker: ForeignReadingTracker, info_caplog: pytest.LogCaptureFixture
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        for poll in ("poll-1", "poll-2", "poll-3"):
            _fail(tracker, _DEVICE_B, poll)
        tracker.note_unsupported(_DEVICE_B, 24)
        text = "\n".join(r.getMessage() for r in info_caplog.records)
        assert len(info_caplog.records) == 3
        for forbidden in (_CANONIC_A, _CANONIC_B, "entry-1", "canonic"):
            assert forbidden not in text
        # No hex run long enough to be a key, an EID or a scalar.
        assert re.search(r"[0-9a-fA-F]{16,}", text) is None
        # No coordinate-like decimal number.
        assert re.search(r"-?\d{1,3}\.\d{4,}", text) is None


class TestDiagnostics:
    """Diagnostics block per config entry, without ID values."""

    def test_block_lists_findings_of_one_entry(
        self, tracker: ForeignReadingTracker
    ) -> None:
        tracker.note_success(("entry-1", "b-device"), _READING_1, 32)
        tracker.note_success(("entry-1", "a-device"), _READING_1, 32)
        _fail(tracker, ("entry-1", "c-device"), "poll-1")
        tracker.note_unsupported(("entry-1", "d-device"), 24)
        tracker.note_success(("entry-2", "e-device"), _READING_2, 32)

        block = tracker.diagnostics_snapshot("entry-1")

        assert block["feedback_url"] == FOREIGN_READING_FEEDBACK_URL
        assert block["devices"] == [
            {
                "index": 0,
                "curve": "secp256r1",
                "sx_len": 32,
                "reading": _READING_1.reading_id,
                "status": "decrypted",
            },
            {
                "index": 1,
                "curve": "secp256r1",
                "sx_len": 32,
                "reading": _READING_1.reading_id,
                "status": "decrypted",
            },
            {
                "index": 2,
                "curve": "secp256r1",
                "sx_len": 32,
                "reading": None,
                "status": "all_failed",
            },
            {
                "index": 3,
                "curve": None,
                "sx_len": 24,
                "reading": None,
                "status": "unsupported_length",
            },
        ]
        dumped = json.dumps(block)
        for canonic_id in ("a-device", "b-device", "c-device", "d-device", "e-device"):
            assert canonic_id not in dumped

    def test_success_is_not_overwritten_by_a_later_failure(
        self, tracker: ForeignReadingTracker
    ) -> None:
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        _fail(tracker, _DEVICE_A, "poll-1")
        tracker.note_unsupported(_DEVICE_A, 24)
        (device,) = tracker.diagnostics_snapshot("entry-1")["devices"]
        assert device["status"] == "decrypted"

    def test_success_replaces_an_earlier_failure(
        self, tracker: ForeignReadingTracker
    ) -> None:
        _fail(tracker, _DEVICE_A, "poll-1")
        tracker.note_success(_DEVICE_A, _READING_1, 32)
        (device,) = tracker.diagnostics_snapshot("entry-1")["devices"]
        assert device["status"] == "decrypted"
        assert device["reading"] == _READING_1.reading_id

    def test_public_wrapper_reads_the_process_tracker(self) -> None:
        FOREIGN_READING_TRACKER.note_success(("entry-9", "x"), _READING_1, 32)
        (device,) = get_foreign_reading_diagnostics("entry-9")["devices"]
        assert device["reading"] == _READING_1.reading_id
        assert get_foreign_reading_diagnostics("entry-other")["devices"] == []


class TestCurveProvider:
    """The lookup through which the decryption path learns a locked curve."""

    def test_no_curve_without_a_provider(self, tracker: ForeignReadingTracker) -> None:
        assert tracker.locked_curve(_CANONIC_A) is None

    def test_the_provider_answers_with_the_raw_id(
        self, tracker: ForeignReadingTracker
    ) -> None:
        asked: list[str] = []
        tracker.set_curve_provider(lambda cid: asked.append(cid) or "secp256r1")
        assert tracker.locked_curve("Canonic-A") == "secp256r1"
        assert asked == ["Canonic-A"]

    def test_a_failing_provider_gives_none_and_a_debug_line(
        self, tracker: ForeignReadingTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(_cid: str) -> str | None:
            raise RuntimeError("boom")

        tracker.set_curve_provider(broken)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert tracker.locked_curve(_CANONIC_A) is None
        assert any("Curve lookup" in r.getMessage() for r in caplog.records)

    def test_clear_removes_only_the_registered_provider(
        self, tracker: ForeignReadingTracker
    ) -> None:
        def first(_cid: str) -> str | None:
            return "secp160r1"

        def second(_cid: str) -> str | None:
            return "secp256r1"

        tracker.set_curve_provider(second)
        tracker.clear_curve_provider(first)
        assert tracker.locked_curve(_CANONIC_A) == "secp256r1"
        tracker.clear_curve_provider(second)
        assert tracker.locked_curve(_CANONIC_A) is None

    def test_clear_matches_a_fresh_bound_method(
        self, tracker: ForeignReadingTracker
    ) -> None:
        """Each attribute access builds a new bound method; equality must hold."""

        class Owner:
            def lookup(self, _cid: str) -> str | None:
                return "secp256r1"

        owner = Owner()
        tracker.set_curve_provider(owner.lookup)
        tracker.clear_curve_provider(owner.lookup)
        assert tracker.locked_curve(_CANONIC_A) is None

    def test_reset_removes_the_provider(self, tracker: ForeignReadingTracker) -> None:
        tracker.set_curve_provider(lambda _cid: "secp256r1")
        tracker.reset()
        assert tracker.locked_curve(_CANONIC_A) is None

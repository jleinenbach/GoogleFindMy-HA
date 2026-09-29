# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/foreign_reading_tracker.py
"""Per-device memory and feedback logs for provisional foreign-report readings.

P-256 crowdsourced reports are decrypted by trying several provisional
readings (``FMDNCrypto/foreign_tracker_cryptor.py``). No public test vector
tells which reading real trackers use, so users are asked to post the reading
that worked. This module is the single place that

* remembers, per device, the reading that decrypted the last report so the
  next report tries it first;
* writes the feedback lines, each once per process: an INFO line naming a
  provisional reading that decrypted a report, a WARNING when reports keep
  failing with every reading, and a WARNING for an ``Sx`` length that matches
  no curve;
* keeps the findings for the diagnostics download.

Log lines start with ``FMDN_FOREIGN_READING <status>`` so users can search for
them, name no device and carry no key material, ID or location; they are meant
to be posted publicly. Devices are keyed internally by
``(entry_id, canonic_id)`` so two config entries never share state.

State is module-global on purpose: the decryption path has no ``hass`` object
to hang it on (same precedent as the EIK cache). It lives for the process, so
after a restart each line appears once more if the situation persists.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Final, TypedDict

from custom_components.googlefindmy.FMDNCrypto.curve_profile import SECP160R1
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    FOREIGN_READING_FEEDBACK_URL,
    ForeignReading,
)

__all__ = [
    "FOREIGN_READING_TRACKER",
    "STATUS_ALL_FAILED",
    "STATUS_DECRYPTED",
    "STATUS_UNSUPPORTED_LENGTH",
    "DeviceKey",
    "ForeignReadingDevice",
    "ForeignReadingDiagnostics",
    "ForeignReadingTracker",
    "get_foreign_reading_diagnostics",
]

_LOGGER = logging.getLogger(__name__)

#: ``(entry_id, canonic_id)``; ``entry_id`` is ``None`` for caches without one.
type DeviceKey = tuple[str | None, str]

#: A failed-report WARNING needs this many failed reports ...
_FAIL_REPORTS_BEFORE_WARNING: Final[int] = 3
#: ... spread over at least this many polls (legitimate failures happen, e.g.
#: a report encrypted for a key of another account).
_FAIL_POLLS_BEFORE_WARNING: Final[int] = 2

#: Closed value set of ``status`` in logs and diagnostics.
STATUS_DECRYPTED: Final[str] = "decrypted"
STATUS_ALL_FAILED: Final[str] = "all_failed"
STATUS_UNSUPPORTED_LENGTH: Final[str] = "unsupported_length"


class ForeignReadingDevice(TypedDict):
    """One device in the diagnostics block (no ID value)."""

    index: int
    curve: str | None
    sx_len: int
    reading: str | None
    status: str


class ForeignReadingDiagnostics(TypedDict):
    """The ``foreign_report_readings`` diagnostics block of one config entry."""

    feedback_url: str
    devices: list[ForeignReadingDevice]


@dataclass(slots=True)
class _Finding:
    """Latest diagnostics finding for one device."""

    curve: str | None
    sx_len: int
    reading: str | None
    status: str


class ForeignReadingTracker:
    """Remember winning readings and write each feedback line once per device."""

    def __init__(self) -> None:
        """Create an empty tracker."""
        self._lock = threading.Lock()
        self._preferred: dict[DeviceKey, str] = {}
        self._logged: set[tuple[DeviceKey, str]] = set()
        self._success: set[DeviceKey] = set()
        self._fail_reports: dict[DeviceKey, int] = {}
        self._fail_polls: dict[DeviceKey, set[Hashable]] = {}
        self._warned_fail: set[DeviceKey] = set()
        self._warned_len: set[tuple[DeviceKey, int]] = set()
        self._findings: dict[DeviceKey, _Finding] = {}

    def reset(self) -> None:
        """Forget everything (test isolation)."""
        with self._lock:
            self._preferred.clear()
            self._logged.clear()
            self._success.clear()
            self._fail_reports.clear()
            self._fail_polls.clear()
            self._warned_fail.clear()
            self._warned_len.clear()
            self._findings.clear()

    def preferred(self, device_key: DeviceKey) -> str | None:
        """Return the ``reading_id`` that decrypted this device's last report.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device.

        Returns:
            The reading to try first, or ``None`` if none decrypted yet.
        """
        with self._lock:
            return self._preferred.get(device_key)

    def note_success(
        self, device_key: DeviceKey, reading: ForeignReading, sx_len: int
    ) -> None:
        """Record a decrypted report; log a provisional reading once per device.

        The once-gate is set only when the INFO line is actually emitted. Home
        Assistant hides INFO by default; a gate consumed by an invisible line
        would suppress the line after the user enabled INFO. A success
        replaces an earlier failure finding in the diagnostics.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device.
            reading: The reading whose tag verified.
            sx_len: Length of the report's ``Sx`` coordinate in bytes.
        """
        reading_id = reading.reading_id
        with self._lock:
            self._preferred[device_key] = reading_id
            if not reading.provisional:
                return
            self._success.add(device_key)
            self._findings[device_key] = _Finding(
                curve=reading.curve.name,
                sx_len=sx_len,
                reading=reading_id,
                status=STATUS_DECRYPTED,
            )
            if (device_key, reading_id) in self._logged:  # once-gate
                return
            if not _LOGGER.isEnabledFor(logging.INFO):
                return
            self._logged.add((device_key, reading_id))
        _LOGGER.info(
            "FMDN_FOREIGN_READING decrypted: a crowdsourced report from a P-256 "
            "tracker was decrypted with provisional reading %s (curve=%s, "
            "Sx=%d bytes). Please post this line to %s so unused readings can "
            "be removed. It contains no keys, IDs or locations.",
            reading_id,
            reading.curve.name,
            sx_len,
            FOREIGN_READING_FEEDBACK_URL,
        )

    def note_all_failed(
        self,
        device_key: DeviceKey,
        curve_name: str,
        sx_len: int,
        readings_tried: tuple[str, ...],
        poll_id: Hashable,
    ) -> None:
        """Record a report no key and no reading authenticated.

        Warns at most once per device, and only after three failed reports in
        at least two polls while no report of the device has decrypted. The
        diagnostics finding already shows the latest failure after one report;
        it describes the last report, the log line is the thresholded verdict.
        SECP160R1 failures are ignored here; the caller accounts for them.
        Counting stops once the device has decrypted or warned, and at most
        two poll IDs are kept per device, so the state stays bounded.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device.
            curve_name: ``FmdnCurve.name`` selected from the ``Sx`` length.
            sx_len: Length of the report's ``Sx`` coordinate in bytes.
            readings_tried: Identifiers of the readings that were tried.
            poll_id: Token of the poll or push that delivered the report.
        """
        if curve_name == SECP160R1.name:
            return
        with self._lock:
            if device_key not in self._success:
                self._findings[device_key] = _Finding(
                    curve=curve_name,
                    sx_len=sx_len,
                    reading=None,
                    status=STATUS_ALL_FAILED,
                )
            if device_key in self._success:  # warn-until-success
                return
            if device_key in self._warned_fail:
                return
            reports = self._fail_reports.get(device_key, 0) + 1
            self._fail_reports[device_key] = reports
            polls = self._fail_polls.setdefault(device_key, set())
            if len(polls) < _FAIL_POLLS_BEFORE_WARNING:
                polls.add(poll_id)
            if (
                reports < _FAIL_REPORTS_BEFORE_WARNING
                or len(polls) < _FAIL_POLLS_BEFORE_WARNING
            ):
                return
            self._warned_fail.add(device_key)
        _LOGGER.warning(
            "FMDN_FOREIGN_READING all_failed: crowdsourced reports from a %s "
            "tracker could not be authenticated with any known reading "
            "(Sx=%d bytes, tried=%s). Please post this line to %s. It contains "
            "no keys, IDs or locations.",
            curve_name,
            sx_len,
            ",".join(readings_tried),
            FOREIGN_READING_FEEDBACK_URL,
        )

    def note_unsupported(self, device_key: DeviceKey, sx_len: int) -> None:
        """Record an ``Sx`` length that matches no curve; warn once per length.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device.
            sx_len: The unsupported ``Sx`` length in bytes.
        """
        with self._lock:
            if device_key not in self._success:
                self._findings[device_key] = _Finding(
                    curve=None,
                    sx_len=sx_len,
                    reading=None,
                    status=STATUS_UNSUPPORTED_LENGTH,
                )
            if (device_key, sx_len) in self._warned_len:  # len-gate
                return
            self._warned_len.add((device_key, sx_len))
        _LOGGER.warning(
            "FMDN_FOREIGN_READING unsupported_length: crowdsourced reports carry "
            "a %d-byte Sx, which matches no supported curve. Please post this "
            "line to %s. It contains no keys, IDs or locations.",
            sx_len,
            FOREIGN_READING_FEEDBACK_URL,
        )

    def diagnostics_snapshot(self, entry_id: str | None) -> ForeignReadingDiagnostics:
        """Return the diagnostics block for one config entry.

        Only devices of ``entry_id`` with a P-256 or unknown-length finding
        are listed. ``index`` is the position of the device among the listed
        devices of this entry, sorted by ``canonic_id``. It is deliberately
        not the per-device index of other diagnostics blocks: the tracker does
        not know the entry's device list, and this block is meant to be posted
        on its own. No ID value is included, so the block survives redaction
        by key name unchanged.

        Args:
            entry_id: Config entry whose devices are listed.

        Returns:
            ``feedback_url`` and the list of device findings.
        """
        with self._lock:
            listed = sorted(
                (
                    (key[1], finding)
                    for key, finding in self._findings.items()
                    if key[0] == entry_id
                ),
                key=lambda item: item[0],
            )
        return {
            "feedback_url": FOREIGN_READING_FEEDBACK_URL,
            "devices": [
                {
                    "index": index,
                    "curve": finding.curve,
                    "sx_len": finding.sx_len,
                    "reading": finding.reading,
                    "status": finding.status,
                }
                for index, (_canonic_id, finding) in enumerate(listed)
            ],
        }


#: Process-wide tracker used by the decryption path and the diagnostics.
FOREIGN_READING_TRACKER: Final[ForeignReadingTracker] = ForeignReadingTracker()


def get_foreign_reading_diagnostics(entry_id: str | None) -> ForeignReadingDiagnostics:
    """Return the ``foreign_report_readings`` diagnostics block for an entry.

    Args:
        entry_id: Config entry whose devices are listed.

    Returns:
        The block described in ``ForeignReadingTracker.diagnostics_snapshot``.
    """
    return FOREIGN_READING_TRACKER.diagnostics_snapshot(entry_id)

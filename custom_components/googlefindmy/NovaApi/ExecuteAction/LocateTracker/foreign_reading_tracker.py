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
to hang it on (same precedent as the EIK cache). It survives reloads and lives
for the process, so after a restart each line appears once more if the
situation persists. Removing a config entry or a device drops its state.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Hashable
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
    "foreign_device_key",
    "get_foreign_reading_diagnostics",
]

_LOGGER = logging.getLogger(__name__)

#: ``(entry_id, canonic_id)``; ``entry_id`` is ``None`` for caches without one.
type DeviceKey = tuple[str | None, str]


def foreign_device_key(entry_id: str | None, canonic_id: str) -> DeviceKey:
    """Return the tracker key of one device.

    The canonical ID is lowercased, as everywhere else in the integration: the
    server may change the hex casing of the same ID between responses
    (``custom_components/googlefindmy/AGENTS.md``), and the device registry
    keeps whatever casing it saw first. Every key goes through here so the
    decoder and the removal paths always agree.

    Args:
        entry_id: Config entry of the token cache, or ``None``.
        canonic_id: Canonical ID in any casing.

    Returns:
        The key used for all per-device state.
    """
    return (entry_id, canonic_id.lower())


def _normalized(device_key: DeviceKey) -> DeviceKey:
    """Return ``device_key`` with its canonical ID lowercased.

    The tracker methods apply this themselves, so a caller that builds the
    tuple by hand cannot split one device into two entries.
    """
    return foreign_device_key(*device_key)


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
        # Lookup of the curve a device's EID is locked to, registered by the
        # EID resolver (see set_curve_provider).
        self._curve_provider: Callable[[str], str | None] | None = None
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
            self._curve_provider = None

    def set_curve_provider(self, provider: Callable[[str], str | None]) -> None:
        """Register the lookup that names the curve a device's EID is locked to.

        The EID resolver registers its ``locked_curve_name`` when it starts. The
        decryption path has no public route to ``hass`` (it receives only the
        token cache), so it asks through this tracker, in the same way the Nova
        decryptor reaches its cache through ``nova_request.register_cache_provider``.

        Args:
            provider: Callable mapping a canonical ID to a curve name or ``None``.
        """
        with self._lock:
            self._curve_provider = provider

    def clear_curve_provider(self, provider: Callable[[str], str | None]) -> None:
        """Remove ``provider`` if it is still the registered lookup.

        Bound methods are compared with ``==``: every attribute access creates a
        new bound-method object, so an identity check would never match.

        Args:
            provider: The lookup passed to :meth:`set_curve_provider`.
        """
        with self._lock:
            if self._curve_provider == provider:
                self._curve_provider = None

    def locked_curve(self, canonic_id: str) -> str | None:
        """Return the curve name of the EID variant ``canonic_id`` is locked to.

        Args:
            canonic_id: Canonical ID in any casing.

        Returns:
            The curve name, or ``None`` without a registered lookup, without a
            lock, or when the lookup raises; callers then keep the behaviour
            they had before a lock was known.
        """
        with self._lock:
            provider = self._curve_provider
        if provider is None:
            return None
        try:
            return provider(canonic_id)
        except Exception as err:  # noqa: BLE001 - a lookup must never break decryption
            _LOGGER.debug("Curve lookup for a locked EID failed: %s", err)
            return None

    def forget_entry(self, entry_id: str | None) -> None:
        """Forget every device of a removed config entry.

        Called when the entry is removed, not when it unloads: the remembered
        reading and the once-per-device lines are meant to survive a reload.

        Args:
            entry_id: Config entry whose devices are dropped.
        """
        with self._lock:
            self._forget_where(lambda key: key[0] == entry_id)

    def forget_device(self, entry_id: str | None, canonic_id: str) -> None:
        """Forget one device removed from its config entry.

        Args:
            entry_id: Config entry the device belonged to.
            canonic_id: Canonical ID of the removed device, in any casing.
        """
        device_key = foreign_device_key(entry_id, canonic_id)
        with self._lock:
            self._forget_where(lambda key: key == device_key)

    def _forget_where(self, matches: Callable[[DeviceKey], bool]) -> None:
        """Drop the state of every device key ``matches`` accepts (lock held)."""
        _drop_keys(self._preferred, matches)
        _drop_keys(self._fail_reports, matches)
        _drop_keys(self._fail_polls, matches)
        _drop_keys(self._findings, matches)
        self._success.difference_update({key for key in self._success if matches(key)})
        self._warned_fail.difference_update(
            {key for key in self._warned_fail if matches(key)}
        )
        self._logged.difference_update(
            {gate for gate in self._logged if matches(gate[0])}
        )
        self._warned_len.difference_update(
            {gate for gate in self._warned_len if matches(gate[0])}
        )

    def preferred(self, device_key: DeviceKey) -> str | None:
        """Return the ``reading_id`` that decrypted this device's last report.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device; the
                canonical ID may come in any casing.

        Returns:
            The reading to try first, or ``None`` if none decrypted yet.
        """
        device_key = _normalized(device_key)
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
            device_key: ``(entry_id, canonic_id)`` of the device; the
                canonical ID may come in any casing.
            reading: The reading whose tag verified.
            sx_len: Length of the report's ``Sx`` coordinate in bytes.
        """
        device_key = _normalized(device_key)
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
        diagnostics finding already shows the latest failure after one report,
        until a report of the device decrypts; from then on it keeps the
        success, because the block answers which reading works. The log line
        is the thresholded verdict.
        SECP160R1 failures are ignored here; the caller accounts for them.
        Counting stops once the device has decrypted or warned, and at most
        two poll IDs are kept per device, so the state stays bounded.

        Args:
            device_key: ``(entry_id, canonic_id)`` of the device; the
                canonical ID may come in any casing.
            curve_name: ``FmdnCurve.name`` selected from the ``Sx`` length.
            sx_len: Length of the report's ``Sx`` coordinate in bytes.
            readings_tried: Identifiers of the readings that were tried.
            poll_id: Token of the poll or push that delivered the report.
        """
        if curve_name == SECP160R1.name:
            return
        device_key = _normalized(device_key)
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
            device_key: ``(entry_id, canonic_id)`` of the device; the
                canonical ID may come in any casing.
            sx_len: The unsupported ``Sx`` length in bytes.
        """
        device_key = _normalized(device_key)
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


def _drop_keys[V](
    mapping: dict[DeviceKey, V], matches: Callable[[DeviceKey], bool]
) -> None:
    """Delete every key of ``mapping`` that ``matches`` accepts."""
    for key in [key for key in mapping if matches(key)]:
        del mapping[key]


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

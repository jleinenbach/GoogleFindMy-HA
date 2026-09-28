# custom_components/googlefindmy/FMDNCrypto/foreign_report_errors.py
"""Error taxonomy for decrypting crowdsourced (foreign) FMDN location reports.

A foreign report is encrypted by a finder device to the tracker's public
identity. Decrypting it can fail for three unrelated reasons, and callers must
be able to tell them apart by type, never by the text of an exception message:

* ``ForeignReportStructureError``: the report itself is unusable (wrong length,
  a point that is not on the curve). No key candidate can change that, so the
  check runs once, before any key is tried.
* ``UnsupportedCurveError``: a structure error whose cause is an ``Sx`` length
  that maps to no supported curve. It carries ``sx_len`` so the caller can
  report the length without parsing a message.
* ``ForeignReportAuthError``: every key and every reading was tried and the
  AES-EAX tag verified for none of them.

The family deliberately derives from ``ValueError`` and not from the owner-key
``DecryptionError`` (a ``RuntimeError`` that drives the reauthentication path):
a foreign report that cannot be read says nothing about the account's
credentials. ``ValueError`` keeps the documented contract of
``foreign_tracker_cryptor.decrypt`` ("Raises ValueError").

This module imports nothing from the integration so every layer can depend on
it without creating an import cycle.
"""

from __future__ import annotations

__all__ = [
    "ForeignReportAuthError",
    "ForeignReportError",
    "ForeignReportStructureError",
    "UnsupportedCurveError",
]


class ForeignReportError(ValueError):
    """Base class for every failure to decrypt a foreign location report."""


class ForeignReportStructureError(ForeignReportError):
    """The report is malformed independently of any key candidate."""


class UnsupportedCurveError(ForeignReportStructureError):
    """The ``Sx`` length maps to no supported FMDN curve.

    Attributes:
        sx_len: Length of the received ``Sx`` coordinate in bytes.
    """

    def __init__(self, sx_len: int) -> None:
        """Store the offending length and build a message that names it."""
        self.sx_len: int = sx_len
        super().__init__(f"Sx length {sx_len} bytes matches no supported FMDN curve")


class ForeignReportAuthError(ForeignReportError):
    """No key candidate and no reading produced a verifying AES-EAX tag.

    Attributes:
        curve_name: Name of the curve selected from the ``Sx`` length.
        readings_tried: Identifiers of the readings that were tried, in order.
        keys_tried: Number of identity key candidates that were tried.
    """

    def __init__(
        self, curve_name: str, readings_tried: tuple[str, ...], keys_tried: int
    ) -> None:
        """Store the attempt summary and build a message without key material."""
        self.curve_name: str = curve_name
        self.readings_tried: tuple[str, ...] = readings_tried
        self.keys_tried: int = keys_tried
        super().__init__(
            f"No reading authenticated the report on {curve_name} "
            f"({len(readings_tried)} readings x {keys_tried} keys)"
        )

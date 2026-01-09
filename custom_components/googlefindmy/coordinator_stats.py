"""Statistics and diagnostics utilities for the coordinator.

This module contains pure classes and functions extracted from coordinator.py
for improved testability and maintainability (Phase 2 of refactoring).

Contents:
- DiagnosticsBuffer: Bounded, deduplicated buffer for warnings/errors
- StatusSnapshot: Lightweight status descriptor
- ApiStatus: Polling state constants
- FcmStatus: Push transport health constants
- short_error_message(): Compact error string formatting
- get_duration(): Duration calculation helper
- format_recent_errors(): Error list formatting
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Status Constants
# ---------------------------------------------------------------------------


class ApiStatus:
    """String constants describing the coordinator's polling state."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"
    REAUTH = "reauth_required"


class FcmStatus:
    """String constants representing push-transport health."""

    UNKNOWN = "unknown"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


# ---------------------------------------------------------------------------
# Status Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSnapshot:
    """Lightweight status descriptor shared with diagnostic entities/tests."""

    state: str
    reason: str | None = None
    changed_at: float | None = None


# ---------------------------------------------------------------------------
# Diagnostics Buffer
# ---------------------------------------------------------------------------


@dataclass
class DiagnosticsBuffer:
    """Bounded, deduplicated in-memory buffer for warnings and errors.

    This buffer is used to expose runtime findings via diagnostics.py.
    Sensitive data MUST NOT be stored here (no tokens, no coordinates).
    The buffer is intentionally small and deduplicated to avoid large dumps.
    """

    max_items: int = 200
    warnings: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _add(
        self, bucket: dict[str, dict[str, Any]], key: str, payload: dict[str, Any]
    ) -> None:
        """Add payload once (dedup by key); bounded to max_items."""
        if key in bucket:
            return
        if len(bucket) >= self.max_items:
            return
        bucket[key] = payload

    def add_warning(self, code: str, context: dict[str, Any]) -> None:
        """Record a warning with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id', '?')}"
        self._add(self.warnings, key, context)

    def add_error(self, code: str, context: dict[str, Any]) -> None:
        """Record an error with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id', '?')}:{context.get('arg', '')}"
        self._add(self.errors, key, context)

    def to_dict(self) -> dict[str, Any]:
        """Return a minimal, redacted, diagnostics-friendly dict."""
        return {
            "summary": {"warnings": len(self.warnings), "errors": len(self.errors)},
            "warnings": list(self.warnings.values()),
            "errors": list(self.errors.values()),
        }


# ---------------------------------------------------------------------------
# Error Message Formatting
# ---------------------------------------------------------------------------

# Maximum length for error messages before truncation
_MAX_ERROR_MESSAGE_LENGTH = 180


def short_error_message(exc: Exception | str) -> str:
    """Return a compact, single-line error string.

    Normalizes whitespace (collapses newlines, tabs, multiple spaces)
    and truncates to 180 characters with '...' suffix if needed.

    Args:
        exc: Exception instance or string to format.

    Returns:
        Compact error message string.
    """
    msg = str(exc)
    # Collapse newlines/whitespace to single spaces
    msg = " ".join(msg.split())
    # Bound message length
    if len(msg) > _MAX_ERROR_MESSAGE_LENGTH:
        msg = msg[: _MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
    return msg


# ---------------------------------------------------------------------------
# Duration Calculation
# ---------------------------------------------------------------------------


def get_duration(
    start: float | int | str | None, end: float | int | str | None
) -> float | None:
    """Calculate duration between two timestamps.

    Args:
        start: Start timestamp (numeric or numeric string).
        end: End timestamp (numeric or numeric string).

    Returns:
        Duration in seconds (never negative), or None if either input is invalid.
    """
    if start is None or end is None:
        return None
    try:
        start_val = float(start)
        end_val = float(end)
        return max(0.0, end_val - start_val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Error List Formatting
# ---------------------------------------------------------------------------


def format_recent_errors(
    errors: Sequence[tuple[float, str, str]],
) -> list[dict[str, Any]]:
    """Format error tuples as JSON-friendly list of dicts.

    Args:
        errors: Sequence of (timestamp, error_type, message) tuples.

    Returns:
        List of dicts with 'timestamp', 'error_type', 'message' keys.
    """
    out: list[dict[str, Any]] = []
    for ts, et, msg in list(errors):
        out.append({"timestamp": ts, "error_type": et, "message": msg})
    return out

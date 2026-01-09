"""Tests for coordinator_stats.py - Phase 2 of coordinator refactoring.

These tests validate the statistics and diagnostics utilities extracted
from coordinator.py into coordinator_stats.py.

Test categories:
1. DiagnosticsBuffer - Bounded, deduplicated buffer for warnings/errors
2. StatusSnapshot - Lightweight status descriptor
3. ApiStatus / FcmStatus - Status constants
4. short_error_message - Error message formatting
5. get_duration - Duration calculation
6. format_recent_errors - Error list formatting

REQUIREMENT: 100% test coverage for all extracted classes and functions.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator.helpers.stats import (
    ApiStatus,
    DiagnosticsBuffer,
    FcmStatus,
    StatusSnapshot,
    format_recent_errors,
    get_duration,
    short_error_message,
)

# ---------------------------------------------------------------------------
# DiagnosticsBuffer Tests
# ---------------------------------------------------------------------------


class TestDiagnosticsBuffer:
    """Tests for DiagnosticsBuffer class."""

    def test_default_max_items(self) -> None:
        """Default max_items is 200."""
        buf = DiagnosticsBuffer()
        assert buf.max_items == 200

    def test_custom_max_items(self) -> None:
        """Custom max_items can be set."""
        buf = DiagnosticsBuffer(max_items=50)
        assert buf.max_items == 50

    def test_empty_buffer_to_dict(self) -> None:
        """Empty buffer returns correct structure."""
        buf = DiagnosticsBuffer()
        result = buf.to_dict()
        assert result == {
            "summary": {"warnings": 0, "errors": 0},
            "warnings": [],
            "errors": [],
        }

    def test_add_warning(self) -> None:
        """add_warning adds to warnings bucket."""
        buf = DiagnosticsBuffer()
        buf.add_warning("TEST_CODE", {"device_id": "dev1", "detail": "test"})
        assert len(buf.warnings) == 1
        assert buf.errors == {}

    def test_add_error(self) -> None:
        """add_error adds to errors bucket."""
        buf = DiagnosticsBuffer()
        buf.add_error("ERR_CODE", {"device_id": "dev1", "arg": "some_arg"})
        assert len(buf.errors) == 1
        assert buf.warnings == {}

    def test_warning_deduplication(self) -> None:
        """Warnings with same code and device_id are deduplicated."""
        buf = DiagnosticsBuffer()
        buf.add_warning("TEST_CODE", {"device_id": "dev1"})
        buf.add_warning("TEST_CODE", {"device_id": "dev1"})
        assert len(buf.warnings) == 1

    def test_warning_different_devices_not_deduplicated(self) -> None:
        """Warnings with same code but different device_id are kept."""
        buf = DiagnosticsBuffer()
        buf.add_warning("TEST_CODE", {"device_id": "dev1"})
        buf.add_warning("TEST_CODE", {"device_id": "dev2"})
        assert len(buf.warnings) == 2

    def test_error_deduplication(self) -> None:
        """Errors with same code, device_id, and arg are deduplicated."""
        buf = DiagnosticsBuffer()
        buf.add_error("ERR_CODE", {"device_id": "dev1", "arg": "x"})
        buf.add_error("ERR_CODE", {"device_id": "dev1", "arg": "x"})
        assert len(buf.errors) == 1

    def test_error_different_args_not_deduplicated(self) -> None:
        """Errors with same code/device but different arg are kept."""
        buf = DiagnosticsBuffer()
        buf.add_error("ERR_CODE", {"device_id": "dev1", "arg": "x"})
        buf.add_error("ERR_CODE", {"device_id": "dev1", "arg": "y"})
        assert len(buf.errors) == 2

    def test_max_items_limit_warnings(self) -> None:
        """Warnings are bounded by max_items."""
        buf = DiagnosticsBuffer(max_items=3)
        for i in range(10):
            buf.add_warning("CODE", {"device_id": f"dev{i}"})
        assert len(buf.warnings) == 3

    def test_max_items_limit_errors(self) -> None:
        """Errors are bounded by max_items."""
        buf = DiagnosticsBuffer(max_items=3)
        for i in range(10):
            buf.add_error("CODE", {"device_id": f"dev{i}", "arg": str(i)})
        assert len(buf.errors) == 3

    def test_to_dict_with_data(self) -> None:
        """to_dict returns correct structure with data."""
        buf = DiagnosticsBuffer()
        buf.add_warning("WARN1", {"device_id": "d1", "info": "w1"})
        buf.add_warning("WARN2", {"device_id": "d2", "info": "w2"})
        buf.add_error("ERR1", {"device_id": "d3", "arg": "e1"})

        result = buf.to_dict()
        assert result["summary"] == {"warnings": 2, "errors": 1}
        assert len(result["warnings"]) == 2
        assert len(result["errors"]) == 1

    def test_warning_missing_device_id(self) -> None:
        """Warnings without device_id use '?' as fallback."""
        buf = DiagnosticsBuffer()
        buf.add_warning("CODE", {"other": "data"})
        # Key should be "CODE:?"
        assert "CODE:?" in buf.warnings

    def test_error_missing_device_id_and_arg(self) -> None:
        """Errors without device_id or arg use fallbacks."""
        buf = DiagnosticsBuffer()
        buf.add_error("CODE", {"other": "data"})
        # Key should be "CODE:?:"
        assert "CODE:?:" in buf.errors

    def test_add_internal_method(self) -> None:
        """_add method handles edge cases correctly."""
        buf = DiagnosticsBuffer(max_items=2)
        bucket: dict[str, dict[str, Any]] = {}

        # Add first item
        buf._add(bucket, "key1", {"data": 1})
        assert len(bucket) == 1

        # Add duplicate (should be ignored)
        buf._add(bucket, "key1", {"data": 2})
        assert len(bucket) == 1
        assert bucket["key1"]["data"] == 1  # Original preserved

        # Add second item
        buf._add(bucket, "key2", {"data": 2})
        assert len(bucket) == 2

        # Add third item (exceeds max_items, should be ignored)
        buf._add(bucket, "key3", {"data": 3})
        assert len(bucket) == 2
        assert "key3" not in bucket


# ---------------------------------------------------------------------------
# StatusSnapshot Tests
# ---------------------------------------------------------------------------


class TestStatusSnapshot:
    """Tests for StatusSnapshot dataclass."""

    def test_create_with_state_only(self) -> None:
        """StatusSnapshot can be created with just state."""
        snap = StatusSnapshot(state="ok")
        assert snap.state == "ok"
        assert snap.reason is None
        assert snap.changed_at is None

    def test_create_with_all_fields(self) -> None:
        """StatusSnapshot can be created with all fields."""
        snap = StatusSnapshot(state="error", reason="timeout", changed_at=123.456)
        assert snap.state == "error"
        assert snap.reason == "timeout"
        assert snap.changed_at == 123.456

    def test_frozen(self) -> None:
        """StatusSnapshot is immutable (frozen)."""
        snap = StatusSnapshot(state="ok")
        with pytest.raises(AttributeError):
            snap.state = "error"  # type: ignore[misc]

    def test_equality(self) -> None:
        """StatusSnapshot supports equality comparison."""
        snap1 = StatusSnapshot(state="ok", reason="test")
        snap2 = StatusSnapshot(state="ok", reason="test")
        snap3 = StatusSnapshot(state="error", reason="test")
        assert snap1 == snap2
        assert snap1 != snap3


# ---------------------------------------------------------------------------
# ApiStatus Tests
# ---------------------------------------------------------------------------


class TestApiStatus:
    """Tests for ApiStatus constants."""

    def test_unknown(self) -> None:
        """UNKNOWN constant is 'unknown'."""
        assert ApiStatus.UNKNOWN == "unknown"

    def test_ok(self) -> None:
        """OK constant is 'ok'."""
        assert ApiStatus.OK == "ok"

    def test_error(self) -> None:
        """ERROR constant is 'error'."""
        assert ApiStatus.ERROR == "error"

    def test_reauth(self) -> None:
        """REAUTH constant is 'reauth_required'."""
        assert ApiStatus.REAUTH == "reauth_required"

    def test_all_values_unique(self) -> None:
        """All ApiStatus values are unique."""
        values = [ApiStatus.UNKNOWN, ApiStatus.OK, ApiStatus.ERROR, ApiStatus.REAUTH]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# FcmStatus Tests
# ---------------------------------------------------------------------------


class TestFcmStatus:
    """Tests for FcmStatus constants."""

    def test_unknown(self) -> None:
        """UNKNOWN constant is 'unknown'."""
        assert FcmStatus.UNKNOWN == "unknown"

    def test_connected(self) -> None:
        """CONNECTED constant is 'connected'."""
        assert FcmStatus.CONNECTED == "connected"

    def test_degraded(self) -> None:
        """DEGRADED constant is 'degraded'."""
        assert FcmStatus.DEGRADED == "degraded"

    def test_disconnected(self) -> None:
        """DISCONNECTED constant is 'disconnected'."""
        assert FcmStatus.DISCONNECTED == "disconnected"

    def test_all_values_unique(self) -> None:
        """All FcmStatus values are unique."""
        values = [
            FcmStatus.UNKNOWN,
            FcmStatus.CONNECTED,
            FcmStatus.DEGRADED,
            FcmStatus.DISCONNECTED,
        ]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# short_error_message Tests
# ---------------------------------------------------------------------------


class TestShortErrorMessage:
    """Tests for short_error_message function."""

    def test_simple_string(self) -> None:
        """Simple string is returned as-is."""
        result = short_error_message("simple error")
        assert result == "simple error"

    def test_exception_converted(self) -> None:
        """Exception is converted to string."""
        exc = ValueError("test error")
        result = short_error_message(exc)
        assert result == "test error"

    def test_newlines_collapsed(self) -> None:
        """Newlines are collapsed to single space."""
        result = short_error_message("line1\nline2\nline3")
        assert result == "line1 line2 line3"

    def test_multiple_spaces_collapsed(self) -> None:
        """Multiple spaces are collapsed to single space."""
        result = short_error_message("word1   word2    word3")
        assert result == "word1 word2 word3"

    def test_tabs_collapsed(self) -> None:
        """Tabs are collapsed to single space."""
        result = short_error_message("word1\t\tword2")
        assert result == "word1 word2"

    def test_mixed_whitespace(self) -> None:
        """Mixed whitespace is normalized."""
        result = short_error_message("  word1\n\t  word2  \n")
        assert result == "word1 word2"

    def test_truncation_at_180(self) -> None:
        """Messages longer than 180 chars are truncated."""
        long_msg = "x" * 200
        result = short_error_message(long_msg)
        assert len(result) == 180
        assert result.endswith("...")

    def test_exactly_180_not_truncated(self) -> None:
        """Messages exactly 180 chars are not truncated."""
        msg = "x" * 180
        result = short_error_message(msg)
        assert len(result) == 180
        assert not result.endswith("...")

    def test_179_chars_not_truncated(self) -> None:
        """Messages under 180 chars are not truncated."""
        msg = "x" * 179
        result = short_error_message(msg)
        assert len(result) == 179

    def test_empty_string(self) -> None:
        """Empty string returns empty string."""
        result = short_error_message("")
        assert result == ""

    def test_exception_with_multiline(self) -> None:
        """Exception with multiline message is normalized."""
        exc = Exception("Error occurred:\n  - detail 1\n  - detail 2")
        result = short_error_message(exc)
        assert "\n" not in result
        assert "Error occurred: - detail 1 - detail 2" == result


# ---------------------------------------------------------------------------
# get_duration Tests
# ---------------------------------------------------------------------------


class TestGetDuration:
    """Tests for get_duration function."""

    def test_valid_duration(self) -> None:
        """Returns correct duration for valid inputs."""
        result = get_duration(start=10.0, end=15.5)
        assert result == 5.5

    def test_zero_duration(self) -> None:
        """Returns 0 when start equals end."""
        result = get_duration(start=10.0, end=10.0)
        assert result == 0.0

    def test_negative_duration_returns_zero(self) -> None:
        """Returns 0 when end is before start (never negative)."""
        result = get_duration(start=20.0, end=10.0)
        assert result == 0.0

    def test_none_start(self) -> None:
        """Returns None when start is None."""
        result = get_duration(start=None, end=10.0)
        assert result is None

    def test_none_end(self) -> None:
        """Returns None when end is None."""
        result = get_duration(start=10.0, end=None)
        assert result is None

    def test_both_none(self) -> None:
        """Returns None when both are None."""
        result = get_duration(start=None, end=None)
        assert result is None

    def test_integer_inputs(self) -> None:
        """Works with integer inputs."""
        result = get_duration(start=5, end=10)
        assert result == 5.0
        assert isinstance(result, float)

    def test_string_numeric_inputs(self) -> None:
        """Works with numeric strings."""
        result = get_duration(start="5.0", end="10.0")  # type: ignore[arg-type]
        assert result == 5.0

    def test_invalid_string_start(self) -> None:
        """Returns None for non-numeric string start."""
        result = get_duration(start="invalid", end=10.0)  # type: ignore[arg-type]
        assert result is None

    def test_invalid_string_end(self) -> None:
        """Returns None for non-numeric string end."""
        result = get_duration(start=10.0, end="invalid")  # type: ignore[arg-type]
        assert result is None

    def test_large_duration(self) -> None:
        """Handles large duration values."""
        result = get_duration(start=0.0, end=1_000_000.0)
        assert result == 1_000_000.0

    def test_small_duration(self) -> None:
        """Handles small duration values."""
        result = get_duration(start=0.0, end=0.001)
        assert abs(result - 0.001) < 1e-9


# ---------------------------------------------------------------------------
# format_recent_errors Tests
# ---------------------------------------------------------------------------


class TestFormatRecentErrors:
    """Tests for format_recent_errors function."""

    def test_empty_deque(self) -> None:
        """Empty deque returns empty list."""
        errors: deque[tuple[float, str, str]] = deque()
        result = format_recent_errors(errors)
        assert result == []

    def test_single_error(self) -> None:
        """Single error is formatted correctly."""
        errors: deque[tuple[float, str, str]] = deque()
        errors.append((1234567890.123, "ValueError", "test error"))
        result = format_recent_errors(errors)
        assert len(result) == 1
        assert result[0] == {
            "timestamp": 1234567890.123,
            "error_type": "ValueError",
            "message": "test error",
        }

    def test_multiple_errors(self) -> None:
        """Multiple errors are formatted in order."""
        errors: deque[tuple[float, str, str]] = deque()
        errors.append((100.0, "TypeA", "msg1"))
        errors.append((200.0, "TypeB", "msg2"))
        errors.append((300.0, "TypeC", "msg3"))
        result = format_recent_errors(errors)
        assert len(result) == 3
        assert result[0]["error_type"] == "TypeA"
        assert result[1]["error_type"] == "TypeB"
        assert result[2]["error_type"] == "TypeC"

    def test_preserves_order(self) -> None:
        """Order of errors is preserved."""
        errors: deque[tuple[float, str, str]] = deque()
        for i in range(5):
            errors.append((float(i), f"Type{i}", f"msg{i}"))
        result = format_recent_errors(errors)
        for i, item in enumerate(result):
            assert item["timestamp"] == float(i)

    def test_returns_copy(self) -> None:
        """Returns a new list, not a reference to internal data."""
        errors: deque[tuple[float, str, str]] = deque()
        errors.append((100.0, "Type", "msg"))
        result1 = format_recent_errors(errors)
        result2 = format_recent_errors(errors)
        assert result1 is not result2
        assert result1 == result2

    def test_empty_strings(self) -> None:
        """Handles empty strings in error data."""
        errors: deque[tuple[float, str, str]] = deque()
        errors.append((100.0, "", ""))
        result = format_recent_errors(errors)
        assert result[0] == {"timestamp": 100.0, "error_type": "", "message": ""}

    def test_special_characters(self) -> None:
        """Handles special characters in messages."""
        errors: deque[tuple[float, str, str]] = deque()
        errors.append((100.0, "Error", "msg with\nnewline and\ttab"))
        result = format_recent_errors(errors)
        assert result[0]["message"] == "msg with\nnewline and\ttab"


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestStatsIntegration:
    """Integration tests for stats module."""

    def test_diagnostics_buffer_workflow(self) -> None:
        """Full workflow with DiagnosticsBuffer."""
        buf = DiagnosticsBuffer(max_items=100)

        # Add various warnings and errors
        buf.add_warning("LOW_ACCURACY", {"device_id": "tracker1", "accuracy": 500})
        buf.add_warning("STALE_DATA", {"device_id": "tracker2", "age_hours": 24})
        buf.add_error("API_FAIL", {"device_id": "tracker1", "arg": "locate"})
        buf.add_error("TIMEOUT", {"device_id": "tracker2", "arg": "poll"})

        # Duplicate warning (should be ignored)
        buf.add_warning("LOW_ACCURACY", {"device_id": "tracker1", "accuracy": 600})

        result = buf.to_dict()
        assert result["summary"]["warnings"] == 2
        assert result["summary"]["errors"] == 2

    def test_status_snapshot_comparison(self) -> None:
        """StatusSnapshot comparison with ApiStatus constants."""
        snap_ok = StatusSnapshot(state=ApiStatus.OK)
        snap_error = StatusSnapshot(state=ApiStatus.ERROR, reason="timeout")
        snap_reauth = StatusSnapshot(state=ApiStatus.REAUTH)

        assert snap_ok.state == "ok"
        assert snap_error.state == "error"
        assert snap_reauth.state == "reauth_required"

    def test_error_formatting_pipeline(self) -> None:
        """Full error formatting pipeline."""
        # Create an error message
        exc = Exception(
            "Connection failed:\n  - timeout after 30s\n  - retry exhausted"
        )
        msg = short_error_message(exc)

        # Verify it's normalized
        assert "\n" not in msg
        assert len(msg) <= 180

        # Add to deque and format
        errors: deque[tuple[float, str, str]] = deque(maxlen=10)
        errors.append((time.time(), type(exc).__name__, msg))

        result = format_recent_errors(errors)
        assert len(result) == 1
        assert result[0]["error_type"] == "Exception"
        assert "Connection failed" in result[0]["message"]

    def test_duration_calculation_realistic(self) -> None:
        """Realistic duration calculation scenario."""
        # Simulate setup timing
        setup_start = 1000.0
        fcm_acquired = 1002.5
        setup_end = 1005.0

        setup_duration = get_duration(setup_start, setup_end)
        fcm_duration = get_duration(setup_start, fcm_acquired)

        assert setup_duration == 5.0
        assert fcm_duration == 2.5
        assert fcm_duration < setup_duration

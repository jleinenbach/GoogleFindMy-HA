# tests/test_api_guard_logging.py
"""Tests for the API guard logging helpers."""

from __future__ import annotations

import importlib
import logging
from types import ModuleType

import pytest

from custom_components.googlefindmy import api as api_module


@pytest.fixture
def fresh_api_module() -> ModuleType:
    """Reload the API module to reset guard state for each test."""

    return importlib.reload(api_module)


def test_guard_log_includes_extra_context_without_suffix_artifacts(
    caplog: pytest.LogCaptureFixture, fresh_api_module: ModuleType
) -> None:
    """Ensure `_maybe_log_guard_once` formats extra context cleanly."""

    fresh_api_module._GUARD_LOGGED_ONCE = False  # type: ignore[attr-defined]

    caplog.set_level(logging.INFO)
    fresh_api_module._maybe_log_guard_once(  # type: ignore[attr-defined]
        "guard context", email="user@example.com", entry_id="entry-123"
    )

    assert caplog.records, "Expected guard log message to be emitted"
    record = caplog.records[-1]
    message = record.getMessage()

    assert record.levelno == logging.INFO
    assert "(email=user@example.com, entry_id=entry-123)" in message
    assert "if extra else" not in message

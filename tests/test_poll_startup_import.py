# tests/test_poll_startup_import.py
"""The startup regression remains importable in a partial test environment."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest


def test_missing_optional_helper_skips_startup_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing the HA pytest helper skips this module instead of raising ImportError."""
    monkeypatch.setitem(
        sys.modules, "pytest_homeassistant_custom_component.common", None
    )
    module_path = Path(__file__).with_name("test_poll_startup.py")

    with pytest.raises(
        pytest.skip.Exception,
        match="pytest-homeassistant-custom-component is required",
    ):
        runpy.run_path(str(module_path))

"""Tests for the Home Assistant callback stub helper."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType

import pytest

from tests.helpers import install_homeassistant_core_callback_stub


@pytest.fixture
def clear_homeassistant_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ``homeassistant.core`` from ``sys.modules`` for the test duration."""

    monkeypatch.delitem(sys.modules, "homeassistant.core", raising=False)


def test_stub_inserts_module_when_missing(clear_homeassistant_core: None) -> None:
    """Helper populates ``sys.modules`` when ``homeassistant.core`` is absent."""

    module = install_homeassistant_core_callback_stub()

    assert module is sys.modules["homeassistant.core"]

    def _sample() -> str:
        return "value"

    assert module.callback(_sample) is _sample


def test_stub_preserves_existing_callback_when_not_overwriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing callbacks remain intact unless overwrite is requested."""

    existing_module = ModuleType("homeassistant.core")

    def _existing_callback(func: Callable[..., object]) -> str:
        return "sentinel"

    existing_module.callback = _existing_callback  # type: ignore[attr-defined]
    with monkeypatch.context() as context:
        context.setitem(sys.modules, "homeassistant.core", existing_module)
        module = install_homeassistant_core_callback_stub()

    assert module.callback is _existing_callback


def test_stub_overwrites_existing_callback_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overwrite flag replaces existing callback decorators with the stub."""

    existing_module = ModuleType("homeassistant.core")

    def _existing_callback(func: Callable[..., object]) -> str:
        return "sentinel"

    existing_module.callback = _existing_callback  # type: ignore[attr-defined]
    with monkeypatch.context() as context:
        context.setitem(sys.modules, "homeassistant.core", existing_module)
        module = install_homeassistant_core_callback_stub(overwrite=True)

        def _sample() -> str:
            return "value"

        result = module.callback(_sample)

    assert result is _sample

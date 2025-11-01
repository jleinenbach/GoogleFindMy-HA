# tests/test_chrome_driver.py
"""Tests for the Chrome driver helpers using stubbed undetected-chromedriver APIs."""

from __future__ import annotations

import importlib
import logging
import sys
from types import SimpleNamespace

import pytest


class _BootstrapChromeOptions:
    """Lightweight Chrome options stub for module import bootstrap."""

    def add_argument(self, _: str) -> None:  # pragma: no cover - defensive placeholder
        """Ignore bootstrap arguments added during import."""


def _bootstrap_chrome(*, options: object) -> object:  # pragma: no cover - defensive placeholder
    """Return a generic driver object during bootstrap imports."""

    return object()


sys.modules.setdefault(
    "undetected_chromedriver",
    SimpleNamespace(ChromeOptions=_BootstrapChromeOptions, Chrome=_bootstrap_chrome),
)

chrome_driver = importlib.import_module("custom_components.googlefindmy.chrome_driver")


class FakeChromeOptions:
    """Record Chrome options arguments for inspection in tests."""

    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.binary_location: str | None = None

    def add_argument(self, argument: str) -> None:
        self.arguments.append(argument)


class SentinelError(RuntimeError):
    """Sentinel error raised by the Chrome stub."""


@pytest.fixture(autouse=True)
def _reset_uc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the undetected_chromedriver stubs for each test."""

    monkeypatch.setattr(chrome_driver.uc, "ChromeOptions", _BootstrapChromeOptions)
    monkeypatch.setattr(chrome_driver.uc, "Chrome", _bootstrap_chrome)


def test_get_options_headless_uses_expected_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure headless options populate the expected Chrome arguments."""

    monkeypatch.setattr(chrome_driver.uc, "ChromeOptions", FakeChromeOptions)

    options = chrome_driver.get_options(headless=True)

    assert isinstance(options, FakeChromeOptions)
    assert options.arguments == [
        "--headless",
        "--disable-extensions",
        "--disable-gpu",
        "--no-sandbox",
    ]


def test_create_driver_headless_passes_options_to_uc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the driver factory returns the fake driver and forwards options."""

    fake_driver = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(chrome_driver.uc, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(*, options: object) -> object:
        captured["options"] = options
        return fake_driver

    monkeypatch.setattr(chrome_driver.uc, "Chrome", fake_chrome)

    driver = chrome_driver.create_driver(headless=True)

    assert driver is fake_driver
    assert isinstance(captured["options"], FakeChromeOptions)
    assert captured["options"].arguments == [
        "--headless",
        "--disable-extensions",
        "--disable-gpu",
        "--no-sandbox",
    ]


def test_create_driver_fallback_logs_and_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The driver falls back to the system Chrome binary and raises a runtime error when startup fails twice."""

    caplog.set_level(logging.WARNING)

    chrome_calls: list[FakeChromeOptions] = []

    def chrome_stub(*, options: FakeChromeOptions) -> object:
        chrome_calls.append(options)
        raise SentinelError("driver start failed")

    monkeypatch.setattr(chrome_driver.uc, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(chrome_driver.uc, "Chrome", chrome_stub)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=True)

    assert len(chrome_calls) == 2, "Both bundled and fallback Chrome invocations should be attempted"
    assert chrome_calls[0].binary_location is None
    assert chrome_calls[1].binary_location == "/opt/chrome"
    assert "Default ChromeDriver startup failed" in " ".join(caplog.messages)
    assert "ChromeDriver failed using system binary" in " ".join(caplog.messages)

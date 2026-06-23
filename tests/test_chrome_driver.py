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


def _bootstrap_chrome(
    *, options: object
) -> object:  # pragma: no cover - defensive placeholder
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

    stub_uc = SimpleNamespace(
        ChromeOptions=_BootstrapChromeOptions, Chrome=_bootstrap_chrome
    )
    chrome_driver._reset_uc_cache(stub_uc)

    yield

    chrome_driver._reset_uc_cache(None)


def test_get_options_headless_uses_expected_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure headless options populate the expected Chrome arguments."""

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    options = chrome_driver.get_options(headless=True)

    assert isinstance(options, FakeChromeOptions)
    assert options.arguments == [
        "--headless",
        "--disable-extensions",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-web-security",
        "--allow-running-insecure-content",
    ]


def test_create_driver_headless_passes_options_to_uc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the driver factory returns the fake driver and forwards options."""

    fake_driver = object()
    captured: dict[str, object] = {}

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    # Mock _kill_existing_chrome_processes to avoid actual process killing
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    # Mock find_chrome to return None (no Chrome found)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)

    def fake_chrome(
        *, options: object, version_main: int | None = None, **kwargs: object
    ) -> object:
        captured["options"] = options
        captured["version_main"] = version_main
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)

    driver = chrome_driver.create_driver(headless=True)

    assert driver is fake_driver
    assert isinstance(captured["options"], FakeChromeOptions)
    assert captured["options"].arguments == [
        "--headless",
        "--disable-extensions",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-web-security",
        "--allow-running-insecure-content",
    ]
    assert captured["version_main"] is None


def test_create_driver_fallback_logs_and_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The driver falls back through multiple strategies and raises a runtime error when all fail.

    With the new 5-strategy approach (headless=True skips strategy 4):
    1. Standard with version_main
    2. browser_executable_path parameter
    3. Without specifying version
    5. webdriver-manager fallback (skipped if not available)
    """

    caplog.set_level(logging.WARNING)

    chrome_calls: list[FakeChromeOptions] = []

    # Mock _kill_existing_chrome_processes to avoid actual process killing
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    # Mock get_chrome_version to return None (can't detect version)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _: None)
    # Disable webdriver-manager fallback
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        chrome_calls.append(options)
        raise SentinelError("driver start failed")

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=True)

    # With headless=True: strategies 1, 2, 3 are attempted (strategy 4 skipped, 5 disabled)
    assert len(chrome_calls) == 3, (
        "Strategies 1, 2, and 3 should be attempted (headless skips strategy 4)"
    )
    # All calls should have binary_location set since find_chrome returns a path
    assert chrome_calls[0].binary_location == "/opt/chrome"
    assert (
        chrome_calls[1].binary_location is None
    )  # Strategy 2 uses browser_executable_path param
    assert chrome_calls[2].binary_location == "/opt/chrome"
    assert "Strategy 1 (default) failed" in " ".join(caplog.messages)
    assert "Strategy 2 (explicit path) failed" in " ".join(caplog.messages)
    assert "Strategy 3 (no version) failed" in " ".join(caplog.messages)


def test_create_driver_headless_fallback_on_non_headless_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When non-headless mode fails, the driver tries headless as strategy 4.

    With the new 5-strategy approach (headless=False includes strategy 4):
    1. Standard with version_main
    2. browser_executable_path parameter
    3. Without specifying version
    4. Headless mode fallback
    5. webdriver-manager fallback (skipped if not available)
    """

    caplog.set_level(logging.WARNING)

    chrome_calls: list[FakeChromeOptions] = []

    # Mock _kill_existing_chrome_processes to avoid actual process killing
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    # Mock get_chrome_version to return None (can't detect version)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _: None)
    # Disable webdriver-manager fallback
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        chrome_calls.append(options)
        raise SentinelError("driver start failed")

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=False)

    # With headless=False: strategies 1, 2, 3, 4 are attempted (5 disabled)
    assert len(chrome_calls) == 4, (
        "Strategies 1, 2, 3, and 4 (headless) should be attempted"
    )
    # All calls with options should have binary_location set
    assert chrome_calls[0].binary_location == "/opt/chrome"
    assert (
        chrome_calls[1].binary_location is None
    )  # Strategy 2 uses browser_executable_path param
    assert chrome_calls[2].binary_location == "/opt/chrome"
    assert chrome_calls[3].binary_location == "/opt/chrome"
    assert "--headless" in chrome_calls[3].arguments
    assert "Strategy 4 (headless) failed" in " ".join(caplog.messages)


@pytest.mark.xfail(
    strict=True,
    reason="characterizes Strategy-3 None-escalation bug; flips to GREEN in AP2",
)
def test_strategy3_escalates_to_none_when_strategy1_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterize the Strategy-3 ``version_main=None`` escalation bug.

    When Strategy 1 and Strategy 2 fail transiently but a Chrome version was
    detected (149), the current code reaches Strategy 3, which hardcodes
    ``version_main=None``. undetected-chromedriver then fetches the latest
    stable driver (e.g. 150) instead of the detected 149, causing a hard
    version-mismatch abort.

    This test pins the desired behavior: no ``Chrome(...)`` call may pass
    ``version_main=None`` while a version is known. It is marked ``xfail``
    here because the fix lands in AP2; the marker is removed there so the test
    flips to a hard GREEN in the same commit as the fix.
    """

    recorded_versions: list[int | None] = []

    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _: 149)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    fake_driver = object()

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        recorded_versions.append(version_main)
        # Strategy 1 and Strategy 2 fail transiently; Strategy 3 (third call)
        # succeeds, so the loop returns the fake driver from Strategy 3.
        if len(recorded_versions) < 3:
            raise SentinelError("transient driver start failure")
        return fake_driver

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)

    driver = chrome_driver.create_driver(headless=True)

    assert driver is fake_driver
    # The third call is Strategy 3. Against today's code it records ``None``.
    assert recorded_versions[-1] == 149, (
        "Strategy 3 must reuse the detected version, not escalate to None"
    )
    assert None not in recorded_versions

# tests/test_chrome_driver.py
"""Tests for the Chrome driver helpers using stubbed undetected-chromedriver APIs."""

from __future__ import annotations

import importlib
import logging
import os
import platform
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

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
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _, **_k: None)
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
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _, **_k: None)
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


def test_strategy3_does_not_escalate_to_none_when_version_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No uc.Chrome attempt may pass ``version_main=None`` when a version is known.

    With Chrome 149 detected, every ``Chrome(...)`` call must carry
    ``version_main=149``. The old code reached Strategy 3 and hardcoded
    ``version_main=None``, making undetected-chromedriver fetch the latest
    stable driver (e.g. 150) and abort with a version mismatch.

    All attempts are forced to fail so we can inspect every recorded
    ``version_main`` regardless of which strategy would otherwise succeed; the
    assertion is design-agnostic (it pins the invariant, not the call count).
    """

    recorded_versions: list[int | None] = []

    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _, **_k: 149)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)
    monkeypatch.delenv("GOOGLEFINDMY_CHROME_PATH", raising=False)
    monkeypatch.delenv("GOOGLEFINDMY_CHROME_VERSION", raising=False)

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        recorded_versions.append(version_main)
        raise SentinelError("driver start failure")

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=True)

    # Core invariant: not a single attempt may use None while 149 is known.
    assert recorded_versions, "expected at least one uc.Chrome attempt"
    assert None not in recorded_versions
    assert all(version == 149 for version in recorded_versions)


def test_module_import_with_webdriver_manager_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload the module with stubbed webdriver-manager deps to cover the import-success block.

    The optional ``webdriver_manager``/Selenium service imports normally fail in
    this environment (``except ImportError`` path). Injecting lightweight stubs
    into ``sys.modules`` and reloading exercises the success branch that records
    the fallback classes. The module is reloaded again afterwards so the rest of
    the suite observes the original (unavailable) state.
    """

    import types

    class _StubChromeDriverManager:
        def install(self) -> str:  # pragma: no cover - stub never invoked here
            return "/driver"

    wdm_chrome_mod = types.ModuleType("webdriver_manager.chrome")
    wdm_chrome_mod.ChromeDriverManager = _StubChromeDriverManager  # type: ignore[attr-defined]
    wdm_pkg = types.ModuleType("webdriver_manager")

    monkeypatch.setitem(sys.modules, "webdriver_manager", wdm_pkg)
    monkeypatch.setitem(sys.modules, "webdriver_manager.chrome", wdm_chrome_mod)

    try:
        importlib.reload(chrome_driver)
        assert chrome_driver._WEBDRIVER_MANAGER_AVAILABLE is True
        assert chrome_driver._chrome_driver_manager_cls is _StubChromeDriverManager
    finally:
        # Restore the real (import-failing) module state for later tests.
        sys.modules.pop("webdriver_manager.chrome", None)
        sys.modules.pop("webdriver_manager", None)
        importlib.reload(chrome_driver)
        assert chrome_driver._WEBDRIVER_MANAGER_AVAILABLE is False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the Chrome override env vars for deterministic resolution."""

    monkeypatch.delenv("GOOGLEFINDMY_CHROME_PATH", raising=False)
    monkeypatch.delenv("GOOGLEFINDMY_CHROME_VERSION", raising=False)


# ---------------------------------------------------------------------------
# _load_uc / _get_uc_module
# ---------------------------------------------------------------------------


def test_load_uc_falls_back_to_stub_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the real package is missing, ``_load_uc`` returns a stub namespace."""

    def fake_import(name: str) -> Any:
        raise ImportError("no undetected_chromedriver")

    monkeypatch.setattr(importlib, "import_module", fake_import)

    stub = chrome_driver._load_uc()

    options = stub.ChromeOptions()
    options.add_argument("--foo")
    assert options.arguments == ["--foo"]
    assert options.binary_location is None
    with pytest.raises(RuntimeError, match="could not be imported"):
        stub.Chrome(options=options)


def test_get_uc_module_caches_loaded_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_get_uc_module`` loads via ``_load_uc`` once and then reuses the cache."""

    sentinel = SimpleNamespace(name="cached")
    calls = {"count": 0}

    def fake_load() -> Any:
        calls["count"] += 1
        return sentinel

    chrome_driver._reset_uc_cache(None)
    monkeypatch.setattr(chrome_driver, "_load_uc", fake_load)

    first = chrome_driver._get_uc_module()
    second = chrome_driver._get_uc_module()

    assert first is sentinel
    assert second is sentinel
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# get_chrome_version
# ---------------------------------------------------------------------------


def test_get_chrome_version_linux_parses_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(stdout="Google Chrome 149.0.1234.56 ")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert chrome_driver.get_chrome_version("/usr/bin/chrome") == 149


def test_get_chrome_version_linux_no_match_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="no version here")
    )

    assert chrome_driver.get_chrome_version("/usr/bin/chrome") is None


def test_get_chrome_version_subprocess_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot run chrome")

    monkeypatch.setattr(subprocess, "run", boom)

    assert chrome_driver.get_chrome_version("/usr/bin/chrome") is None


def test_get_chrome_version_windows_registry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows path reads the version from a fake ``winreg`` module."""

    monkeypatch.setattr(platform, "system", lambda: "Windows")

    class _FakeWinreg:
        HKEY_CURRENT_USER = 1

        @staticmethod
        def OpenKey(root: Any, sub: str) -> Any:
            return object()

        @staticmethod
        def QueryValueEx(key: Any, name: str) -> tuple[str, int]:
            return "152.0.99.7", 1

        @staticmethod
        def CloseKey(key: Any) -> None:
            return None

    monkeypatch.setattr(chrome_driver, "_winreg", _FakeWinreg)

    assert chrome_driver.get_chrome_version("C:/chrome.exe") == 152


def test_get_chrome_version_windows_registry_failure_falls_back_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry errors fall through to the ``--version`` subprocess on Windows."""

    monkeypatch.setattr(platform, "system", lambda: "Windows")

    class _BrokenWinreg:
        HKEY_CURRENT_USER = 1

        @staticmethod
        def OpenKey(root: Any, sub: str) -> Any:
            raise OSError("registry unavailable")

    monkeypatch.setattr(chrome_driver, "_winreg", _BrokenWinreg)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="Chrome 151.0.0.0"),
    )

    assert chrome_driver.get_chrome_version("C:/chrome.exe") == 151


def test_get_chrome_version_windows_no_winreg_uses_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_winreg`` is ``None`` on Windows, only the subprocess path runs."""

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome_driver, "_winreg", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="Chrome 153.1.2.3"),
    )

    assert chrome_driver.get_chrome_version("C:/chrome.exe") == 153


def test_get_chrome_version_windows_no_match_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome_driver, "_winreg", None)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="nothing")
    )

    assert chrome_driver.get_chrome_version("C:/chrome.exe") is None


def test_get_chrome_version_windows_prefer_binary_skips_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``prefer_binary=True`` bypasses the registry and queries the binary.

    The registry reports the registered default Chrome. For an explicit/env
    path override the overridden binary may carry a different version, so the
    binary's own ``--version`` must win. The fake ``winreg`` raises if touched,
    proving the registry branch is skipped entirely.
    """

    monkeypatch.setattr(platform, "system", lambda: "Windows")

    class _DefaultWinreg:
        """Registry that successfully reports the registered default Chrome (152)."""

        HKEY_CURRENT_USER = 1

        @staticmethod
        def OpenKey(root: Any, sub: str) -> Any:
            return object()

        @staticmethod
        def QueryValueEx(key: Any, name: str) -> tuple[str, int]:
            return "152.0.99.7", 1

        @staticmethod
        def CloseKey(key: Any) -> None:
            return None

    monkeypatch.setattr(chrome_driver, "_winreg", _DefaultWinreg)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="Chrome 149.0.7827.155"),
    )

    # The registry would have returned the default 152; with prefer_binary the
    # overridden binary's own version (149) must win instead. Without the skip
    # this would return 152, so the assertion is mutation-sharp.
    assert (
        chrome_driver.get_chrome_version(
            "C:/portable/chrome.exe", prefer_binary=True
        )
        == 149
    )


# ---------------------------------------------------------------------------
# _kill_existing_chrome_processes
# ---------------------------------------------------------------------------


def test_kill_existing_chrome_processes_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace()
    )

    chrome_driver._kill_existing_chrome_processes()

    assert calls == [["pkill", "-f", "chrome"]]


def test_kill_existing_chrome_processes_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace()
    )

    chrome_driver._kill_existing_chrome_processes()

    assert calls == [["taskkill", "/f", "/im", "chrome.exe"]]


# ---------------------------------------------------------------------------
# find_chrome
# ---------------------------------------------------------------------------


def test_find_chrome_returns_existing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    target = "/usr/bin/google-chrome"
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os.path, "exists", lambda p: p == target)

    assert chrome_driver.find_chrome() == target


def test_find_chrome_uses_which_when_no_path_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)

    def fake_which(name: str) -> str | None:
        return "/snap/chromium" if name == "chromium" else None

    monkeypatch.setattr(chrome_driver.shutil, "which", fake_which)

    assert chrome_driver.find_chrome() == "/snap/chromium"


def test_find_chrome_returns_none_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    monkeypatch.setattr(chrome_driver.shutil, "which", lambda _n: None)

    assert chrome_driver.find_chrome() is None


def test_find_chrome_windows_which_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)

    def fake_which(name: str) -> str | None:
        return "C:/chrome.exe" if name == "chrome" else None

    monkeypatch.setattr(chrome_driver.shutil, "which", fake_which)

    assert chrome_driver.find_chrome() == "C:/chrome.exe"


def test_find_chrome_windows_where_command_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    monkeypatch.setattr(chrome_driver.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="C:/where/chrome.exe\nC:/other.exe"
        ),
    )

    assert chrome_driver.find_chrome() == "C:/where/chrome.exe"


def test_find_chrome_windows_where_nonzero_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ``where`` return code falls through to ``None``."""

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    monkeypatch.setattr(chrome_driver.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""),
    )

    assert chrome_driver.find_chrome() is None


def test_find_chrome_windows_where_command_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(os.path, "exists", lambda _p: False)
    monkeypatch.setattr(chrome_driver.shutil, "which", lambda _n: None)

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("where failed")

    monkeypatch.setattr(subprocess, "run", boom)

    assert chrome_driver.find_chrome() is None


# ---------------------------------------------------------------------------
# get_driver
# ---------------------------------------------------------------------------


def test_get_driver_passes_resolved_version_and_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_driver = object()

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        captured["binary_location"] = options.binary_location
        captured["version_main"] = version_main
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: 140)

    driver = chrome_driver.get_driver("/opt/chrome", chrome_version=149)

    assert driver is fake_driver
    assert captured["binary_location"] == "/opt/chrome"
    # Explicit version wins over the detected one.
    assert captured["version_main"] == 149


def test_get_driver_without_path_uses_detected_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_driver = object()

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        captured["binary_location"] = options.binary_location
        captured["version_main"] = version_main
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)

    chrome_driver.get_driver(None)

    assert captured["binary_location"] is None
    assert captured["version_main"] is None


def test_get_driver_detects_explicit_binary_with_prefer_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_driver`` always queries an explicit path binary directly.

    The ``chrome_path`` argument here is always an explicit override, so the
    call site must pass ``prefer_binary=True`` to bypass the Windows registry.
    """

    captured: dict[str, Any] = {}
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **kwargs: object(),
    )

    def spy(path: str, *, prefer_binary: bool = False) -> int:
        captured["path"] = path
        captured["prefer_binary"] = prefer_binary
        return 149

    monkeypatch.setattr(chrome_driver, "get_chrome_version", spy)

    chrome_driver.get_driver("/opt/portable/chrome")

    assert captured == {"path": "/opt/portable/chrome", "prefer_binary": True}


# ---------------------------------------------------------------------------
# _try_webdriver_manager_fallback
# ---------------------------------------------------------------------------


def test_webdriver_manager_fallback_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)
    assert chrome_driver._try_webdriver_manager_fallback() is None


def test_webdriver_manager_fallback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()

    class _FakeOptions:
        def __init__(self) -> None:
            self.arguments: list[str] = []

        def add_argument(self, arg: str) -> None:
            self.arguments.append(arg)

    fake_webdriver = SimpleNamespace(
        ChromeOptions=_FakeOptions,
        Chrome=lambda *, service, options: fake_driver,
    )

    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", True)
    monkeypatch.setattr(chrome_driver, "_selenium_webdriver", fake_webdriver)
    monkeypatch.setattr(
        chrome_driver, "_chrome_service_cls", lambda path: SimpleNamespace(path=path)
    )
    monkeypatch.setattr(
        chrome_driver,
        "_chrome_driver_manager_cls",
        lambda: SimpleNamespace(install=lambda: "/driver"),
    )

    assert chrome_driver._try_webdriver_manager_fallback() is fake_driver


def test_webdriver_manager_fallback_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", True)

    def boom() -> Any:
        raise RuntimeError("manager install failed")

    monkeypatch.setattr(chrome_driver, "_chrome_driver_manager_cls", boom)

    assert chrome_driver._try_webdriver_manager_fallback() is None


# ---------------------------------------------------------------------------
# safe_quit_driver / _quit_driver
# ---------------------------------------------------------------------------


def test_safe_quit_driver_none_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # No subprocess should be invoked for a ``None`` driver.
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("should not run")
    )
    chrome_driver.safe_quit_driver(None)


def test_safe_quit_driver_normal_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace()
    )

    quit_calls = {"n": 0}

    class _Driver:
        def quit(self) -> None:
            quit_calls["n"] += 1

    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]

    assert quit_calls["n"] == 1
    assert calls == [["pkill", "-f", "chromedriver"]]


def test_safe_quit_driver_oserror_is_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace()
    )

    class _Driver:
        def quit(self) -> None:
            raise OSError("WinError 6")

    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]

    assert calls == [["taskkill", "/f", "/im", "chromedriver.exe"]]


def test_safe_quit_driver_other_exception_is_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace())

    class _Driver:
        def quit(self) -> None:
            raise RuntimeError("boom")

    # Must not raise.
    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]


def test_safe_quit_driver_swallows_cleanup_subprocess_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    class _Driver:
        def quit(self) -> None:
            return None

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("pkill missing")

    monkeypatch.setattr(subprocess, "run", boom)

    # The cleanup subprocess error is swallowed by the inner try/except.
    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]


def test_quit_driver_none_is_noop() -> None:
    chrome_driver._quit_driver(None)


def test_quit_driver_suppresses_quit_error() -> None:
    class _Driver:
        def quit(self) -> None:
            raise RuntimeError("boom")

    chrome_driver._quit_driver(_Driver())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_env_version
# ---------------------------------------------------------------------------


def test_parse_env_version_none() -> None:
    assert chrome_driver._parse_env_version(None) is None


def test_parse_env_version_empty_and_whitespace() -> None:
    assert chrome_driver._parse_env_version("") is None
    assert chrome_driver._parse_env_version("   ") is None


def test_parse_env_version_valid_int() -> None:
    assert chrome_driver._parse_env_version("149") == 149
    assert chrome_driver._parse_env_version("  150  ") == 150


def test_parse_env_version_invalid_warns_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    assert chrome_driver._parse_env_version("abc") is None
    assert "Ignoring invalid" in " ".join(caplog.messages)


# ---------------------------------------------------------------------------
# _resolve_chrome_version / _resolve_chrome_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("explicit", "env_raw", "detected", "expected"),
    [
        (149, "150", 151, (149, "cli")),
        (None, "150", 151, (150, "env")),
        (None, None, 151, (151, "auto")),
        (None, "  ", 151, (151, "auto")),
        (None, "bad", 151, (151, "auto")),
        (None, None, None, (None, "none")),
    ],
)
def test_resolve_chrome_version_precedence(
    explicit: int | None,
    env_raw: str | None,
    detected: int | None,
    expected: tuple[int | None, str],
) -> None:
    assert (
        chrome_driver._resolve_chrome_version(
            explicit=explicit, env_raw=env_raw, detected=detected
        )
        == expected
    )


@pytest.mark.parametrize(
    ("explicit", "env_raw", "detected", "expected"),
    [
        ("/cli", "/env", "/auto", ("/cli", "cli")),
        (None, "/env", "/auto", ("/env", "env")),
        (None, "  /env  ", "/auto", ("/env", "env")),
        (None, "   ", "/auto", ("/auto", "auto")),
        (None, None, "/auto", ("/auto", "auto")),
        (None, None, None, (None, "none")),
    ],
)
def test_resolve_chrome_path_precedence(
    explicit: str | None,
    env_raw: str | None,
    detected: str | None,
    expected: tuple[str | None, str],
) -> None:
    assert (
        chrome_driver._resolve_chrome_path(
            explicit=explicit, env_raw=env_raw, detected=detected
        )
        == expected
    )


# ---------------------------------------------------------------------------
# _is_file_lock_error / _is_version_mismatch_error / _log_version_mismatch_hint
# ---------------------------------------------------------------------------


def test_is_file_lock_error() -> None:
    assert chrome_driver._is_file_lock_error(OSError("WinError 32 something"))
    assert chrome_driver._is_file_lock_error(OSError("WinError 183"))
    assert not chrome_driver._is_file_lock_error(OSError("other"))


def test_is_version_mismatch_error() -> None:
    assert chrome_driver._is_version_mismatch_error(
        RuntimeError("only supports Chrome version 150")
    )
    assert not chrome_driver._is_version_mismatch_error(RuntimeError("nope"))


def test_log_version_mismatch_hint_noop_for_non_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    chrome_driver._log_version_mismatch_hint(
        RuntimeError("unrelated"),
        detected_version=149,
        resolved_version=150,
        version_source="cli",
    )
    assert caplog.messages == []


def test_log_version_mismatch_hint_noop_for_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    chrome_driver._log_version_mismatch_hint(
        None, detected_version=None, resolved_version=None, version_source="none"
    )
    assert caplog.messages == []


def test_log_version_mismatch_hint_emits_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    chrome_driver._log_version_mismatch_hint(
        RuntimeError("only supports Chrome version 150"),
        detected_version=None,
        resolved_version=None,
        version_source="auto",
    )
    joined = " ".join(caplog.messages)
    assert "version mismatch" in joined.lower()
    assert "Unknown" in joined


# ---------------------------------------------------------------------------
# Strategy helpers (failure return contracts)
# ---------------------------------------------------------------------------


def _install_failing_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        raise SentinelError("strategy failed")

    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)


def test_strategy_default_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_chrome(monkeypatch)
    driver, error = chrome_driver._try_strategy_default(
        resolved_path="/opt/chrome", version_main=149, headless=False
    )
    assert driver is None
    assert isinstance(error, SentinelError)


def test_strategy_explicit_path_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_chrome(monkeypatch)
    driver, error = chrome_driver._try_strategy_explicit_path(
        resolved_path="/opt/chrome", version_main=149, headless=True
    )
    assert driver is None
    assert isinstance(error, SentinelError)


def test_strategy_no_version_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_chrome(monkeypatch)
    driver, error = chrome_driver._try_strategy_no_version(
        resolved_path=None, headless=True
    )
    assert driver is None
    assert isinstance(error, SentinelError)


def test_strategy_headless_failure_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_chrome(monkeypatch)
    driver, error = chrome_driver._try_strategy_headless(
        resolved_path=None, version_main=149
    )
    assert driver is None
    assert isinstance(error, SentinelError)


def test_strategy_default_success_without_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module, "Chrome", lambda *, options, version_main=None, **k: fake_driver
    )

    driver, error = chrome_driver._try_strategy_default(
        resolved_path=None, version_main=None, headless=False
    )
    assert driver is fake_driver
    assert error is None


def test_strategy_explicit_path_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    captured: dict[str, Any] = {}
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(
        *, options: Any, version_main: int | None = None, **kwargs: object
    ) -> object:
        captured["version_main"] = version_main
        captured["browser_executable_path"] = kwargs.get("browser_executable_path")
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)

    driver, error = chrome_driver._try_strategy_explicit_path(
        resolved_path="/opt/chrome", version_main=149, headless=False
    )
    assert driver is fake_driver
    assert error is None
    assert captured == {
        "version_main": 149,
        "browser_executable_path": "/opt/chrome",
    }


def test_strategy_no_version_success_with_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    captured: dict[str, Any] = {}
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(*, options: Any, version_main: int | None = None, **k: object) -> object:
        captured["binary"] = options.binary_location
        captured["version_main"] = version_main
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)

    driver, error = chrome_driver._try_strategy_no_version(
        resolved_path="/opt/chrome", headless=True
    )
    assert driver is fake_driver
    assert error is None
    assert captured == {"binary": "/opt/chrome", "version_main": None}


def test_strategy_headless_success_with_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    captured: dict[str, Any] = {}
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(*, options: Any, version_main: int | None = None, **k: object) -> object:
        captured["binary"] = options.binary_location
        captured["headless"] = "--headless" in options.arguments
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)

    driver, error = chrome_driver._try_strategy_headless(
        resolved_path="/opt/chrome", version_main=149
    )
    assert driver is fake_driver
    assert error is None
    assert captured == {"binary": "/opt/chrome", "headless": True}


# ---------------------------------------------------------------------------
# Resolver behavior in _create_driver_inner (precedence / env / overrides)
# ---------------------------------------------------------------------------


def _stub_uc_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Install a UC stub that records every Chrome call and returns a driver."""

    records: list[dict[str, Any]] = []
    fake_driver = object()
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def fake_chrome(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        records.append(
            {
                "version_main": version_main,
                "binary_location": options.binary_location,
                "browser_executable_path": kwargs.get("browser_executable_path"),
            }
        )
        return fake_driver

    monkeypatch.setattr(uc_module, "Chrome", fake_chrome)
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    return records


@pytest.mark.parametrize(
    ("explicit", "env", "detected", "expected_version"),
    [
        (149, "150", 151, 149),  # explicit beats env
        (None, "150", 151, 150),  # env beats auto
        (None, None, 151, 151),  # auto
    ],
)
def test_version_precedence(
    monkeypatch: pytest.MonkeyPatch,
    explicit: int | None,
    env: str | None,
    detected: int | None,
    expected_version: int,
) -> None:
    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: detected)
    if env is not None:
        monkeypatch.setenv("GOOGLEFINDMY_CHROME_VERSION", env)

    chrome_driver.create_driver(chrome_version=explicit, headless=True)

    assert records[0]["version_main"] == expected_version


@pytest.mark.parametrize(
    ("explicit", "env", "detected", "expected_path"),
    [
        ("/cli", "/env", "/auto", "/cli"),
        (None, "/env", "/auto", "/env"),
        (None, None, "/auto", "/auto"),
    ],
)
def test_path_precedence(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    env: str | None,
    detected: str | None,
    expected_path: str,
) -> None:
    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: detected)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    if env is not None:
        monkeypatch.setenv("GOOGLEFINDMY_CHROME_PATH", env)

    chrome_driver.create_driver(chrome_path=explicit, headless=True)

    assert records[0]["binary_location"] == expected_path


@pytest.mark.parametrize(
    ("explicit", "env", "expected_prefer"),
    [
        ("/cli/chrome", None, True),  # explicit override -> query binary
        (None, "/env/chrome", True),  # env override -> query binary
        (None, None, False),  # auto-detected -> registry-first preserved
    ],
)
def test_prefer_binary_follows_path_source(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    env: str | None,
    expected_prefer: bool,
) -> None:
    """The version lookup bypasses the registry only for explicit/env paths.

    Auto-detected paths keep ``prefer_binary=False`` (registry-first), while an
    explicit CLI argument or the env override must set ``prefer_binary=True`` so
    the overridden binary's own version is detected.
    """

    _stub_uc_capture(monkeypatch)
    captured: dict[str, bool] = {}
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/auto/chrome")

    def spy(path: str, *, prefer_binary: bool = False) -> int:
        captured["prefer_binary"] = prefer_binary
        return 149

    monkeypatch.setattr(chrome_driver, "get_chrome_version", spy)
    if env is not None:
        monkeypatch.setenv("GOOGLEFINDMY_CHROME_PATH", env)

    chrome_driver.create_driver(chrome_path=explicit, headless=True)

    assert captured["prefer_binary"] is expected_prefer


def test_strategy3_passes_none_only_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no resolvable version, strategy 3 (version_main=None) is appended."""

    versions: list[int | None] = []
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)

    def chrome_stub(
        *, options: FakeChromeOptions, version_main: int | None = None, **kwargs: object
    ) -> object:
        versions.append(version_main)
        raise SentinelError("fail")

    monkeypatch.setattr(uc_module, "Chrome", chrome_stub)
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=True)

    # No path, no version: strategies 1 and 3 run, both with version_main=None.
    assert versions == [None, None]


def test_version_override_without_path_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An explicit version with no resolvable path logs a warning and is used."""

    caplog.set_level(logging.WARNING)
    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)

    chrome_driver.create_driver(chrome_version=149, headless=True)

    assert records[0]["version_main"] == 149
    assert records[0]["binary_location"] is None
    assert "Chrome version override is set" in " ".join(caplog.messages)


def test_env_var_applied_without_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_driver()`` with no args picks up both env overrides."""

    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setenv("GOOGLEFINDMY_CHROME_PATH", "/env/chrome")
    monkeypatch.setenv("GOOGLEFINDMY_CHROME_VERSION", "148")

    chrome_driver.create_driver(headless=True)

    assert records[0]["binary_location"] == "/env/chrome"
    assert records[0]["version_main"] == 148


def test_invalid_env_version_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: 151)
    monkeypatch.setenv("GOOGLEFINDMY_CHROME_VERSION", "garbage")

    chrome_driver.create_driver(headless=True)

    # Invalid env is ignored; detected version (auto) is used instead.
    assert records[0]["version_main"] == 151
    assert "Ignoring invalid" in " ".join(caplog.messages)


def test_empty_env_version_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _stub_uc_capture(monkeypatch)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: 151)
    monkeypatch.setenv("GOOGLEFINDMY_CHROME_VERSION", "   ")

    chrome_driver.create_driver(headless=True)

    assert records[0]["version_main"] == 151


def test_final_error_message_contains_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **k: (_ for _ in ()).throw(
            SentinelError("nope")
        ),
    )
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    with pytest.raises(RuntimeError) as excinfo:
        chrome_driver.create_driver(headless=True)

    message = str(excinfo.value)
    assert "detected:" in message
    assert "requested/used:" in message
    assert "source:" in message


def test_final_error_logs_version_mismatch_hint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **k: (_ for _ in ()).throw(
            SentinelError("only supports Chrome version 150")
        ),
    )
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)

    with pytest.raises(RuntimeError):
        chrome_driver.create_driver(headless=True)

    assert "version mismatch" in " ".join(caplog.messages).lower()


def test_create_driver_uses_webdriver_manager_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_driver = object()
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **k: (_ for _ in ()).throw(
            SentinelError("nope")
        ),
    )
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(
        chrome_driver, "_try_webdriver_manager_fallback", lambda: fallback_driver
    )

    assert chrome_driver.create_driver(headless=True) is fallback_driver


def test_get_driver_invariant_with_and_without_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int | None] = []
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **k: captured.append(version_main)
        or object(),
    )
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: 151)

    chrome_driver.get_driver("/opt/chrome", chrome_version=149)
    chrome_driver.get_driver("/opt/chrome")

    assert captured == [149, 151]


# ---------------------------------------------------------------------------
# create_driver retry loop (PermissionError / RuntimeError)
# ---------------------------------------------------------------------------


def test_create_driver_retries_on_file_lock_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    state = {"attempts": 0}

    def fake_inner(**kwargs: Any) -> Any:
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise PermissionError("WinError 32 file in use")
        return fake_driver

    monkeypatch.setattr(chrome_driver, "_create_driver_inner", fake_inner)
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    assert chrome_driver.create_driver() is fake_driver
    assert state["attempts"] == 2


def test_create_driver_non_file_lock_oserror_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_inner(**kwargs: Any) -> Any:
        raise OSError("some other failure")

    monkeypatch.setattr(chrome_driver, "_create_driver_inner", fake_inner)
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    with pytest.raises(OSError, match="some other failure"):
        chrome_driver.create_driver()


def test_create_driver_file_lock_exhausts_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_inner(**kwargs: Any) -> Any:
        raise PermissionError("WinError 32 still locked")

    monkeypatch.setattr(chrome_driver, "_create_driver_inner", fake_inner)
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        chrome_driver.create_driver()


def test_create_driver_runtime_error_retries_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_driver = object()
    state = {"attempts": 0}

    def fake_inner(**kwargs: Any) -> Any:
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise RuntimeError("all strategies failed")
        return fake_driver

    monkeypatch.setattr(chrome_driver, "_create_driver_inner", fake_inner)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    assert chrome_driver.create_driver() is fake_driver
    assert state["attempts"] == 2


def test_create_driver_runtime_error_non_windows_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_inner(**kwargs: Any) -> Any:
        raise RuntimeError("all strategies failed")

    monkeypatch.setattr(chrome_driver, "_create_driver_inner", fake_inner)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError, match="all strategies failed"):
        chrome_driver.create_driver()


def test_create_driver_inner_strategy_returns_no_driver_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strategy yielding ``(None, None)`` loops on without recording an error.

    This pins the ``error is None`` branch of the attempt loop: the first
    strategy reports neither a driver nor an error, so the loop continues to the
    next attempt without flipping ``all_file_lock`` or storing ``last_error``.
    """

    fake_driver = object()
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(
        chrome_driver,
        "_try_strategy_default",
        lambda **kwargs: (None, None),
    )
    # Strategy 3 (no version) succeeds and short-circuits the rest.
    monkeypatch.setattr(
        chrome_driver,
        "_try_strategy_no_version",
        lambda **kwargs: (fake_driver, None),
    )

    assert chrome_driver._create_driver_inner(headless=True) is fake_driver


def test_create_driver_inner_all_file_lock_raises_permission_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All strategies failing with WinError 32 on Windows raises PermissionError."""

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module,
        "Chrome",
        lambda *, options, version_main=None, **k: (_ for _ in ()).throw(
            OSError("WinError 32 file in use")
        ),
    )
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: None)
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: None)
    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", False)
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    with pytest.raises(PermissionError, match="file lock"):
        chrome_driver._create_driver_inner(headless=True)
